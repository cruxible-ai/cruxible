from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cruxible_core.playbill.acquisition_policies import (
    AcquisitionCandidateV1,
    BoundedWindowCoherenceV1,
    DeclaredSnapshotGroupCoherenceV1,
    IndependentCoherenceV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
    acquisition_policy_path,
    evaluate_acquisition_policy_law,
    select_sources,
)
from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.capture_journal import (
    InMemoryCaptureLandingJournal,
    capture_landing_idempotency_key,
)
from cruxible_core.playbill.captures import (
    CanonicalDurationV1,
    build_cas_capture,
    capture_component_pin,
)
from tests.test_playbill._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)


def _rule(
    name: str,
    *,
    requirement: str = "required",
    unavailable: str = "refuse",
) -> InputAcquisitionRuleV1:
    return InputAcquisitionRuleV1(
        input_name=name,
        requirement=requirement,  # type: ignore[arg-type]
        permitted_replayability=("attested_only", "exact"),
        max_age=CanonicalDurationV1(microseconds=60_000_000),
        on_unavailable=unavailable,  # type: ignore[arg-type]
        on_stale=unavailable,  # type: ignore[arg-type]
        on_oversized=unavailable,  # type: ignore[arg-type]
        on_conflict="preserve",
        conservative_default=(False if requirement == "conservative_default" else None),
    )


def _policy(*rules: InputAcquisitionRuleV1, bounded_seconds: int | None = None):
    coherence = (
        IndependentCoherenceV1()
        if bounded_seconds is None
        else BoundedWindowCoherenceV1(
            max_cross_source_skew=CanonicalDurationV1(microseconds=bounded_seconds * 1_000_000)
        )
    )
    return SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="order-release"),
        inputs=tuple(sorted(rules, key=lambda item: item.input_name)),
        coherence=coherence,
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
    )


def _candidate(
    tmp_path: Path,
    *,
    name: str,
    observed_offset: int = 0,
    current_replay_available: bool = True,
) -> AcquisitionCandidateV1:
    contract = capture_contract(name=f"test.{name}-v1")
    provider_artifact = provider(contract, name=f"provider.{name}")
    result = build_cas_capture(
        store=body_store(tmp_path / name),
        contract=contract,
        source_body=(f'{{"input":"{name}"}}').encode(),
        run_coordinate=provider_run(provider_artifact, run_id=f"run-{name}"),
        run_receipt_digest=digest("receipt", name),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("binding", name),
        observed_at=NOW + timedelta(seconds=observed_offset),
    )
    journal = InMemoryCaptureLandingJournal()
    key = capture_landing_idempotency_key(
        instance_id="inst-acquisition",
        envelope=result.envelope,
    )
    event = journal.append(
        instance_id="inst-acquisition",
        envelope=result.envelope,
        landed_at=NOW + timedelta(seconds=observed_offset + 1),
        idempotency_key=key,
    )
    return AcquisitionCandidateV1(
        input_name=name,
        envelope=result.envelope,
        capture_digest=result.capture_digest,
        landing_event=event,
        current_replay_available=current_replay_available,
        selection_budget=contract.selection_budget,
        selected_bytes=16,
        selected_rows=1,
        selected_items=1,
    )


def test_required_optional_and_guarded_default_decisions_are_explicit(tmp_path: Path) -> None:
    orders = _candidate(tmp_path, name="orders")
    policy = _policy(
        _rule("orders"),
        _rule("risk", requirement="optional", unavailable="omit_optional"),
        _rule(
            "sanctions",
            requirement="conservative_default",
            unavailable="declared_conservative_default",
        ),
    )
    without_guard = select_sources(
        policy,
        (orders,),
        anchor=orders.landing_event,
        evaluation_time=NOW + timedelta(seconds=2),
    )
    assert without_guard.verdict == "refused"
    assert {item.input_name: item.disposition for item in without_guard.decisions} == {
        "orders": "selected",
        "risk": "omitted",
        "sanctions": "refused",
    }
    guarded = select_sources(
        policy,
        (orders,),
        anchor=orders.landing_event,
        evaluation_time=NOW + timedelta(seconds=2),
        default_authorizations=("sanctions",),
    )
    assert guarded.verdict == "selected"
    assert {item.input_name: item.disposition for item in guarded.decisions}[
        "sanctions"
    ] == "defaulted"


def test_bounded_window_and_current_replay_are_reverified(tmp_path: Path) -> None:
    orders = _candidate(tmp_path, name="orders")
    risk = _candidate(
        tmp_path,
        name="risk",
        observed_offset=30,
        current_replay_available=False,
    )
    policy = _policy(_rule("orders"), _rule("risk"), bounded_seconds=5)
    replay_refusal = select_sources(
        policy,
        (orders, risk),
        anchor=orders.landing_event,
        evaluation_time=NOW + timedelta(seconds=31),
    )
    assert replay_refusal.verdict == "refused"
    assert "playbill.acquisition.replay_unavailable" in {
        reason for item in replay_refusal.decisions for reason in item.reason_codes
    }
    replayable_risk = risk.model_copy(update={"current_replay_available": True})
    skew_refusal = select_sources(
        policy,
        (orders, replayable_risk),
        anchor=orders.landing_event,
        evaluation_time=NOW + timedelta(seconds=31),
    )
    assert skew_refusal.verdict == "refused"
    assert "playbill.acquisition.cross_source_skew" in {
        reason for item in skew_refusal.decisions for reason in item.reason_codes
    }


def test_declared_snapshot_requires_registered_components_and_exact_group(tmp_path: Path) -> None:
    grammar = capture_component_pin(
        "coordinate-grammar", "playbill.database-snapshot-coordinate-v1"
    )
    proof = capture_component_pin("proof-adapter", "playbill.database-snapshot-proof-v1")
    policy = SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="snapshot-release"),
        inputs=(_rule("orders"), _rule("risk")),
        coherence=DeclaredSnapshotGroupCoherenceV1(
            coordinate_grammar_digest=grammar.artifact_digest,
            proof_adapter_digest=proof.artifact_digest,
        ),
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        pins=tuple(sorted((grammar, proof), key=lambda item: (item.role, item.target.qualified))),
    )
    accepted = evaluate_acquisition_policy_law(
        policy,
        path=acquisition_policy_path(policy.identity.name),
        actor_roles=("owner",),
        predecessor=None,
    )
    assert accepted.verdict == "accepted"

    unregistered = policy.model_copy(
        update={
            "pins": tuple(
                sorted(
                    (
                        grammar,
                        ArtifactPin(
                            role="proof-adapter",
                            target=ArtifactIdentity(kind="Contract", name="unknown-proof-v1"),
                            artifact_digest=proof.artifact_digest,
                        ),
                    ),
                    key=lambda item: (item.role, item.target.qualified),
                )
            )
        }
    )
    refused = evaluate_acquisition_policy_law(
        unregistered,
        path=acquisition_policy_path(unregistered.identity.name),
        actor_roles=("owner",),
        predecessor=None,
    )
    assert refused.diagnostics[0].code == (
        "playbill.acquisition_policy.snapshot_registry_unresolved"
    )

    orders = _candidate(tmp_path, name="orders").model_copy(
        update={"snapshot_group": "snapshot-42", "snapshot_proof_digest": digest("proof", "o")}
    )
    risk = _candidate(tmp_path, name="risk").model_copy(
        update={"snapshot_group": "snapshot-42", "snapshot_proof_digest": digest("proof", "r")}
    )
    selected = select_sources(
        policy,
        (orders, risk),
        anchor=orders.landing_event,
        evaluation_time=NOW + timedelta(seconds=2),
    )
    assert selected.verdict == "selected"
    assert selected.coherence_proof_digest is not None

    mismatched = select_sources(
        policy,
        (orders, risk.model_copy(update={"snapshot_group": "snapshot-43"})),
        anchor=orders.landing_event,
        evaluation_time=NOW + timedelta(seconds=2),
    )
    assert mismatched.verdict == "refused"
    assert "playbill.acquisition.snapshot_group_unproved" in {
        reason for item in mismatched.decisions for reason in item.reason_codes
    }
