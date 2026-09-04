"""Shared test fixtures for cruxible-core."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

_DOCKER_TEST_ENV = "CRUXIBLE_RUN_DOCKER_TESTS"
_WHEEL_TEST_ENV = "CRUXIBLE_RUN_WHEEL_TESTS"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


@pytest.fixture(scope="session", autouse=True)
def isolate_server_state_root(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep every test away from user-scoped current and legacy daemon state."""

    state_root = tmp_path_factory.mktemp("server-state")
    previous = os.environ.get("CRUXIBLE_STATE_ROOT")
    os.environ["CRUXIBLE_STATE_ROOT"] = str(state_root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CRUXIBLE_STATE_ROOT", None)
        else:
            os.environ["CRUXIBLE_STATE_ROOT"] = previous


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
    request: pytest.FixtureRequest,
) -> None:
    """Keep tests isolated from any user-scoped remembered CLI/server context."""

    context_dir = tmp_path_factory.mktemp("cli-context")
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_dir / "client-context.json"))
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("CRUXIBLE_PLAYBILL_WORKSPACE", raising=False)
    if request.node.get_closest_marker("state_root_fallback") is not None:
        isolated_home = tmp_path_factory.mktemp("state-root-fallback-home")
        monkeypatch.setenv("HOME", str(isolated_home))
        monkeypatch.delenv("CRUXIBLE_STATE_ROOT", raising=False)


@pytest.fixture
def configs_dir() -> Path:
    """Path to the test config fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_playbill_manager_singleton() -> None:
    """Start every test with a manager singleton that shadows none of its methods.

    `get_playbill_manager()` is process-wide, and several tests patch a method
    ON THAT OBJECT rather than on its class -- `monkeypatch.setattr(manager,
    "get", ...)`. Undo does not DELETE what it set: it writes back the value it
    read, and reading a method off an instance yields a BOUND METHOD, so the
    undo installs that bound method into the instance `__dict__`. The singleton
    then carries a permanent shadow of its own class attribute, and it outlives
    both `manager.clear()` and the module that made it.

    Nothing notices where it is made, because the shadow IS the real method. A
    later test that patches the CLASS does: its patch becomes invisible, the
    real method runs, and the failure lands somewhere else entirely -- a daemon
    startup test whose injected recovery fold never fires, reported as card 122
    and reproducible at the base commit with those two files alone.

    Cleaning at SETUP rather than teardown is deliberate: a teardown finalizer
    can run before `monkeypatch`'s own undo, which would then reinstate the
    shadow. Only what a test finds when it starts is under its control.
    """

    from cruxible_core.runtime.playbill_manager import get_playbill_manager

    manager = get_playbill_manager()
    for name, value in tuple(vars(manager).items()):
        if callable(value) and callable(getattr(type(manager), name, None)):
            delattr(manager, name)
