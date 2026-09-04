"""The hosted execution policy refuses before any Provider child can start."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_core.errors import (
    CustomerCodeExecutionUnsupportedError,
    HostedProfileUnknownError,
    IsolatedExecutorDiscoveryError,
)
from cruxible_core.playbill import provider_local_runtime as runtime_module
from cruxible_core.playbill.service import provider_seed as seed_module
from cruxible_core.runtime import execution_policy as policy_module
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.execution_policy import (
    ISOLATION_BACKEND_NOT_IMPLEMENTED,
    customer_code_execution_supported,
    discover_isolated_executors,
    enforce_customer_code_execution_supported,
    register_isolated_executor,
    registered_isolated_executors,
)

PROFILE = "CRUXIBLE_HOSTED_SERVER_PROFILE"
BACKEND = "CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND"


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything reaches a real process spawn."""

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a child process was spawned under the shared hosted profile")

    monkeypatch.setattr(runtime_module.subprocess, "Popen", _forbidden)
    monkeypatch.setattr(seed_module.subprocess, "run", _forbidden)


def _shared_without_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE, "shared")
    monkeypatch.delenv(BACKEND, raising=False)


def test_the_shared_profile_without_a_backend_refuses_customer_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shared_without_backend(monkeypatch)

    assert customer_code_execution_supported() is False
    with pytest.raises(CustomerCodeExecutionUnsupportedError) as refused:
        enforce_customer_code_execution_supported()
    assert refused.value.error_code == "customer_code_execution_unsupported"


@pytest.mark.parametrize(
    "environment",
    (
        {PROFILE: "shared", BACKEND: "docker"},
        {PROFILE: "shared", BACKEND: "firecracker"},
        {PROFILE: "shared"},
    ),
    ids=("docker-backend", "unregistered-backend", "no-backend"),
)
def test_the_shared_profile_refuses_until_an_executor_is_registered(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
) -> None:
    """Naming a backend is a claim; only a REGISTERED executor is a mechanism.

    `docker` used to re-enable spawning the Provider directly on the host,
    because no container executor exists here (maintainer ruling 2026-09-03).
    """

    monkeypatch.delenv(PROFILE, raising=False)
    monkeypatch.delenv(BACKEND, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert registered_isolated_executors() == {}
    assert customer_code_execution_supported() is False
    with pytest.raises(CustomerCodeExecutionUnsupportedError) as refused:
        enforce_customer_code_execution_supported()
    assert refused.value.detail is not None
    assert refused.value.detail.startswith(ISOLATION_BACKEND_NOT_IMPLEMENTED)
    configured = environment.get(BACKEND)
    if configured is not None:
        assert f"backend {configured!r} is not registered" in refused.value.detail


def test_execution_proceeds_when_no_hosted_profile_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROFILE, raising=False)
    monkeypatch.delenv(BACKEND, raising=False)

    assert customer_code_execution_supported() is True
    enforce_customer_code_execution_supported()


def test_an_unknown_hosted_profile_refuses_typed_instead_of_failing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile this build cannot read is not evidence that it is unrestricted."""

    monkeypatch.setenv(PROFILE, "Shared-Pool")
    monkeypatch.delenv(BACKEND, raising=False)

    assert customer_code_execution_supported() is False
    with pytest.raises(HostedProfileUnknownError) as refused:
        enforce_customer_code_execution_supported()
    assert refused.value.error_code == "hosted_profile_unknown"
    assert refused.value.profile == "shared-pool"
    assert "repair:" in str(refused.value)


def test_the_unknown_profile_refusal_maps_to_a_typed_403_and_a_typed_client_error() -> None:
    from cruxible_client.errors import HostedProfileUnknownError as ClientRefusal
    from cruxible_client.errors import response_to_error
    from cruxible_core.server.errors import error_to_response

    status, body = error_to_response(HostedProfileUnknownError("shared-pool"))

    assert status == 403
    assert body.error_type == "HostedProfileUnknownError"
    assert body.error_code == "hosted_profile_unknown"
    reconstructed = response_to_error(status, body)
    assert isinstance(reconstructed, ClientRefusal)
    assert reconstructed.profile == "shared-pool"


def test_the_provider_child_chokepoint_refuses_before_popen(
    monkeypatch: pytest.MonkeyPatch,
    no_spawn: None,
    tmp_path: Path,
) -> None:
    """The gate is inside `_run_child`, so no later caller can bypass it."""

    _shared_without_backend(monkeypatch)

    with pytest.raises(CustomerCodeExecutionUnsupportedError):
        runtime_module._run_child(
            Path("/nonexistent/python"),
            entrypoint="cruxible_provider_workspace.child",
            context=b"{}",
            budgets=None,  # type: ignore[arg-type]
            secret_fd=None,
            invocation_id="INV-hosted-policy",
            process_leases=None,  # type: ignore[arg-type]
        )


def test_the_seed_materialization_refuses_before_building_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
    no_spawn: None,
    tmp_path: Path,
) -> None:
    _shared_without_backend(monkeypatch)

    with pytest.raises(CustomerCodeExecutionUnsupportedError):
        seed_module._derive_local_seed_pins(str(tmp_path), "0" * 40)


@pytest.mark.parametrize(
    ("verb", "arguments"),
    (
        ("playbill_procedure_run", ("inst_policy", "demo.procedure")),
        ("playbill_line_run", ("inst_policy", "sha256:" + "a" * 64)),
        ("playbill_provider_seed", ("inst_policy",)),
    ),
)
def test_the_served_run_verbs_refuse_before_touching_any_instance(
    monkeypatch: pytest.MonkeyPatch,
    no_spawn: None,
    verb: str,
    arguments: tuple[str, ...],
) -> None:
    """The refusal is the served answer, not a node refusal inside a run journal."""

    _shared_without_backend(monkeypatch)
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_a, **_k: None)
    monkeypatch.setattr(
        playbill_api,
        "get_playbill_manager",
        lambda: pytest.fail("the served verb loaded an instance before refusing"),
    )
    callable_verb = getattr(playbill_api, verb)

    with pytest.raises(CustomerCodeExecutionUnsupportedError):
        if verb == "playbill_provider_seed":
            callable_verb(*arguments)
        else:
            callable_verb(*arguments, request=None)  # type: ignore[arg-type]


def test_the_refusal_maps_to_a_typed_403_and_a_typed_client_error() -> None:
    from cruxible_client.errors import CustomerCodeExecutionUnsupportedError as ClientRefusal
    from cruxible_client.errors import response_to_error
    from cruxible_core.server.errors import error_to_response

    status, body = error_to_response(CustomerCodeExecutionUnsupportedError())

    assert status == 403
    assert body.error_type == "CustomerCodeExecutionUnsupportedError"
    assert body.error_code == "customer_code_execution_unsupported"
    assert isinstance(response_to_error(status, body), ClientRefusal)


def test_the_cli_renders_the_refusal_through_handle_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-2 class: a security refusal must never reach the operator as empty output."""

    import click
    from click.testing import CliRunner

    from cruxible_core.cli.main import handle_errors

    @click.command("probe")
    @handle_errors
    def probe() -> None:
        raise CustomerCodeExecutionUnsupportedError()

    result = CliRunner().invoke(probe, [])

    assert result.exit_code == 1
    assert result.output.strip() != ""
    assert "CustomerCodeExecutionUnsupportedError" in result.output
    assert "not supported in this hosted runtime profile" in result.output


def test_the_driver_refuses_before_it_resolves_a_tenant_secret(
    monkeypatch: pytest.MonkeyPatch,
    no_spawn: None,
) -> None:
    """No customer secret material is decrypted for a run that will be refused."""

    _shared_without_backend(monkeypatch)

    class _ForbiddenResolvers:
        def resolve(self, _plan: object) -> object:
            raise AssertionError("a tenant secret was resolved for a refused run")

    driver = runtime_module.LocalProviderExecutionDriver()

    with pytest.raises(CustomerCodeExecutionUnsupportedError):
        driver.invoke(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            secret_plan=None,  # type: ignore[arg-type]
            secret_resolvers=_ForbiddenResolvers(),  # type: ignore[arg-type]
            invocation_id="INV-secret-order",
            process_leases=None,  # type: ignore[arg-type]
        )


class _StubIsolatedExecutor:
    """A minimal out-of-tree executor registering through the typed seam."""

    def __init__(self, backend_id: str) -> None:
        self._backend_id = backend_id

    def registration(self) -> contracts.IsolatedExecutorRegistrationV1:
        return contracts.IsolatedExecutorRegistrationV1(
            backend_id=self._backend_id,
            implementation_digest="sha256:" + "5" * 64,
            capabilities=("process-isolation",),
        )


def test_core_registers_no_isolated_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The empty registry is the law, not an accident of import order."""

    monkeypatch.setattr(policy_module, "_REGISTERED_ISOLATED_EXECUTORS", {})
    assert registered_isolated_executors() == {}


def test_a_registered_executor_is_what_permits_shared_profile_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration, not an environment string, is what unlocks execution."""

    monkeypatch.setattr(policy_module, "_REGISTERED_ISOLATED_EXECUTORS", {})
    monkeypatch.setenv(PROFILE, "shared")
    monkeypatch.setenv(BACKEND, "stub-isolation")

    with pytest.raises(CustomerCodeExecutionUnsupportedError):
        enforce_customer_code_execution_supported()

    record = register_isolated_executor(_StubIsolatedExecutor("stub-isolation"))

    assert record.backend_id == "stub-isolation"
    assert registered_isolated_executors() == {"stub-isolation": record}
    assert customer_code_execution_supported() is True
    enforce_customer_code_execution_supported()


def test_a_second_executor_cannot_silently_take_over_a_backend_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy_module, "_REGISTERED_ISOLATED_EXECUTORS", {})
    register_isolated_executor(_StubIsolatedExecutor("stub-isolation"))

    class _Impostor(_StubIsolatedExecutor):
        def registration(self) -> contracts.IsolatedExecutorRegistrationV1:
            return contracts.IsolatedExecutorRegistrationV1(
                backend_id="stub-isolation",
                implementation_digest="sha256:" + "6" * 64,
            )

    with pytest.raises(ValueError, match="already registered"):
        register_isolated_executor(_Impostor("stub-isolation"))


class _FakeDistribution:
    """A real on-disk distribution advertising executors at an entry-point group.

    Nothing here is a double: the files are the ones a wheel installs, and
    discovery reads them through `importlib.metadata` exactly as it will read a
    Cloud tenant image's executor package.
    """

    def __init__(self, root: Path, *, module: str, entry_points: dict[str, str]) -> None:
        self.root = root
        (root / f"{module}.py").write_text(_EXECUTOR_MODULE, encoding="utf-8")
        dist_info = root / "fake_executor-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: fake-executor\nVersion: 1.0\n",
            encoding="utf-8",
        )
        rendered = "\n".join(f"{name} = {value}" for name, value in entry_points.items())
        (dist_info / "entry_points.txt").write_text(
            f"[{policy_module.ISOLATED_EXECUTOR_ENTRY_POINT_GROUP}]\n{rendered}\n",
            encoding="utf-8",
        )


_EXECUTOR_MODULE = '''
from cruxible_client import contracts


class PackagedExecutor:
    def registration(self):
        return contracts.IsolatedExecutorRegistrationV1(
            backend_id="packaged-isolation",
            implementation_digest="sha256:" + "7" * 64,
            capabilities=("process-isolation",),
        )


packaged = PackagedExecutor()


class NotAnExecutor:
    """Advertised, loadable, and missing the one method the seam requires."""


class UnbuildableExecutor:
    def __init__(self):
        raise RuntimeError("the image never shipped the runtime this needs")

    def registration(self):  # pragma: no cover - never constructed
        raise AssertionError("unreachable")
'''


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy_module, "_REGISTERED_ISOLATED_EXECUTORS", {})


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry_points: dict[str, str],
) -> None:
    _FakeDistribution(tmp_path, module="fake_executor_pkg", entry_points=entry_points)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()


def test_a_packaged_executor_is_discovered_and_registered_at_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_registry: None,
) -> None:
    """The registry is built from what is INSTALLED, never from an env string."""

    _install(
        monkeypatch,
        tmp_path,
        {"packaged": "fake_executor_pkg:packaged"},
    )

    registered = discover_isolated_executors()

    assert [item.backend_id for item in registered] == ["packaged-isolation"]
    assert set(registered_isolated_executors()) == {"packaged-isolation"}
    monkeypatch.setenv(PROFILE, "shared")
    monkeypatch.setenv(BACKEND, "packaged-isolation")
    assert customer_code_execution_supported() is True


def test_a_discovered_backend_id_reaches_the_server_info_provider_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_registry: None,
) -> None:
    """An operator can see which backend is doing the isolating, by id."""

    _install(monkeypatch, tmp_path, {"packaged": "fake_executor_pkg:packaged"})
    discover_isolated_executors()

    lane = contracts.ProviderLaneStatusV1(
        state="available",
        code=None,
        detail=None,
        isolated_executors=tuple(sorted(registered_isolated_executors())),
    )

    assert lane.isolated_executors == ("packaged-isolation",)


def test_an_entry_point_that_cannot_be_imported_refuses_typed_and_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_registry: None,
) -> None:
    """Fail closed: a broken advertisement stops the daemon, naming itself."""

    _install(monkeypatch, tmp_path, {"broken": "fake_executor_pkg:missing_attribute"})

    with pytest.raises(IsolatedExecutorDiscoveryError) as excinfo:
        discover_isolated_executors()

    assert excinfo.value.entry_point == "fake_executor_pkg:missing_attribute"
    assert excinfo.value.group == policy_module.ISOLATED_EXECUTOR_ENTRY_POINT_GROUP
    assert "repair:" in str(excinfo.value)
    assert registered_isolated_executors() == {}


def test_an_object_that_is_not_an_executor_refuses_typed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_registry: None,
) -> None:
    _install(monkeypatch, tmp_path, {"wrong": "fake_executor_pkg:NotAnExecutor"})

    with pytest.raises(IsolatedExecutorDiscoveryError) as excinfo:
        discover_isolated_executors()

    assert "IsolatedExecutor protocol" in str(excinfo.value)
    assert registered_isolated_executors() == {}


def test_an_executor_that_cannot_be_constructed_refuses_typed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_registry: None,
) -> None:
    _install(monkeypatch, tmp_path, {"unbuildable": "fake_executor_pkg:UnbuildableExecutor"})

    with pytest.raises(IsolatedExecutorDiscoveryError) as excinfo:
        discover_isolated_executors()

    assert "RuntimeError" in str(excinfo.value)
    assert registered_isolated_executors() == {}


def test_one_broken_advertisement_stops_the_whole_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_registry: None,
) -> None:
    """A partly-registered daemon is the state fail-closed exists to prevent."""

    _install(
        monkeypatch,
        tmp_path,
        {
            "aa-broken": "fake_executor_pkg:missing_attribute",
            "zz-packaged": "fake_executor_pkg:packaged",
        },
    )

    with pytest.raises(IsolatedExecutorDiscoveryError):
        discover_isolated_executors()

    assert registered_isolated_executors() == {}


def test_a_daemon_with_no_advertised_executor_registers_nothing(
    empty_registry: None,
) -> None:
    """Core ships no executor; discovery over an empty group is a no-op."""

    assert discover_isolated_executors() == ()
    assert registered_isolated_executors() == {}
