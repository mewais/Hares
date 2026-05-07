# Contributing to Hares

## Development setup

```sh
git clone https://github.com/Maksab/Hares.git
cd Hares
pip install -e ".[dev]"
```

The full test suite requires [bubblewrap](https://github.com/containers/bubblewrap):

```sh
# Debian / Ubuntu
sudo apt-get install bubblewrap

# Fedora / RHEL
sudo dnf install bubblewrap
```

Run all tests:

```sh
pytest -q
```

To skip bwrap tests on a machine without bubblewrap:

```sh
HARES_SANDBOX_DISABLED=1 pytest -q
```

## Submitting changes

- Open an issue first for non-trivial changes so we can discuss the approach.
- Keep pull requests focused — one feature or fix per PR.
- Add tests for new behaviour. The CI matrix runs on Python 3.10, 3.11, and 3.12.
- Run the full test suite locally before opening the PR.

## Reporting bugs

Open a [GitHub issue](https://github.com/Maksab/Hares/issues) with:
- Hares version (`hares-mcp --version`)
- OS and kernel version
- Minimal reproduction steps
- Expected vs actual behaviour

## Security issues

Please **do not** open a public GitHub issue for security vulnerabilities.
Email `mohammad.a.ewais@gmail.com` directly.
