"""The hosted execution policy refuses before any Provider child can start."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.errors import CustomerCodeExecutionUnsupportedError
from cruxible_core.playbill import provider_local_runtime as runtime_module
from cruxible_core.playbill.service import provider_seed as seed_module
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.execution_policy import (
    customer_code_execution_supported,
    enforce_customer_code_execution_supported,
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
        {},
    ),
    ids=("docker-backend", "profile-unset"),
)
def test_execution_proceeds_when_the_profile_permits_it(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
) -> None:
    monkeypatch.delenv(PROFILE, raising=False)
    monkeypatch.delenv(BACKEND, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert customer_code_execution_supported() is True
    enforce_customer_code_execution_supported()


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
