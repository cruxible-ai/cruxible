"""Shared test fixtures for cruxible-core."""

import contextlib
import os
import tempfile
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


def test_owned_roots(base_temp: Path) -> tuple[Path, ...]:
    """The directories a test is allowed to have put a workspace binding in.

    Both spellings, because tests reach for both: the session's pytest temp
    tree, and the platform temp directory that `tempfile` hands out directly.
    Resolved, because macOS answers `/var/folders/...` and resolves it to
    `/private/var/folders/...`, and one comparison has to see them as one place.
    """

    roots: list[Path] = []
    for candidate in (base_temp, Path(tempfile.gettempdir())):
        with contextlib.suppress(OSError):
            roots.append(candidate.expanduser().resolve())
    return tuple(roots)


def _is_test_owned(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:  # pragma: no cover - unreadable candidate root
        return False
    return any(resolved.is_relative_to(root) for root in roots)


@pytest.fixture(autouse=True)
def isolate_workspace_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """No test reads a Playbill workspace binding it did not create.

    Workspace discovery walks up from the current directory to the enclosing Git
    checkout looking for `.playbill/coverage.json`, and prefers what it finds
    over the remembered CLI context -- which is exactly right in a terminal and
    exactly wrong in a test. A developer whose checkout is itself a governed
    workspace was running a suite that silently retargeted at their real
    instance: `test_remembered_playbill_write_marks_remembered_target` read the
    live binding instead of the remembered one it had just written, and failed
    on a machine where nothing was wrong with the code.

    So the discovery seam answers "no binding here" for any candidate root
    outside the test's own temporary directories. A test that wants a binding
    still gets one -- it writes the file under `tmp_path` first, which is the
    only way it could have known what to assert about it anyway. The suite's
    answer stops depending on where the checkout happens to be.

    Both discovery doors are covered: the CLI/SDK target resolver and the block
    reader, which each read the same file through their own helper.
    """

    from cruxible_client.authoring import blocks as playbill_blocks
    from cruxible_client.authoring import context as playbill_context

    roots = test_owned_roots(tmp_path_factory.getbasetemp())
    resolve_binding = playbill_context._workspace_binding
    read_binding = playbill_blocks._workspace_binding

    def _context_binding(root: Path):
        if not _is_test_owned(root, roots):
            return None, None
        return resolve_binding(root)

    def _block_binding(root: Path):
        if not _is_test_owned(root, roots):
            return None
        return read_binding(root)

    monkeypatch.setattr(playbill_context, "_workspace_binding", _context_binding)
    monkeypatch.setattr(playbill_blocks, "_workspace_binding", _block_binding)


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
