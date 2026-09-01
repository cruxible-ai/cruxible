"""Shared test fixtures for cruxible-core."""

import os
from pathlib import Path

import pytest

_DOCKER_TEST_ENV = "CRUXIBLE_RUN_DOCKER_TESTS"
_WHEEL_TEST_ENV = "CRUXIBLE_RUN_WHEEL_TESTS"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep slow, environment-heavy opt-in suites out of the default run."""
    if not _opt_in_enabled(config, marker="docker", env_var=_DOCKER_TEST_ENV):
        _skip_marked(
            items,
            marker="docker",
            reason=(
                "Docker image tests are opt-in; "
                "set CRUXIBLE_RUN_DOCKER_TESTS=1 or run with -m docker"
            ),
        )

    if not _opt_in_enabled(config, marker="wheel", env_var=_WHEEL_TEST_ENV):
        _skip_marked(
            items,
            marker="wheel",
            reason=(
                "Wheel install tests are opt-in; "
                "set CRUXIBLE_RUN_WHEEL_TESTS=1 or run with -m wheel"
            ),
        )


def _skip_marked(items: list[pytest.Item], *, marker: str, reason: str) -> None:
    skip_marker = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker(marker) is not None:
            item.add_marker(skip_marker)


def _opt_in_enabled(config: pytest.Config, *, marker: str, env_var: str) -> bool:
    env_value = os.environ.get(env_var, "").strip().lower()
    if env_value in _TRUE_ENV_VALUES:
        return True

    marker_expression = (getattr(config.option, "markexpr", "") or "").strip()
    return marker in marker_expression and f"not {marker}" not in marker_expression


@pytest.fixture(autouse=True)
def isolate_cli_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Keep tests isolated from any user-scoped remembered CLI/server context."""

    context_dir = tmp_path_factory.mktemp("cli-context")
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_dir / "client-context.json"))
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("CRUXIBLE_PLAYBILL_WORKSPACE", raising=False)


@pytest.fixture
def configs_dir() -> Path:
    """Path to the test config fixtures directory."""
    return Path(__file__).parent / "fixtures"
