"""Network allowlist policy for the bwrap sandbox.

When HARES_SANDBOX_NETWORK_ALLOW is set, Hares uses a combination of:

  --unshare-net   (isolated network namespace — we get CAP_NET_ADMIN there)
  slirp4netns     (userspace TCP/IP bridge providing real connectivity)
  nftables        (kernel-level filtering within the isolated netns)

to allow outbound connections only to declared host:port pairs while
blocking everything else.

Why each piece:

  --unshare-net alone   → isolated, no connectivity.  Equivalent to NETWORK=off.
  slirp4netns           → provides real connectivity into the isolated netns
                          without needing root on the host.
  nftables in netns     → kernel-level filtering; the process CAN'T bypass it
                          because it's in the kernel, not in userspace.

Without slirp4netns, the allowlist mode falls back to NETWORK=off with a
warning. It's correct to fail closed — better no network than unfiltered.

Configuration
-------------
HARES_SANDBOX_NETWORK_ALLOW=github.com:443,pypi.org:443,8.8.8.8:53

Each entry: host:port (host may be a domain name or IP address, port is
an integer). Loopback is always allowed. DNS resolution happens at server
startup so IPs may rotate; operators using CDN-backed hosts (GitHub, etc.)
should expect occasional stale-IP failures until the server restarts.

What this does and doesn't do
------------------------------
DOES: Prevent connections to non-allowlisted IPs/ports. An LLM agent
  cannot exfiltrate data to evil.com, cannot hit arbitrary APIs.

DOES NOT: Distinguish HTTP GET from POST to allowlisted hosts. GitHub
  allowed means git clone AND git push are both reachable. The fix for
  that is credential scoping (read-only tokens), not network filtering.
  This is documented in the README threat model section.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# slirp4netns binary name. Can be overridden for tests.
SLIRP4NETNS_BIN = os.environ.get("HARES_SLIRP4NETNS_BIN", "slirp4netns")

# Default tap interface name slirp4netns creates.
SLIRP_TAP = "tap0"

# slirp4netns default address assignments (--configure mode).
SLIRP_GUEST_IP = "10.0.2.100"
SLIRP_NETMASK  = "24"
SLIRP_GATEWAY  = "10.0.2.2"


@dataclass(frozen=True)
class AllowEntry:
    """One resolved host:port allowlist entry."""
    host: str         # original host string (for logging)
    ip: str           # resolved IP address
    port: int         # TCP port


@dataclass(frozen=True)
class NetworkPolicy:
    """Parsed and resolved network allowlist.

    ``allow`` is the resolved list; ``raw`` is the original spec string for
    logging/display. An empty allow list means the policy is disabled (full
    network access, current default).
    """
    allow: tuple[AllowEntry, ...]
    raw: str          # original HARES_SANDBOX_NETWORK_ALLOW value

    @property
    def enabled(self) -> bool:
        return bool(self.allow)


def load_network_policy() -> Optional[NetworkPolicy]:
    """Build a NetworkPolicy from HARES_SANDBOX_NETWORK_ALLOW.

    Returns None when the env var is unset or empty (no policy = full
    network access, unchanged from current behaviour).
    """
    raw = os.environ.get("HARES_SANDBOX_NETWORK_ALLOW", "").strip()
    if not raw:
        return None

    entries: list[AllowEntry] = []
    parse_errors: list[str] = []

    for spec in raw.split(","):
        spec = spec.strip()
        if not spec:
            continue
        if ":" not in spec:
            parse_errors.append(f"{spec!r}: missing port (expected host:port)")
            continue
        host, port_str = spec.rsplit(":", 1)
        host = host.strip()
        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                raise ValueError("port out of range")
        except ValueError:
            parse_errors.append(f"{spec!r}: invalid port {port_str!r}")
            continue

        # Resolve hostname → IP at startup.
        try:
            # Use getaddrinfo to get the first IPv4 address.
            results = socket.getaddrinfo(host, port, socket.AF_INET,
                                          socket.SOCK_STREAM)
            ip = results[0][4][0]
            entries.append(AllowEntry(host=host, ip=ip, port=port))
            if host != ip:
                logger.info(
                    "Network allowlist: %s:%d resolved to %s:%d "
                    "(IPs may rotate; restart server to re-resolve)",
                    host, port, ip, port,
                )
        except socket.gaierror as exc:
            parse_errors.append(f"{spec!r}: DNS resolution failed: {exc}")

    if parse_errors:
        logger.warning(
            "HARES_SANDBOX_NETWORK_ALLOW: could not parse/resolve %d entr%s:\n  %s",
            len(parse_errors),
            "y" if len(parse_errors) == 1 else "ies",
            "\n  ".join(parse_errors),
        )

    if not entries:
        logger.warning(
            "HARES_SANDBOX_NETWORK_ALLOW=%r produced no usable entries; "
            "falling back to NETWORK=off (no external connectivity).",
            raw,
        )

    return NetworkPolicy(allow=tuple(entries), raw=raw)


def slirp4netns_available() -> bool:
    """Return True if slirp4netns is on PATH."""
    return shutil.which(SLIRP4NETNS_BIN) is not None


def build_inner_setup_script(policy: NetworkPolicy, inner_command: str) -> str:
    """Build a shell script that configures the isolated netns and execs the
    real command.

    This runs INSIDE bwrap's network namespace after slirp4netns has set up
    the tap interface. It:
      1. Brings up tap0 and lo (slirp4netns --configure handles IP assignment)
      2. Sets up nftables allowlist rules
      3. Execs the real command

    We exec rather than calling normally so the inner command's PID replaces
    the shell PID — needed for killpg and resource tracking to work correctly.
    """
    nft_rules = _build_nftables_rules(policy)

    # slirp4netns --configure assigns the IP; we just need to bring up the
    # interface and set the default route.
    setup = f"""
set -e
# Networking (slirp4netns --configure assigns tap0 IP + gateway).
ip link set lo up 2>/dev/null || true
ip link set {SLIRP_TAP} up 2>/dev/null || true
ip route add default via {SLIRP_GATEWAY} dev {SLIRP_TAP} 2>/dev/null || true

# nftables allowlist — drop by default, allow declared IPs/ports only.
{nft_rules}

# Exec the actual command so its PID replaces this shell.
exec /bin/sh -c {_sh_quote(inner_command)}
""".strip()
    return setup


def _build_nftables_rules(policy: NetworkPolicy) -> str:
    """Generate nft commands for the allowlist.

    Default policy: DROP outbound connections. Allow:
      - loopback (lo)
      - established/related (so TCP replies flow back in)
      - explicitly declared host:port pairs
    """
    lines: list[str] = [
        "nft add table inet hares_filter 2>/dev/null || true",
        "nft delete table inet hares_filter 2>/dev/null || true",
        "nft add table inet hares_filter",
        # Output chain: filter outbound connections, default drop.
        "nft 'add chain inet hares_filter output { type filter hook output priority 0; policy drop; }'",
        # Always allow loopback.
        "nft add rule inet hares_filter output oifname lo accept",
        # Allow replies to connections we initiated.
        "nft add rule inet hares_filter output ct state established,related accept",
    ]

    for entry in policy.allow:
        lines.append(
            f"nft add rule inet hares_filter output "
            f"ip daddr {entry.ip} tcp dport {entry.port} accept"
            f"  # {entry.host}"
        )

    if not policy.allow:
        lines.append("# No allowlist entries — all outbound blocked.")

    return "\n".join(lines)


def _sh_quote(s: str) -> str:
    """Single-quote a string for embedding in a /bin/sh -c argument."""
    return "'" + s.replace("'", "'\\''") + "'"
