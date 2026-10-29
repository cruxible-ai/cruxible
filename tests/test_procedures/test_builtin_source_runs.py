"""The built-in workspace.file runs in-process: binding, lane rules and crash closure."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.providers import AcceptedProvider, provider_digest
from cruxible_core.exhaust.records import parse_journal_payload
from cruxible_core.governance.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
    workspace_file_builtin_provider,
    workspace_file_operation,
)
from cruxible_core.providers.builtin_runtime import (
    BuiltinDispatchingInvoker,
    BuiltinProviderInvoker,
    builtin_provider_binding,
)
from cruxible_core.providers.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeBudgets,
    ProviderRuntimeRunContext,
)
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequest,
    service_recover_provider_invocations,
    service_run_playbill_procedure,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_procedure_source_runs import (
    NOW,
    PROCEDURE_NAME,
    _actor,
    _reader,
    _world,
)


def _accepted() -> tuple[AcceptedProvider, Any]:
    operation = workspace_file_operation()
    provider = workspace_file_builtin_provider()
    accepted = AcceptedProvider(
        path="providers/cruxible-builtin.json",
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
    return accepted, operation


def _context(binding: Any, payload: dict[str, object], *, output_bytes: int = 1 << 20) -> Any:
    return ProviderRuntimeRunContext(
        protocol_version="2.0",
        run_id="run-1",
        operation=binding.operation,
        operation_signature_digest=binding.operation_signature_digest,
        build_digest=binding.provider_artifact_digest,
        implementation_digest=binding.implementation_digest,
        entrypoint=binding.entrypoint,
        input=payload,
        budgets=ProviderRuntimeBudgets(wall_clock_seconds=5.0, output_bytes=output_bytes),
    )


def _payload(data: bytes) -> dict[str, object]:
    return {
        "logical_source": "docs/readme",
        "commitment_digest": "sha256:" + "c0" * 32,
        "content_encoding": "base64",
        "bytes": base64.b64encode(data).decode("ascii"),
        "byte_length": len(data),
        "bytes_digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def test_a_degraded_provider_lane_still_admits_and_runs_the_built_in(tmp_path: Path) -> None:
    operator = ProviderRuntimeOperator(tmp_path / "state")
    operator.mark_unavailable("provider_runtime_recovery_failed", "fences are down")
    provider, interface = _accepted()

    binding = operator.admit_line_provider(
        provider,
        interface,
        WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
        eligible_environment_pin_keys=("builtin",),
    )
    assert binding.fence_scope == "in_process"
    assert "fence_scope" in binding.model_dump(mode="json")

    invoker = operator.invoker_for(SimpleNamespace(), accepted_oid="a" * 40)  # type: ignore[arg-type]
    occurrence = SimpleNamespace(local_execution=binding)
    bound = invoker.bind_provider(occurrence=occurrence)
    outcome = invoker.invoke_provider(
        occurrence=occurrence,
        context=_context(binding, _payload(b"high\n")),
        invocation_id="sha256:" + "1" * 64,
        bound=bound,
        deadline=None,
    )
    assert outcome.envelope.status == "ok"
    assert outcome.envelope.output["content"]["lines"] == ["high"]
    # The output's bucket is the adapter's own reading of the bytes, never the
    # context's meaningless placeholder.
    assert outcome.envelope.output["input_bucket"] == "content_kind=text;byte_size=tiny"
    assert outcome.egress.observer_backend == "core.in-process"
    assert outcome.verified_binding == binding
    # A subprocess occurrence still refuses on the degraded lane.
    with pytest.raises(ProviderLocalRuntimeRefused) as refused:
        invoker.bind_provider(occurrence=object())
    assert refused.value.code == "provider_unavailable"


def test_a_provider_row_that_borrows_the_built_in_digest_refuses() -> None:
    provider, interface = _accepted()
    (record,) = provider.provider.implementations
    reference = record.materialization_references[0].model_copy(
        update={"materialization_digest": "sha256:" + "e" * 64}
    )
    forged = provider.provider.model_construct(
        **{
            **dict(provider.provider),
            "implementations": (
                record.model_copy(update={"materialization_references": (reference,)}),
            ),
        }
    )
    with pytest.raises(ProviderLocalRuntimeRefused) as refused:
        builtin_provider_binding(
            provider.model_copy(update={"provider": forged}),
            interface,
            WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
        )
    assert refused.value.code == "acceptance_divergence"
    assert builtin_provider_binding(provider, interface, "sha256:" + "0" * 64) is None


def test_the_built_in_honours_its_output_budget() -> None:
    provider, interface = _accepted()
    binding = builtin_provider_binding(
        provider, interface, WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST
    )
    assert binding is not None
    invoker = BuiltinDispatchingInvoker(delegate=None)
    occurrence = SimpleNamespace(local_execution=binding)
    with pytest.raises(ProviderLocalRuntimeRefused) as refused:
        invoker.invoke_provider(
            occurrence=occurrence,
            context=_context(binding, _payload(b"x" * 4000), output_bytes=1000),
            invocation_id="sha256:" + "1" * 64,
            bound=invoker.bind_provider(occurrence=occurrence),
            deadline=None,
        )
    assert refused.value.code == "budget_output_size"


def _builtin_with(adapter: Any) -> tuple[BuiltinProviderInvoker, Any, Any]:
    """The built-in invoker with its one revision's adapter replaced by ``adapter``."""

    from dataclasses import replace

    import cruxible_core.providers.builtin_runtime as runtime

    provider, interface = _accepted()
    binding = builtin_provider_binding(
        provider, interface, WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST
    )
    assert binding is not None
    original = runtime.BUILTIN_IMPLEMENTATIONS[WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST]
    swapped = {
        **runtime.BUILTIN_IMPLEMENTATIONS,
        WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST: replace(original, adapter=adapter),
    }
    return BuiltinProviderInvoker(), binding, swapped


def _invoke_builtin(
    monkeypatch: pytest.MonkeyPatch, adapter: Any, *, context_update: dict[str, Any] | None = None
) -> Any:
    import cruxible_core.providers.builtin_runtime as runtime

    invoker, binding, swapped = _builtin_with(adapter)
    monkeypatch.setattr(runtime, "BUILTIN_IMPLEMENTATIONS", swapped)
    occurrence = SimpleNamespace(local_execution=binding)
    context = _context(binding, _payload(b"high\n"))
    if context_update:
        context = context.model_copy(update=context_update)
    return invoker.invoke_provider(
        occurrence=occurrence,  # type: ignore[arg-type]
        context=context,
        invocation_id="sha256:" + "1" * 64,
        bound=invoker.bind_provider(occurrence=occurrence),  # type: ignore[arg-type]
        deadline=None,
    )


class _ReturnsRuntimeCode:
    operations = ("workspace.file",)

    def __call__(self, context: Any) -> Any:
        from cruxible_core.providers.provider_runtime_contract import (
            ProviderRuntimeRefusal,
            ProviderRuntimeResultEnvelope,
        )

        return ProviderRuntimeResultEnvelope(
            protocol_version="2.0",
            run_id=context.run_id,
            status="refused",
            refusal=ProviderRuntimeRefusal(code="budget_wall_clock", message="invented"),
        )


class _RaisesRuntimeCode:
    operations = ("workspace.file",)

    def __call__(self, context: Any) -> Any:
        from cruxible_core.providers.provider_runtime_contract import ProviderRuntimeWireError

        raise ProviderRuntimeWireError("unresolved_secret_ref", "invented")


class _DeclaresOtherOperation:
    operations = ("workspace.other",)

    def __call__(self, context: Any) -> Any:
        raise AssertionError("an undeclared operation is never called")


@pytest.mark.parametrize("adapter", [_ReturnsRuntimeCode(), _RaisesRuntimeCode()])
def test_a_runtime_code_from_built_in_adapter_code_is_a_protocol_violation(
    monkeypatch: pytest.MonkeyPatch, adapter: Any
) -> None:
    """The child's provider-code guard, applied in-process: adapter code is provider code."""

    outcome = _invoke_builtin(monkeypatch, adapter)
    refusal = outcome.envelope.refusal
    assert refusal is not None and refusal.code == "provider_protocol_violation"
    assert refusal.detail["stage"] == "call"
    assert refusal.detail["refused_code"] in {"budget_wall_clock", "unresolved_secret_ref"}


def test_a_built_in_that_does_not_declare_the_operation_refuses_undeclared_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _invoke_builtin(monkeypatch, _DeclaresOtherOperation())
    refusal = outcome.envelope.refusal
    assert refusal is not None and refusal.code == "undeclared_operation"


@pytest.mark.parametrize(
    ("update", "field"),
    [
        ({"operation": "workspace.other"}, "operation"),
        ({"operation_signature_digest": "sha256:" + "9" * 64}, "operation signature"),
        ({"build_digest": "sha256:" + "9" * 64}, "build"),
        ({"entrypoint": "elsewhere:Other"}, "entrypoint"),
        ({"implementation_digest": "sha256:" + "9" * 64}, "implementation"),
    ],
)
def test_a_built_in_runs_only_the_exact_admitted_binding(
    monkeypatch: pytest.MonkeyPatch, update: dict[str, Any], field: str
) -> None:
    with pytest.raises(ProviderLocalRuntimeRefused) as refused:
        _invoke_builtin(monkeypatch, _DeclaresOtherOperation(), context_update=update)
    assert refused.value.code == "acceptance_divergence"
    assert refused.value.details == {"field": field}


class _Crash(BaseException):
    """The daemon dying between the durable start and the completion."""


class _CrashingBuiltin(BuiltinProviderInvoker):
    def invoke_provider(self, **_kwargs: Any) -> Any:  # type: ignore[override]
        raise _Crash


class _BuiltinOperator:
    """The real admission and dispatch, with a built-in that crashes mid-call."""

    def invoker_for(
        self, instance: object, *, accepted_oid: str, pins: object = ()
    ) -> BuiltinDispatchingInvoker:
        return BuiltinDispatchingInvoker(delegate=None, builtin=_CrashingBuiltin())

    def admit_line_provider(
        self,
        accepted_provider: Any,
        accepted_interface: Any,
        implementation_digest: str,
        *,
        eligible_environment_pin_keys: tuple[str, ...],
    ) -> Any:
        return builtin_provider_binding(
            accepted_provider, accepted_interface, implementation_digest
        )


def test_startup_closes_a_built_in_start_a_crash_left_open(tmp_path: Path) -> None:
    instance, _owner, _procedure, root, _policy = _world(tmp_path)
    with pytest.raises(_Crash):
        service_run_playbill_procedure(
            instance,
            name=PROCEDURE_NAME,
            request=ProcedureRunRequest(input={}, evaluation_time=NOW),
            actor_context=_actor(instance).model_copy(update={"timestamp": NOW}),
            provider_runtime_operator=_BuiltinOperator(),  # type: ignore[arg-type]
            workspace_file_reader=_reader(instance, root),
        )

    import cruxible_core.service.procedures.procedure_runs as service

    def events() -> list[tuple[str, dict[str, Any]]]:
        journal, _root = service._journal(instance)  # noqa: SLF001
        stream = service._stream(instance)  # noqa: SLF001
        access = BodyAccessContext(principal_id="test", can_read_body=True)
        found = []
        for partition_id in journal.partition_ids(stream):
            for stored in journal.all_records(stream, partition_id):
                payload = parse_journal_payload(
                    instance.body_store().read(stored.record.payload_digest, access=access)
                )
                found.append((stored.record.event_kind, payload))
        return found

    kinds = [kind for kind, _payload in events()]
    assert kinds.count("provider_invocation_started") == 1
    assert "provider_invocation_completed" not in kinds

    # The lease fold alone never names it: no lease was ever taken.
    assert service_recover_provider_invocations(instance, invocation_ids=(), recorded_at=NOW) == ()
    closed = service_recover_provider_invocations(
        instance,
        invocation_ids=(),
        recorded_at=datetime(2026, 9, 12, 12, 5, tzinfo=UTC),
        close_in_process_starts=True,
    )
    assert len(closed) == 1
    completed = [payload for kind, payload in events() if kind == "provider_invocation_completed"]
    (completion,) = completed
    receipt = completion["receipt"]
    assert receipt["invocation_id"] == closed[0]
    assert receipt["fence_scope"] == "in_process"
    assert receipt["egress"]["observer_backend"] == "core.in-process"
    assert receipt["outcome"]["code"] == "provider_in_process_interrupted"
    kind, finalized = events()[-1]
    assert kind == "attempt_finalized"
    assert finalized["failure"] == (
        "Built-in Provider invocation was interrupted and closed at daemon startup."
    )
    assert finalized["failure_details"] == {
        "provider_refusal_code": "provider_in_process_interrupted"
    }
    # A second scan finds nothing left to close.
    assert (
        service_recover_provider_invocations(
            instance, invocation_ids=(), recorded_at=NOW, close_in_process_starts=True
        )
        == ()
    )
