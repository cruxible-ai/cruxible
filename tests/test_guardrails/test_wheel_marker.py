"""Guardrails for the wheel-install test opt-in."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_wheel_install_tests_are_wheel_marked() -> None:
    test_source = (
        REPO_ROOT / "tests" / "test_packaging" / "test_wheel_console_scripts.py"
    ).read_text()

    assert "pytestmark = pytest.mark.wheel" in test_source


def test_wheel_marker_is_registered() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]

    assert any(marker.startswith("wheel:") for marker in markers)


def test_ci_runs_wheel_marked_tests_in_dedicated_job() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "wheel-install-test:" in workflow
    assert "CRUXIBLE_RUN_WHEEL_TESTS" in workflow
    assert "uv run pytest tests/test_packaging -m wheel --tb=short" in workflow
