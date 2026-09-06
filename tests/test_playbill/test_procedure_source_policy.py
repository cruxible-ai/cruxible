"""Served Source acquisition honors accepted eligibility and coherence rules."""

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.acquisition_policies import (
    BoundedWindowCoherenceV1,
    InputAcquisitionRuleV1,
    acquisition_policy_digest,
)
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_core.playbill.procedures.acquisition import (
    ProcedureSourceAcquisitionResultV1,
    apply_acquisition_result,
)
from cruxible_core.playbill.source_readers import (
    ExternalSourceReadRequestV1,
    FakeVersionedExternalSourceReader,
    ProducerBindingV1,
)
from cruxible_core.service import playbill_procedure_runs as runs
from tests.test_playbill import test_procedure_source_runs as source
from tests.test_playbill._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)


@pytest.mark.parametrize("lane", ["direct", "line"])
def test_source_refuses_capture_excluded_by_accepted_replayability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    original_request = source._source_request
    monkeypatch.setattr(
        source,
        "_source_request",
        lambda *args: original_request(*args).model_copy(update={"replayability": "attested_only"}),
    )
    policy = source._policy()
    policy = policy.model_copy(
        update={
            "inputs": (policy.inputs[0].model_copy(update={"permitted_replayability": ("exact",)}),)
        }
    )
    if lane == "direct":
        instance, _, _, root, _ = source._world(tmp_path, policy=policy, pin_policy=True)
        state, invoker = source._run(instance, root)
    else:
        instance, root, line = source._line_world(tmp_path, policy=policy, pin_policy=True)
        state, invoker = source._run_line(instance, root, line)
    assert invoker.spawn_calls == 1  # The real result is graded after observation.
    assert state.status == "node_refused", state.terminal
    assert state.terminal is not None
    assert state.terminal.code == "playbill.acquisition.unavailable"
    assert state.result is None
    assert state.source_observations[0].source_read_receipt is not None
    assert state.source_observations[0].capture_digest is None
    retained = source.service_get_playbill_procedure_run(instance, run_id=state.run_id or "")
    assert retained.status == state.status
    assert retained.source_observations == state.source_observations


@pytest.mark.parametrize("lane", ["direct", "line"])
def test_source_refuses_unsupported_coherence_before_read_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    policy = source._policy().model_copy(
        update={
            "coherence": BoundedWindowCoherenceV1(
                max_cross_source_skew=CanonicalDurationV1(microseconds=1_000_000)
            )
        }
    )
    invoker = source._WorkspaceInvoker()
    if lane == "direct":
        instance, _, _, root, _ = source._world(tmp_path, policy=policy, pin_policy=True)
    else:
        instance, root, line = source._line_world(tmp_path, policy=policy, pin_policy=True)

    def unexpected_read(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("unsupported coherence must refuse before reading")

    monkeypatch.setattr(source.WorkspaceFileReader, "read", unexpected_read)
    if lane == "direct":
        state, _ = source._run(instance, root, invoker=invoker)
    else:
        state, _ = source._run_line(instance, root, line, invoker=invoker)
    assert invoker.spawn_calls == 0
    assert state.status == "admission_refused"
    assert state.run_id is None
    assert state.terminal is not None
    assert state.terminal.code == "source_acquisition_refused"
    assert "playbill.acquisition.coherence_unsupported" in str(state.terminal.details)
    if lane == "direct":
        assert not (instance.root / instance.descriptor.storage.exhaust / "procedure-runs").exists()
    else:
        journal, _ = runs._journal(instance)
        assert (
            journal.all_records(
                runs.procedure_line_journal_stream(instance.descriptor.instance_id),
                runs.procedure_line_partition(line.identity),
            )
            == ()
        )


@pytest.fixture
def acquired(tmp_path: Path) -> ProcedureSourceAcquisitionResultV1:
    contract = capture_contract()
    adapter = provider(contract)
    binding = ProducerBindingV1(
        provider=adapter.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("adapter", "source-policy"),
    )
    reader = FakeVersionedExternalSourceReader()
    reader.seed(
        source_identity=binding.logical_source_identity,
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "1"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
        value={"order_id": 7},
    )
    result = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=adapter,
            binding=binding,
            coordinate_type="postgres-lsn-v1",
            coordinate={"lsn": "1"},
            selector_type="relation-primary-key-v1",
            selector={"id": 7, "relation": "orders"},
            materialization="cas",
            run_coordinate=provider_run(adapter),
            observed_at=NOW,
            resource_budget=contract.selection_budget,
        ),
        store=body_store(tmp_path),
    )
    return ProcedureSourceAcquisitionResultV1(
        node_id="source", input_name="advisory", outcome="acquired", acquisition=result
    )


@pytest.mark.parametrize("reason", ["replayability", "age"])
@pytest.mark.parametrize(
    ("requirement", "behavior", "authorized", "expected"),
    [
        ("required", "refuse", False, "refused"),
        ("optional", "omit_optional", False, "omitted"),
        ("conservative_default", "declared_conservative_default", False, "refused"),
        ("conservative_default", "declared_conservative_default", True, "defaulted"),
    ],
)
def test_acquired_eligibility_applies_declared_failure_and_default_authority(
    acquired: ProcedureSourceAcquisitionResultV1,
    reason: str,
    requirement: str,
    behavior: str,
    authorized: bool,
    expected: str,
) -> None:
    rule = InputAcquisitionRuleV1.model_validate(
        {
            "input_name": "advisory",
            "requirement": requirement,
            "permitted_replayability": ("attested_only",)
            if reason == "replayability"
            else ("exact",),
            "max_age": CanonicalDurationV1(microseconds=1_000_000),
            "on_unavailable": behavior,
            "on_stale": behavior,
            "on_oversized": behavior,
            "on_conflict": "preserve",
            "conservative_default": {"severity": "unknown"}
            if requirement == "conservative_default"
            else None,
        }
    )
    decision = apply_acquisition_result(
        rule,
        acquired,
        default_authorized=authorized,
        evaluation_time=NOW + timedelta(seconds=2),
    )
    assert decision.disposition == expected
    assert decision.selected_capture_digests == ()
    assert acquired.acquisition is not None
    assert decision.considered_capture_digests == (acquired.acquisition.capture_digest,)
    assert decision.reason_codes == (
        "playbill.acquisition.unavailable"
        if reason == "replayability"
        else "playbill.acquisition.stale",
    )


def test_nonindependent_policy_does_not_refuse_source_free_plan() -> None:
    policy = source._policy().model_copy(
        update={
            "coherence": BoundedWindowCoherenceV1(
                max_cross_source_skew=CanonicalDurationV1(microseconds=1)
            )
        }
    )
    selection = runs._plan_selection_decision(
        policy,
        policy_digest=acquisition_policy_digest(policy).tagged,
        occurrences=(),
        capture_contracts={},
    )
    assert selection.verdict == "selected"
    assert selection.decisions == ()


def test_capture_at_maximum_age_remains_eligible(
    acquired: ProcedureSourceAcquisitionResultV1,
) -> None:
    rule = (
        source._policy()
        .inputs[0]
        .model_copy(update={"max_age": CanonicalDurationV1(microseconds=1_000_000)})
    )
    decision = apply_acquisition_result(
        rule, acquired, default_authorized=False, evaluation_time=NOW + timedelta(seconds=1)
    )
    assert decision.disposition == "selected"
    assert acquired.acquisition is not None
    assert decision.selected_capture_digests == (acquired.acquisition.capture_digest,)
