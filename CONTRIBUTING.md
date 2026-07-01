# Contributing to Cruxible Core

Thanks for your interest in contributing! This document covers the basics.

## Development Setup

```bash
git clone https://github.com/cruxible-ai/cruxible-core.git
cd cruxible-core
uv sync --all-extras
```

## Running Tests

```bash
uv run pytest
```

## Code Quality

```bash
uv run ruff check src tests   # lint
uv run ruff format src tests  # format
uv run mypy src               # type check
```

## Pull Requests

1. Fork the repo and create a branch from `main`
2. Make your changes with tests
3. Ensure `uv run pytest`, `uv run ruff check`, and `uv run mypy src` all pass
4. Open a pull request with a clear description of what and why

## Reporting Issues

Open a GitHub issue with:
- What you expected
- What happened
- Steps to reproduce
- Cruxible version (`cruxible --version`)

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
