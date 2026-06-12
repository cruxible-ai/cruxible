"""Guardrails for Docker-dependent test opt-in."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_image_tests_are_docker_marked() -> None:
    test_source = (REPO_ROOT / "tests" / "test_image" / "test_runtime_image.py").read_text()

    assert "pytestmark = pytest.mark.docker" in test_source


def test_docker_marker_is_registered() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]

    assert any(marker.startswith("docker:") for marker in markers)


def test_ci_runs_docker_marked_tests_in_dedicated_job() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "docker-image-test:" in workflow
    assert "docker info" in workflow
    assert "CRUXIBLE_RUN_DOCKER_TESTS" in workflow
    assert "uv run pytest tests/test_image -m docker --tb=short" in workflow
