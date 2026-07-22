#!/bin/bash
# Mirror CI's check suite locally, minus the docker-image tests (CI-only
# environment). Run this ONCE before any push: every step here has caused
# a post-push CI red at least once when skipped (format: v0.2.7; mypy:
# v0.2.8). Scoped test runs during iteration are fine - this is the gate.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== ruff check"
uv run ruff check src packages/cruxible-client/src tests
echo "== ruff format --check"
uv run ruff format --check src packages/cruxible-client/src tests
echo "== mypy"
uv run mypy src packages/cruxible-client/src
echo "== pytest (non-golden)"
uv run pytest tests/ --ignore=tests/test_golden --ignore=tests/goldens -q
echo "== kit lockfiles"
uv run python scripts/check_kit_lockfiles.py
echo "CI PARITY: all local checks green (docker-image tests remain CI-only)"
