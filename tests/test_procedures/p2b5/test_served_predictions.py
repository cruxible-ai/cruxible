"""Served prediction authoring and exact P2-C settlement reuse."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidenceV2,
    PlaybillPredictRequestV2,
    PlaybillSettleRequestV2,
    PredictionEqualityRuleV1,
    PredictionObservationSelectorV1,
    PredictionPresenceRuleV1,
    PredictionThresholdRuleV1,
    TerminalSettlementEvidenceV2,
)
from cruxible_client.contracts.procedures.windows import FixedWindowV1
from cruxible_client.contracts.resolution_contracts import (
    ClaimVersionReferenceV1,
    ResolutionContractReferenceV1,
    ResolutionContractV1,
    resolution_contract_digest,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.procedures.resolution import (
    ResolutionContractActivationV3,
    SettledOutcomeRelationV2,
    evaluate_prediction_correctness_condition,
)
from cruxible_core.procedures.settled_outcomes import (
    SettledOutcomesAccessProfileV1,
    SettledOutcomesQueryRequestV1,
    query_settled_outcomes,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from cruxible_core.service.procedures.predictions import (
    PredictionRefused,
    _journal,
    load_prediction_activations,
    service_predict_playbill,
    service_settle_playbill_prediction,
)
from tests.core_support._knowledge_loop_support import (
    PREDICATE,
    accept_proposal,
    subject_address,
)
from tests.test_claims.test_claims import _claim_type as _work_item_claim_type
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_query.test_query_execution_service import _instance_with_query

PREDICTED_AT = datetime(2026, 9, 2, 12, 1, tzinfo=UTC)
OBSERVED_AT = PREDICTED_AT + timedelta(minutes=1)
RECORDED_AT = OBSERVED_AT + timedelta(minutes=1)


def _world(tmp_path: Path):
    instance, owner = _instance_with_query(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    seed_path = next(path for path in sorted(tree) if path.startswith("claims/"))
    seed = parse_claim(tree[seed_path], path=seed_path)
    return instance, owner, seed.backing.capture_digests[0]


def _reference(instance, claim_id):
    at = instance.accepted_coordinate()
    path = claim_path(claim_id.removeprefix("Claim:"))
    claim = parse_claim(instance.blob_at(at.git_oid, path), path=path)
    return ClaimVersionReferenceV1(
        identity=claim.identity,
        artifact_digest=claim_artifact_digest(claim).tagged,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        coordinate=AcceptedCoordinate.from_internal(at),
    )


def _accept_payload(instance, owner, payload, timestamp):
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    created = coordinator.create(actor=actor, payload=payload, canonical_timestamp=timestamp)
    submitted = coordinator.submit(created.intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None, submitted.status
    accept_proposal(
        instance,
        owner,
        service_inspect_playbill_proposal(instance, proposal_id=submitted.status.proposal_id),
    )
    return _reference(instance, created.intent.semantic_identity)


def _payload(capture_digest: str, *, qualifier: str, value: object) -> ClaimAuthoringPayloadV3:
    return ClaimAuthoringPayloadV3(
        statement=AuthoringClaimStatementV1(
            subject=subject_address("wi-42"),
            predicate=PREDICATE,
            qualifier=qualifier,
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        rationale=f"Record {qualifier} for the served prediction test.",
        source=ExistingCaptureCitationSourceV1(capture_digest=capture_digest),
        citation_role="copy",
        dependency_drafts=ClaimDependencyDraftsV1(),
    )


def _predict(instance, owner, capture_digest, *, value="ready", presence=False):
    maker = _presence_payload if presence else _payload
    h = _accept_payload(
        instance,
        owner,
        maker(capture_digest, qualifier="prediction", value=value),
        "2026-09-02T12:00:45.000000Z",
    )
    contract = ResolutionContractV1(
        identity=ArtifactIdentity(kind="ResolutionContract", name="status-test"),
        hypothesis=h,
        observation=PredictionObservationSelectorV1(
            subject=subject_address("wi-42"),
            predicate=PRESENCE_PREDICATE if presence else PREDICATE,
            qualifier="prediction-outcome",
        ),
        rule=PredictionPresenceRuleV1() if presence else PredictionEqualityRuleV1(),
        window=FixedWindowV1(starts_at=PREDICTED_AT, duration_seconds=3600),
    )
    proposed = service_predict_playbill(
        instance,
        request=PlaybillPredictRequestV2(contract=contract),
        actor=AuthenticatedActor(actor_id="owner"),
        evaluation_time=PREDICTED_AT,
    )
    accept_proposal(
        instance,
        owner,
        service_inspect_playbill_proposal(instance, proposal_id=proposed.proposal_id),
    )
    return ResolutionContractReferenceV1(
        identity=contract.identity,
        artifact_digest=resolution_contract_digest(contract).tagged,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )


def _settle(instance, contract, observation, *, at=RECORDED_AT):
    return service_settle_playbill_prediction(
        instance,
        prediction_id=contract.identity.name,
        request=PlaybillSettleRequestV2(
            contract=contract, evidence=ObservationSettlementEvidenceV2(claim=observation)
        ),
        actor_context=_actor(),
        recorded_at=at,
    )


def _actor(actor_id: str = "owner") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="inst_playbill_test",
        operation_id=f"settle-{actor_id}",
        timestamp=RECORDED_AT,
    )


def test_prediction_rules_are_closed_and_mechanical() -> None:
    assert evaluate_prediction_correctness_condition(
        PredictionEqualityRuleV1().model_dump(mode="json"),
        prediction_value={"expected": 3},
        settlement_value={"expected": 3},
        evidence_present=True,
    )
    threshold = PredictionThresholdRuleV1(comparison="gte", threshold=3)
    assert evaluate_prediction_correctness_condition(
        threshold.model_dump(mode="json"),
        prediction_value=True,
        settlement_value=3,
        evidence_present=True,
    )
    assert evaluate_prediction_correctness_condition(
        threshold.model_dump(mode="json"),
        prediction_value=False,
        settlement_value=2,
        evidence_present=True,
    )
    presence = PredictionPresenceRuleV1().model_dump(mode="json")
    assert evaluate_prediction_correctness_condition(
        presence,
        prediction_value=True,
        settlement_value=None,
        evidence_present=True,
    )
    # A prediction of absence is correct exactly when the evidence is absent.
    assert evaluate_prediction_correctness_condition(
        presence,
        prediction_value=False,
        settlement_value=None,
        evidence_present=False,
    )
    assert not evaluate_prediction_correctness_condition(
        presence,
        prediction_value=False,
        settlement_value="ready",
        evidence_present=True,
    )
    assert (
        evaluate_prediction_correctness_condition(
            {"operator": "invented"},
            prediction_value=True,
            settlement_value=True,
            evidence_present=True,
        )
        is None
    )


def test_observation_settlement_replays_into_existing_fold_and_survives_change(tmp_path: Path):
    instance, owner, capture = _world(tmp_path)
    contract = _predict(instance, owner, capture)
    observation = _accept_payload(
        instance,
        owner,
        _payload(capture, qualifier="prediction-outcome", value="ready"),
        "2026-09-02T12:02:00.000000Z",
    )
    result = _settle(instance, contract, observation)
    assert _settle(instance, contract, observation, at=RECORDED_AT + timedelta(days=1)) == result
    activation = ResolutionContractActivationV3.model_validate(result.activation)
    relation = SettledOutcomeRelationV2.model_validate(result.relation)
    assert relation.resolution.settlement_outcome is True
    assert activation.procedure_artifact_digest is None
    assert load_prediction_activations(instance) == (activation,)
    journal, stream = _journal(instance)
    records = {p: journal.all_records(stream, p) for p in journal.partition_ids(stream)}
    folded, _ = query_settled_outcomes(
        SettledOutcomesQueryRequestV1(
            accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=RECORDED_AT,
            access_profile=SettledOutcomesAccessProfileV1(
                profile_id="test",
                can_read_resolution_bodies=True,
                visible_proof_digests=(relation.resolution.evidence_refs[0].digest,),
            ),
        ),
        activations=(activation,),
        records_by_partition=records,
        bodies=instance.body_store(),
    )
    assert folded.rows[0].relation == relation
    assert not (instance.root / instance.descriptor.storage.exhaust / "predictions").exists()
    # A later proposal cannot reinterpret the already retained settlement.
    _accept_payload(
        instance,
        owner,
        _payload(capture, qualifier="other", value="blocked"),
        "2026-09-02T12:04:00.000000Z",
    )
    assert _settle(instance, contract, observation, at=RECORDED_AT + timedelta(days=2)) == result
    later_reference = contract.model_copy(
        update={"coordinate": AcceptedCoordinate.from_internal(instance.accepted_coordinate())}
    )
    assert (
        _settle(instance, later_reference, observation, at=RECORDED_AT + timedelta(days=2))
        == result
    )
    assert load_prediction_activations(instance) == (activation,)


PRESENCE_PREDICATE = "project.work_item.presence"


def _presence_claim_type() -> ClaimType:
    """A ClaimType whose literal may be a boolean or an explicit absence."""

    base = _work_item_claim_type()
    return base.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=PRESENCE_PREDICATE),
            "predicate": PRESENCE_PREDICATE,
            "literal_schema": {"enum": [False, True, None]},
        }
    )


def _accept_presence_claim_type(instance, owner) -> None:  # type: ignore[no-untyped-def]
    claim_type = _presence_claim_type()
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            claim_type_path(PRESENCE_PREDICATE): render_claim_type(claim_type),
        },
        timestamp="2026-09-02T12:00:30.000000Z",
        proposal_name="presence-claim-type",
    )


def _presence_payload(
    capture_digest: str, *, qualifier: str, value: object
) -> ClaimAuthoringPayloadV3:
    return ClaimAuthoringPayloadV3(
        statement=AuthoringClaimStatementV1(
            subject=subject_address("wi-42"),
            predicate=PRESENCE_PREDICATE,
            qualifier=qualifier,
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        rationale=f"Record {qualifier} for the served presence prediction test.",
        source=ExistingCaptureCitationSourceV1(capture_digest=capture_digest),
        citation_role="copy",
        dependency_drafts=ClaimDependencyDraftsV1(),
    )


@pytest.mark.parametrize(
    ("predicted", "observed", "expected"),
    [(True, True, True), (False, True, False), (True, None, False), (False, None, True)],
)
def test_presence_settles_both_directions(tmp_path: Path, predicted, observed, expected):
    instance, owner, capture = _world(tmp_path)
    _accept_presence_claim_type(instance, owner)
    contract = _predict(instance, owner, capture, value=predicted, presence=True)
    observation = _accept_payload(
        instance,
        owner,
        _presence_payload(capture, qualifier="prediction-outcome", value=observed),
        "2026-09-02T12:02:00.000000Z",
    )
    relation = SettledOutcomeRelationV2.model_validate(
        _settle(instance, contract, observation).relation
    )
    assert relation.resolution.settlement_outcome is expected
    assert relation.resolution.value["evidence_present"] is (observed is not None)


def test_old_observation_cannot_be_relabelled_with_later_snapshot(tmp_path: Path):
    instance, owner, capture = _world(tmp_path)
    observation = _accept_payload(
        instance,
        owner,
        _payload(capture, qualifier="prediction-outcome", value="ready"),
        "2026-09-02T12:00:00.000000Z",
    )
    contract = _predict(instance, owner, capture)
    later = observation.model_copy(
        update={"coordinate": AcceptedCoordinate.from_internal(instance.accepted_coordinate())}
    )
    with pytest.raises(PredictionRefused, match="window"):
        _settle(instance, contract, later)


def test_terminal_cannot_settle_another_investigation(tmp_path: Path, monkeypatch):
    from cruxible_core.service.procedures import procedure_runs
    from cruxible_core.service.procedures.predictions import _terminal_record
    from cruxible_core.service.procedures.resolution_contracts import bind_investigation

    instance, owner, capture = _world(tmp_path)
    contract = _predict(instance, owner, capture)
    binding = bind_investigation(instance, contract, event=None, now=RECORDED_AT)
    monkeypatch.setattr(
        procedure_runs, "_state_from_records", lambda *_a, **_k: SimpleNamespace(investigation=None)
    )
    with pytest.raises(PredictionRefused, match="different contract"):
        _terminal_record(
            instance,
            evidence=TerminalSettlementEvidenceV2(
                claim=binding.hypothesis,
                run_id="RUN-other",
                terminal_record_digest="sha256:" + "a" * 64,
            ),
            investigation=binding,
        )
