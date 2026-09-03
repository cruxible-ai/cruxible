"""Served prediction authoring and exact P2-C settlement reuse."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_playbill._knowledge_loop_support import (
    PREDICATE,
    accept_proposal,
    subject_address,
)
from tests.test_playbill.test_query_execution_service import _instance_with_query
from tests.test_playbill.test_resolution_contracts import _accept_tree, _accepted, _digest

from cruxible_client import ClaimRole, Playbill
from cruxible_client import contracts as api
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
)
from cruxible_client.contracts.claims import LiteralClaimObject, parse_claim
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidenceV1,
    PlaybillPredictRequestV1,
    PlaybillSettleRequestV1,
    PredictionEqualityRuleV1,
    PredictionObservationSelectorV1,
    PredictionPresenceRuleV1,
    PredictionThresholdRuleV1,
    TerminalSettlementEvidenceV1,
)
from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
from cruxible_core.cli.commands.playbill import playbill_group
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.playbill.procedures.egress import (
    TerminalEgressChildReceiptV1,
    TerminalEgressReceiptV1,
)
from cruxible_core.playbill.procedures.resolution import (
    ResolutionContractActivationV2,
    SettledOutcomeRelationV1,
    evaluate_prediction_correctness_condition,
)
from cruxible_core.playbill.procedures.settled_outcomes import (
    SettledOutcomesAccessProfileV1,
    SettledOutcomesQueryRequestV1,
    query_settled_outcomes,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import service_inspect_playbill_proposal
from cruxible_core.service.playbill_predictions import (
    PredictionRefused,
    _journal,
    load_prediction_activations,
    service_predict_playbill,
    service_settle_playbill_prediction,
)

PREDICTED_AT = datetime(2026, 9, 2, 12, 1, tzinfo=UTC)
OBSERVED_AT = PREDICTED_AT + timedelta(minutes=1)
RECORDED_AT = OBSERVED_AT + timedelta(minutes=1)


def _world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = _instance_with_query(tmp_path)
    accepted = _accepted()
    path = procedure_path(accepted.procedure.identity.name)
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            path: render_procedure(accepted.procedure),
        },
        timestamp="2026-09-02T12:00:00.000000Z",
        proposal_name="prediction-procedure",
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    seed_path = next(path for path in sorted(tree) if path.startswith("claims/"))
    seed = parse_claim(tree[seed_path], path=seed_path)
    return instance, owner, seed.backing.capture_digests[0]


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


def _predict(instance, capture_digest: str, *, deadline: datetime | None = None):  # type: ignore[no-untyped-def]
    return service_predict_playbill(
        instance,
        request=PlaybillPredictRequestV1(
            prediction=_payload(capture_digest, qualifier="prediction", value="ready"),
            procedure="measured-procedure",
            measurement_name="unit-health",
            observation=PredictionObservationSelectorV1(
                subject=subject_address("wi-42"),
                predicate=PREDICATE,
                qualifier="prediction-outcome",
            ),
            rule=PredictionEqualityRuleV1(),
            deadline=deadline or PREDICTED_AT + timedelta(hours=1),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
        evaluation_time=PREDICTED_AT,
    )


def _accept_prediction(instance, owner, predicted) -> None:  # type: ignore[no-untyped-def]
    accept_proposal(
        instance,
        owner,
        service_inspect_playbill_proposal(
            instance,
            proposal_id=predicted.declaration.proposal_id,
        ),
    )


def _accept_observation(instance, owner, capture_digest: str, *, value: object = "ready") -> str:  # type: ignore[no-untyped-def]
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    created = coordinator.create(
        actor=actor,
        payload=_payload(capture_digest, qualifier="prediction-outcome", value=value),
        canonical_timestamp="2026-09-02T12:02:00.000000Z",
    )
    submitted = coordinator.submit(created.intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    accept_proposal(
        instance,
        owner,
        service_inspect_playbill_proposal(
            instance,
            proposal_id=submitted.status.proposal_id,
        ),
    )
    return created.intent.semantic_identity


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
    assert evaluate_prediction_correctness_condition(
        PredictionPresenceRuleV1().model_dump(mode="json"),
        prediction_value=True,
        settlement_value=None,
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


def test_observation_settlement_replays_into_the_existing_calibration_fold(
    tmp_path: Path,
) -> None:
    instance, owner, capture_digest = _world(tmp_path)
    predicted = _predict(instance, capture_digest)
    assert predicted.intent.intent.candidate_status.state == "ready_to_activate"
    _accept_prediction(instance, owner, predicted)
    observation_id = _accept_observation(instance, owner, capture_digest)

    result = service_settle_playbill_prediction(
        instance,
        prediction_id=predicted.declaration.prediction_id,
        request=PlaybillSettleRequestV1(
            evidence=ObservationSettlementEvidenceV1(claim_id=observation_id)
        ),
        actor_context=_actor(),
        recorded_at=RECORDED_AT,
    )
    retried = service_settle_playbill_prediction(
        instance,
        prediction_id=predicted.declaration.prediction_id,
        request=PlaybillSettleRequestV1(
            evidence=ObservationSettlementEvidenceV1(claim_id=observation_id)
        ),
        actor_context=_actor("retrying-observer"),
        recorded_at=RECORDED_AT + timedelta(minutes=1),
    )

    activation = ResolutionContractActivationV2.model_validate(result.activation)
    relation = SettledOutcomeRelationV1.model_validate(result.relation)
    assert retried == result
    assert relation.activation == activation
    assert relation.resolution.settlement_outcome is True
    assert (
        relation.resolution.settlement.accepted_coordinate
        != activation.prediction.accepted_coordinate
    )
    assert load_prediction_activations(instance) == (activation,)

    journal, stream = _journal(instance)
    records = {
        partition: journal.all_records(stream, partition)
        for partition in journal.partition_ids(stream)
    }
    proof_digest = relation.resolution.evidence_refs[0].digest
    folded, _receipt = query_settled_outcomes(
        SettledOutcomesQueryRequestV1(
            accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=RECORDED_AT,
            access_profile=SettledOutcomesAccessProfileV1(
                profile_id="served-predictions",
                can_read_resolution_bodies=True,
                visible_proof_digests=(proof_digest,),
            ),
        ),
        activations=(activation,),
        records_by_partition=records,
        bodies=instance.body_store(),
    )
    assert folded.rows[0].relation == relation


def test_terminal_settlement_reuses_the_verified_terminal_actor_and_exact_record(
    tmp_path: Path,
) -> None:
    instance, owner, capture_digest = _world(tmp_path)
    predicted = _predict(instance, capture_digest)
    _accept_prediction(instance, owner, predicted)
    observation_id = _accept_observation(instance, owner, capture_digest)
    terminal_actor = _actor("terminal-operator")
    terminal_receipt = TerminalEgressReceiptV1(
        kind="mandate_settlement",
        run_id="run-prediction",
        node_id="deliver",
        disposition="settled",
        bound_artifact_digest=_digest("terminal-bound-claim-type"),
        children=(
            TerminalEgressChildReceiptV1(
                child_index=0,
                item_key="outcome",
                egress_digest=_digest("terminal-outcome"),
            ),
        ),
    )
    journal, stream = _journal(instance)
    partition = "run-prediction"
    journal.activate_writer(
        stream,
        partition,
        fencing_token="terminal-writer",
        expected_head=journal.read_head(stream, partition),
    )
    stored = ProcedureExhaustWriter(
        journal=journal,
        bodies=instance.body_store(),
        fencing_token="terminal-writer",
    ).append(
        stream=stream,
        partition_id=partition,
        event_kind="terminal_egress",
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        procedure_artifact_digest=predicted.declaration.procedure_artifact_digest,
        definition_digest=_accepted().procedure.definition_digest,
        actor_context=terminal_actor,
        recorded_at=OBSERVED_AT + timedelta(seconds=30),
        run_id="run-prediction",
        payload={
            "node_id": "deliver",
            "kind": "mandate_settlement",
            "verdict": "delivered",
            "receipt": terminal_receipt.model_dump(mode="json"),
        },
    )
    journal.fence_writer(
        stream,
        partition,
        expected_fencing_token="terminal-writer",
    )

    result = service_settle_playbill_prediction(
        instance,
        prediction_id=predicted.declaration.prediction_id,
        request=PlaybillSettleRequestV1(
            evidence=TerminalSettlementEvidenceV1(
                claim_id=observation_id,
                run_id="run-prediction",
                terminal_record_digest=stored.record_digest,
            )
        ),
        actor_context=_actor("unrelated-caller"),
        recorded_at=RECORDED_AT,
    )
    relation = SettledOutcomeRelationV1.model_validate(result.relation)
    assert relation.resolution.actor_context == terminal_actor
    assert relation.resolution.evidence_refs[0].kind == "run_receipt"
    assert relation.resolution.evidence_refs[0].digest == stored.record_digest


def test_prediction_refusals_are_typed_and_carry_runnable_repairs(tmp_path: Path) -> None:
    instance, owner, capture_digest = _world(tmp_path)
    request = PlaybillPredictRequestV1(
        prediction=_payload(capture_digest, qualifier="prediction", value=3),
        procedure="measured-procedure",
        measurement_name="unit-health",
        observation=PredictionObservationSelectorV1(
            subject=subject_address("wi-42"),
            predicate=PREDICATE,
            qualifier="prediction-outcome",
        ),
        rule=PredictionThresholdRuleV1(comparison="gte", threshold=3),
        deadline=PREDICTED_AT + timedelta(hours=1),
    )
    with pytest.raises(PredictionRefused) as rule_refusal:
        service_predict_playbill(
            instance,
            request=request,
            actor=AuthenticatedActor(actor_id="owner"),
            evaluation_time=PREDICTED_AT,
        )
    assert rule_refusal.value.error_code == "prediction_unsettleable_rule"
    assert rule_refusal.value.repair.operation == "playbill.predict"

    with pytest.raises(PredictionRefused) as deadline_refusal:
        _predict(instance, capture_digest, deadline=PREDICTED_AT)
    assert deadline_refusal.value.error_code == "prediction_deadline_passed"
    assert deadline_refusal.value.repair.operation == "playbill.predict"

    predicted = _predict(instance, capture_digest)
    _accept_prediction(instance, owner, predicted)
    with pytest.raises(PredictionRefused) as mismatch_refusal:
        service_settle_playbill_prediction(
            instance,
            prediction_id=predicted.declaration.prediction_id,
            request=PlaybillSettleRequestV1(
                evidence=ObservationSettlementEvidenceV1(
                    claim_id="CLM-" + "f" * 32,
                )
            ),
            actor_context=_actor(),
            recorded_at=RECORDED_AT,
        )
    assert mismatch_refusal.value.error_code == "settlement_evidence_mismatch"
    assert mismatch_refusal.value.repair.operation == "playbill.settle"


class _SdkClient:
    def __init__(self) -> None:
        self.predict_request: PlaybillPredictRequestV1 | None = None
        self.settle_request: PlaybillSettleRequestV1 | None = None

    def search_playbill(self, instance_id: str, **_values: object) -> SimpleNamespace:
        assert instance_id == "inst_test"
        return SimpleNamespace(
            coordinate=api.PlaybillAcceptedCoordinate(
                git_oid="a" * 40,
                semantic_root="sha256:" + "1" * 64,
                generation_root="sha256:" + "2" * 64,
                compiler_digest="sha256:" + "3" * 64,
            ),
            evaluation_time="2026-09-02T12:01:00.000000Z",
            rows=[],
            result_digest="sha256:" + "4" * 64,
            next_cursor=None,
            truncated=False,
            orientation={"state": "empty"},
        )

    def predict_playbill(
        self,
        instance_id: str,
        *,
        request: PlaybillPredictRequestV1,
    ) -> SimpleNamespace:
        assert instance_id == "inst_test"
        self.predict_request = request
        return SimpleNamespace(
            declaration=SimpleNamespace(
                prediction_id="PRD-" + "1" * 32,
                intent_id="AIT-" + "2" * 32,
                proposal_id="sha256:" + "3" * 64,
                predicted_claim_id="CLM-" + "4" * 32,
                declaration_digest="sha256:" + "5" * 64,
            )
        )

    def settle_playbill_prediction(
        self,
        instance_id: str,
        prediction_id: str,
        *,
        request: PlaybillSettleRequestV1,
    ) -> SimpleNamespace:
        assert instance_id == "inst_test"
        self.settle_request = request
        return SimpleNamespace(
            prediction_id=prediction_id,
            resolution={"settlement_outcome": True},
            relation={"tag": "playbill-settled-outcome-relation-v1"},
        )


def test_sdk_and_cli_expose_predict_and_settle(tmp_path: Path) -> None:
    (tmp_path / ".playbill").mkdir()
    (tmp_path / ".playbill" / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: fixture.source
    locator: source.txt
    document_id: source
    document_kind: fixture
    title: Fixture
    media_type: text/plain
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:source]
""",
        encoding="utf-8",
    )
    client = _SdkClient()
    playbill = Playbill._from_client(
        client,  # type: ignore[arg-type]
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: PREDICTED_AT,
    )
    draft = playbill.claim(
        subject="work_item/wi-42",
        predicate=PREDICATE,
        value="ready",
        role=ClaimRole.OBSERVATION,
        rationale="Predict the observed status.",
        supported_by=None,
        copied_from=None,
        self_source="ready",
        qualifier="prediction",
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=None,
        claim_type_definition=None,
    )
    prediction = playbill.predict(
        draft,
        procedure="measured-procedure",
        measurement_name="unit-health",
        observation_subject="work_item/wi-42",
        observation_predicate=PREDICATE,
        observation_qualifier="prediction-outcome",
        rule={"tag": "playbill-prediction-equality-rule-v1", "operator": "equality"},
        deadline=PREDICTED_AT + timedelta(hours=1),
    )
    settlement = playbill.settle(prediction, observation="CLM-" + "6" * 32)

    assert client.predict_request is not None
    prediction_object = client.predict_request.prediction.statement.object
    assert isinstance(prediction_object, LiteralClaimObject)
    assert prediction_object.value == "ready"
    assert client.settle_request == PlaybillSettleRequestV1(
        evidence=ObservationSettlementEvidenceV1(claim_id="CLM-" + "6" * 32)
    )
    assert settlement.outcome is True
    assert {"predict", "settle"}.issubset(playbill_group.commands)
