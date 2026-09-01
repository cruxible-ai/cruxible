"""ResolutionContract v2 relation and settled-outcome fold laws."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
)
from cruxible_core.playbill.procedures.resolution import (
    ProcedureProofReferenceV1,
    ProcedureResolutionBook,
    ResolutionClaimEndpointV1,
    ResolutionContractActivationV2,
    append_procedure_resolution,
    append_resolution_disposition,
    build_procedure_resolution,
    build_procedure_resolution_v2,
    build_resolution_contract_activation_v2,
    build_resolution_disposition,
    build_settled_outcome_relation,
    derive_resolution_activations,
    evaluate_procedure_resolution,
    resolution_contract_partition_id,
)
from cruxible_core.playbill.procedures.settled_outcomes import (
    SettledOutcomesAccessProfileV1,
    SettledOutcomesQueryRequestV1,
    classify_settled_outcome_history,
    query_settled_outcomes,
)
from tests.test_playbill.test_resolution_contracts import (
    NOW,
    _accepted,
    _actor,
    _coordinate,
    _digest,
)


def _activation_v2(label: str, *, index: int = 2) -> ResolutionContractActivationV2:
    base = derive_resolution_activations(
        _accepted(),
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[index]
    return build_resolution_contract_activation_v2(
        base,
        prediction=ResolutionClaimEndpointV1(
            statement_address=SemanticAddress.claim_statement(f"claims/{label}.json"),
            content_digest=_digest(f"prediction-{label}"),
            accepted_coordinate=_coordinate(),
        ),
        outcome_class="boolean-correctness",
        correctness_condition={"operator": "equals", "expected": True},
    )


def _resolution_v2(
    activation: ResolutionContractActivationV2,
    label: str,
    *,
    outcome: bool,
):
    proof = ProcedureProofReferenceV1(
        kind="query_receipt",
        digest=_digest(f"proof-{label}"),
    )
    return build_procedure_resolution_v2(
        activation,
        sequence=1,
        verdict="satisfied",
        settlement=ResolutionClaimEndpointV1(
            statement_address=SemanticAddress.claim_statement(f"claims/{label}-settled.json"),
            content_digest=_digest(f"settlement-{label}"),
            accepted_coordinate=_coordinate(f"settlement-{label}"),
        ),
        settlement_outcome=outcome,
        value={"count": 1},
        evidence_refs=(proof,),
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=3),
        actor_context=_actor(),
    )


def _store(tmp_path, activations):
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    journal = LocalJournalBackend(journal_root)
    bodies = ContentAddressedBodyStore(cas_root)
    stream = JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    for activation in activations:
        partition_id = resolution_contract_partition_id(activation)
        journal.activate_writer(
            stream,
            partition_id,
            fencing_token="writer",
            expected_head=journal.read_head(stream, partition_id),
        )
    writer = ProcedureExhaustWriter(
        journal=journal,
        bodies=bodies,
        fencing_token="writer",
    )
    return journal, bodies, stream, writer


def test_v2_pair_commits_endpoints_condition_outcome_and_proof() -> None:
    activation = _activation_v2("forecast")
    resolution = _resolution_v2(activation, "forecast", outcome=True)
    relation = build_settled_outcome_relation(activation, resolution)

    changed_activation = build_resolution_contract_activation_v2(
        derive_resolution_activations(
            _accepted(),
            accepted_coordinate=_coordinate(),
            activated_at=NOW,
        )[2],
        prediction=activation.prediction,
        outcome_class=activation.outcome_class,
        correctness_condition={"operator": "equals", "expected": False},
    )
    changed_resolution = _resolution_v2(activation, "forecast-changed", outcome=False)

    assert changed_activation.contract_id != activation.contract_id
    assert (
        build_settled_outcome_relation(activation, changed_resolution).relation_digest
        != relation.relation_digest
    )
    assert relation.activation.prediction == activation.prediction
    assert relation.resolution.settlement == resolution.settlement


def test_v2_uses_existing_resolution_event_and_partition_replay(tmp_path) -> None:
    activation = _activation_v2("replay")
    resolution = _resolution_v2(activation, "replay", outcome=True)
    journal, bodies, stream, writer = _store(tmp_path, (activation,))

    stored = append_procedure_resolution(
        writer,
        activation=activation,
        resolution=resolution,
        stream=stream,
    )
    partition_id = resolution_contract_partition_id(activation)
    book = ProcedureResolutionBook((activation,))
    book.replay(journal.all_records(stream, partition_id), bodies=bodies)

    assert stored.record.event_kind == "resolution"
    assert stored.record.partition_id == partition_id
    assert stored.record.accepted_coordinate == resolution.settlement.accepted_coordinate
    assert activation.prediction.accepted_coordinate != resolution.settlement.accepted_coordinate
    assert book.latest_non_overturned(activation.contract_id) == resolution


def test_settlement_must_follow_activation_and_cannot_use_attestation() -> None:
    activation = _activation_v2("law")
    resolution = _resolution_v2(activation, "law", outcome=True)
    simultaneous = resolution.model_copy(
        update={"observed_at": activation.activated_at, "recorded_at": activation.activated_at}
    )
    assert (
        evaluate_procedure_resolution(activation, simultaneous).refusal_code
        == "resolution.settlement_not_after_activation"
    )

    with pytest.raises(ValueError, match="attestations cannot prove"):
        build_procedure_resolution_v2(
            activation,
            sequence=1,
            verdict="satisfied",
            settlement=resolution.settlement,
            settlement_outcome=True,
            value={"count": 1},
            evidence_refs=(
                ProcedureProofReferenceV1(
                    kind="claim_attestation",
                    digest=_digest("attestation"),
                ),
            ),
            observed_at=NOW + timedelta(seconds=2),
            recorded_at=NOW + timedelta(seconds=3),
            actor_context=_actor(),
        )


def test_query_keeps_true_and_false_and_applies_visibility_before_rows(tmp_path) -> None:
    activations = tuple(
        sorted(
            (_activation_v2("true", index=2), _activation_v2("false", index=1)),
            key=lambda item: item.contract_id.encode("utf-8"),
        )
    )
    resolutions = tuple(
        _resolution_v2(activation, label, outcome=outcome)
        for activation, label, outcome in zip(
            activations,
            ("first", "second"),
            (True, False),
            strict=True,
        )
    )
    journal, bodies, stream, writer = _store(tmp_path, activations)
    for activation, resolution in zip(activations, resolutions, strict=True):
        append_procedure_resolution(
            writer,
            activation=activation,
            resolution=resolution,
            stream=stream,
        )
    records = {
        resolution_contract_partition_id(activation): journal.all_records(
            stream, resolution_contract_partition_id(activation)
        )
        for activation in activations
    }
    all_proofs = tuple(sorted(resolution.evidence_refs[0].digest for resolution in resolutions))
    request = SettledOutcomesQueryRequestV1(
        accepted_coordinate=_coordinate("query-after-settlement"),
        evaluation_time=NOW + timedelta(seconds=4),
        access_profile=SettledOutcomesAccessProfileV1(
            profile_id="calibration",
            can_read_resolution_bodies=True,
            visible_proof_digests=all_proofs,
        ),
    )

    result, receipt = query_settled_outcomes(
        request,
        activations=activations,
        records_by_partition=records,
        bodies=bodies,
    )
    replayed = query_settled_outcomes(
        request,
        activations=activations,
        records_by_partition=records,
        bodies=bodies,
    )

    assert replayed == (result, receipt)
    assert request.accepted_coordinate != activations[0].prediction.accepted_coordinate
    assert request.accepted_coordinate != resolutions[0].settlement.accepted_coordinate
    assert {row.relation.resolution.settlement_outcome for row in result.rows} == {False, True}
    assert receipt.visible_row_count == 2

    one_visible, one_receipt = query_settled_outcomes(
        request.model_copy(
            update={
                "access_profile": request.access_profile.model_copy(
                    update={"visible_proof_digests": all_proofs[:1]}
                )
            }
        ),
        activations=activations,
        records_by_partition=records,
        bodies=bodies,
    )
    assert len(one_visible.rows) == one_receipt.visible_row_count == 1
    assert "excluded" not in one_visible.model_dump(mode="json")

    denied, denied_receipt = query_settled_outcomes(
        request.model_copy(
            update={
                "access_profile": SettledOutcomesAccessProfileV1(
                    profile_id="denied",
                    can_read_resolution_bodies=False,
                )
            }
        ),
        activations=activations,
        records_by_partition=records,
        bodies=bodies,
    )
    assert denied.rows == ()
    assert denied_receipt.visible_row_count == 0


def test_open_indeterminate_and_overturned_histories_are_not_settled_rows(tmp_path) -> None:
    activation = _activation_v2("history")
    open_book = ProcedureResolutionBook((activation,))
    assert classify_settled_outcome_history(activation, open_book).status == "open"

    historical_activation = derive_resolution_activations(
        _accepted(),
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[2]
    indeterminate = build_procedure_resolution(
        historical_activation,
        sequence=1,
        verdict="indeterminate",
        value=None,
        evidence_refs=(),
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=2),
        actor_context=_actor(),
    )
    historical_book = ProcedureResolutionBook((historical_activation,))
    historical_book.resolutions[historical_activation.contract_id].append(indeterminate)
    assert (
        classify_settled_outcome_history(historical_activation, historical_book).status
        == "indeterminate"
    )

    resolution = _resolution_v2(activation, "history", outcome=False)
    journal, bodies, stream, writer = _store(tmp_path, (activation,))
    append_procedure_resolution(
        writer,
        activation=activation,
        resolution=resolution,
        stream=stream,
    )
    disposition = build_resolution_disposition(
        resolution,
        sequence=1,
        verdict="overturned",
        reviewer_actor_context=_actor("reviewer"),
        recorded_at=NOW + timedelta(seconds=4),
    )
    append_resolution_disposition(
        writer,
        activation=activation,
        resolution=resolution,
        disposition=disposition,
        stream=stream,
    )
    overturned_book = ProcedureResolutionBook((activation,))
    overturned_book.replay(
        journal.all_records(stream, resolution_contract_partition_id(activation)),
        bodies=bodies,
    )
    assert classify_settled_outcome_history(activation, overturned_book).status == "overturned"
