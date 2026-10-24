"""Served Source acquisition honors accepted eligibility and coherence rules."""

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.acquisition_policies import (
    AcquisitionRule,
    IndependentCoherence,
    InputAcquisitionRule,
    SourceAcquisitionPolicyV1,
    acquisition_policy_path,
    acquisition_rule,
    parse_acquisition_policy,
    render_acquisition_policy,
)
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_core.evidence.source_readers import (
    ExternalSourceReadRequestV1,
    FakeVersionedExternalSourceReader,
    ProducerBindingV1,
)
from cruxible_core.procedures.acquisition import (
    ProcedureSourceAcquisitionResultV1,
    apply_acquisition_result,
)
from tests.core_support._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)
from tests.test_procedures import test_procedure_source_runs as source


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
    rule = InputAcquisitionRule.model_validate(
        {
            "input_name": "advisory",
            "requirement": requirement,
            "permitted_replayability": ("attested_only",)
            if reason == "replayability"
            else ("exact",),
            "max_age": CanonicalDuration(microseconds=1_000_000),
            "on_unavailable": behavior,
            "on_stale": behavior,
            "on_oversized": behavior,
            "on_conflict": "preserve",
            "conservative_default": {"severity": "unknown"}
            if requirement == "conservative_default"
            else None,
        }
    )
    from cruxible_client.contracts.artifacts import ArtifactIdentity

    policy = SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="legacy"),
        inputs=(rule,),
        coherence=IndependentCoherence(),
    )
    parsed = parse_acquisition_policy(
        render_acquisition_policy(policy), path=acquisition_policy_path("legacy")
    )
    assert isinstance(parsed, SourceAcquisitionPolicyV1)
    assert parsed.inputs[0] == rule
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
        "cruxible.acquisition.unavailable"
        if reason == "replayability"
        else "cruxible.acquisition.stale",
    )


def test_capture_at_maximum_age_remains_eligible(
    acquired: ProcedureSourceAcquisitionResultV1,
) -> None:
    rule = AcquisitionRule(input_name="advisory", max_age=CanonicalDuration(microseconds=1_000_000))
    decision = apply_acquisition_result(
        rule, acquired, default_authorized=False, evaluation_time=NOW + timedelta(seconds=1)
    )
    assert decision.disposition == "selected"
    assert acquired.acquisition is not None
    assert decision.selected_capture_digests == (acquired.acquisition.capture_digest,)


@pytest.mark.parametrize("outcome", ["unavailable", "stale", "oversized", "refused"])
def test_current_policy_refuses_every_failed_fetch(outcome: str) -> None:
    rule = acquisition_rule(source._policy(), "advisory")
    assert rule is not None
    result = ProcedureSourceAcquisitionResultV1.model_validate(
        {
            "node_id": "source",
            "input_name": "advisory",
            "outcome": outcome,
            "reason_code": "cruxible.acquisition." + outcome,
        }
    )
    assert apply_acquisition_result(rule, result, evaluation_time=NOW).disposition == "refused"


def test_selection_does_not_restate_contract_replayability(
    acquired: ProcedureSourceAcquisitionResultV1,
) -> None:
    decision = apply_acquisition_result(
        AcquisitionRule(input_name="advisory"), acquired, evaluation_time=NOW
    )
    assert decision.disposition == "selected"


def test_stale_input_refusal_names_alias_age_and_repair(tmp_path: Path) -> None:
    instance, _, _, root, _ = source._world(tmp_path, policy=source._policy())
    state, invoker = source._run(instance, root, evaluation_time=source.NOW + timedelta(days=365))
    assert invoker.spawn_calls == 1
    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "cruxible.acquisition.stale"
    assert "advisory" in state.terminal.message
    assert "age" in state.terminal.message
    assert "Repair:" in state.terminal.message
    assert state.result is None


def test_line_without_named_policy_runs_with_the_instance_default(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_path,
        render_line_spec,
    )

    instance, owner, procedure, root, policy = source._world(tmp_path)
    line = source._served_line(procedure, policy)
    line = line.model_copy(
        update={
            "acquisition_policy": None,
            "pins": tuple(pin for pin in line.pins if pin.target.kind != "SourceAcquisitionPolicy"),
        }
    )
    source._accept_more(
        instance,
        owner,
        {line_spec_path(line.identity.name): render_line_spec(line)},
        name="inherited-line",
    )
    state, invoker = source._run_line(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert invoker.spawn_calls == 1
    assert (
        source._admission(instance, state).acquisition_policy_digest
        == source.acquisition_policy_digest(policy).tagged
    )
