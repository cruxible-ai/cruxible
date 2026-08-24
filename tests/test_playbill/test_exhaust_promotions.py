"""Exact-range ExhaustPromotion law and canonical track-record visibility."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    AcceptedExhaustPromotionV1,
    ExhaustPromotionV1,
    ExhaustReceiptSetManifestV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
    evaluate_exhaust_promotion_law,
    exhaust_promotion_digest,
    exhaust_promotion_path,
    exhaust_receipt_set_manifest_digest,
    procedure_track_record_facts,
    render_exhaust_promotion,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.serving import bind_current_projection
from cruxible_core.service.playbill_procedures import (
    ExhaustReducerRegistry,
    LocalExhaustPromotionVerifier,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_resolution_contracts import (
    NOW,
    _accept_tree,
    _accepted,
    _actor,
    _coordinate,
    _digest,
)


class _Reducer:
    reducer_digest = _digest("reducer")

    def reduce(self, records):
        return {
            "event_count": len(records),
            "events": [item.event_kind for item in records],
        }


def _pin(role: str, kind: str, name: str, digest: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=digest,
    )


def _fixture(tmp_path):
    accepted = _accepted()
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
    journal.activate_writer(
        stream,
        "runs",
        fencing_token="writer",
        expected_head=journal.read_head(stream, "runs"),
    )
    writer = ProcedureExhaustWriter(
        journal=journal,
        bodies=bodies,
        fencing_token="writer",
    )
    for index in range(2):
        writer.append(
            stream=stream,
            partition_id="runs",
            event_kind="node_fired",
            accepted_coordinate=_coordinate(),
            procedure_artifact_digest=accepted.artifact_digest,
            definition_digest=accepted.procedure.definition_digest,
            actor_context=_actor(),
            recorded_at=NOW,
            payload={"node_id": f"node-{index}", "verdict": "succeeded"},
            run_id="run-a",
            admission_binding_digest=_digest("admission"),
        )
    records = journal.all_records(stream, "runs")
    manifest = ExhaustReceiptSetManifestV1(
        stream_id=stream.stream_id,
        partition_id="runs",
        first_sequence=1,
        last_sequence=2,
        record_digests=tuple(item.record_digest for item in records),
        payload_digests=tuple(item.record.payload_digest for item in records),
    )
    reducer = _Reducer()
    output = {"event_count": 2, "events": ["node_fired", "node_fired"]}
    stored_output = bodies.store(canonical_bytes(output))
    manifest_digest = exhaust_receipt_set_manifest_digest(manifest)
    assert bodies.store(canonical_bytes(manifest.model_dump(mode="json"))).digest == manifest_digest
    pins = tuple(
        sorted(
            (
                _pin(
                    "procedure",
                    "Procedure",
                    accepted.procedure.identity.name,
                    accepted.artifact_digest,
                ),
                _pin("reducer", "ExhaustReducer", "count-events", reducer.reducer_digest),
                _pin(
                    "receipt-set-manifest",
                    "ReceiptSetManifest",
                    "run-a",
                    manifest_digest,
                ),
            ),
            key=lambda pin: (
                pin.role.encode(),
                pin.target.qualified.encode(),
                pin.artifact_digest.encode(),
            ),
        )
    )
    promotion = ExhaustPromotionV1(
        identity=ArtifactIdentity(kind="ExhaustPromotion", name="run-a"),
        stream_id=stream.stream_id,
        partition_id="runs",
        first_sequence=1,
        last_sequence=2,
        chain_head_digest=records[-1].record_digest,
        receipt_set_manifest_digest=manifest_digest,
        reducer_digest=reducer.reducer_digest,
        output_digest=stored_output.digest,
        bound_generation_digests=(_coordinate().generation_root,),
        authority=ArtifactAuthority(
            propose_roles=("author",),
            approve_roles=("reviewer",),
        ),
        pins=pins,
    )
    return accepted, bodies, records, reducer, promotion


def test_promotion_verifies_exact_range_chain_receipts_reducer_and_output(tmp_path) -> None:
    _accepted_procedure, bodies, records, reducer, promotion = _fixture(tmp_path)
    result = evaluate_exhaust_promotion_law(
        promotion,
        records=records,
        bodies=bodies,
        reducer=reducer,
    )
    assert result.verdict == "accepted"
    assert result.artifact_digest == exhaust_promotion_digest(promotion)

    omitted = evaluate_exhaust_promotion_law(
        promotion,
        records=records[:1],
        bodies=bodies,
        reducer=reducer,
    )
    assert omitted.verdict == "refused"
    assert omitted.refusal_code == "promotion.range_mismatch"

    wrong_output = promotion.model_copy(update={"output_digest": _digest("wrong-output")})
    mismatch = evaluate_exhaust_promotion_law(
        wrong_output,
        records=records,
        bodies=bodies,
        reducer=reducer,
    )
    assert mismatch.verdict == "refused"
    assert mismatch.refusal_code == "promotion.output_mismatch"

    wrong_procedure_pins = tuple(
        pin.model_copy(update={"artifact_digest": _digest("wrong-procedure")})
        if pin.role == "procedure"
        else pin
        for pin in promotion.pins
    )
    wrong_procedure = promotion.model_copy(update={"pins": wrong_procedure_pins})
    procedure_mismatch = evaluate_exhaust_promotion_law(
        wrong_procedure,
        records=records,
        bodies=bodies,
        reducer=reducer,
    )
    assert procedure_mismatch.refusal_code == "promotion.procedure_set_mismatch"


def test_only_accepted_promotion_produces_canonical_track_record_fact(tmp_path) -> None:
    accepted_procedure, _bodies, _records, _reducer, promotion = _fixture(tmp_path)
    accepted = AcceptedExhaustPromotionV1(
        path=exhaust_promotion_path(promotion.identity.name),
        promotion=promotion,
        artifact_digest=exhaust_promotion_digest(promotion),
        accepted_coordinate=_coordinate(),
    )
    output = {"event_count": 2, "events": ["node_fired", "node_fired"]}
    facts = procedure_track_record_facts(accepted, output=output)

    assert len(facts) == 1
    assert facts[0].subject_identity == accepted_procedure.procedure.identity.qualified
    assert facts[0].schema_id == "playbill.procedure.track_record"
    assert facts[0].value["promotion_digest"] == {"$digest": accepted.artifact_digest}
    assert facts[0].value["output"] == output


def test_promotion_passes_proposal_replay_and_projects_canonical_output(tmp_path) -> None:
    instance, owner = initialize_local(tmp_path)
    base_procedure = _accepted().procedure.model_copy(
        update={
            "authority": ArtifactAuthority(
                propose_roles=("owner",),
                approve_roles=("owner",),
            )
        }
    )
    accepted_procedure = AcceptedProcedureV1(
        path="procedures/measured-procedure.yaml",
        procedure=base_procedure,
        artifact_digest=procedure_artifact_digest(base_procedure).tagged,
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            accepted_procedure.path: render_procedure(base_procedure),
        },
        timestamp="2026-08-17T15:00:00.000000Z",
        proposal_name="promotion-procedure",
    )

    journal_root = tmp_path / "promotion-journal"
    journal_root.mkdir()
    journal = LocalJournalBackend(journal_root)
    bodies = instance.body_store()
    stream = JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    journal.activate_writer(
        stream,
        "runs",
        fencing_token="writer",
        expected_head=journal.read_head(stream, "runs"),
    )
    writer = ProcedureExhaustWriter(journal=journal, bodies=bodies, fencing_token="writer")
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    for index in range(2):
        writer.append(
            stream=stream,
            partition_id="runs",
            event_kind="node_fired",
            accepted_coordinate=coordinate,
            procedure_artifact_digest=accepted_procedure.artifact_digest,
            definition_digest=accepted_procedure.procedure.definition_digest,
            actor_context=_actor(),
            recorded_at=NOW,
            payload={"node_id": f"node-{index}", "verdict": "succeeded"},
            run_id="run-a",
            admission_binding_digest=_digest("admission"),
        )
    records = journal.all_records(stream, "runs")
    manifest = ExhaustReceiptSetManifestV1(
        stream_id=stream.stream_id,
        partition_id="runs",
        first_sequence=1,
        last_sequence=2,
        record_digests=tuple(item.record_digest for item in records),
        payload_digests=tuple(item.record.payload_digest for item in records),
    )
    reducer = _Reducer()
    output = {"event_count": 2, "events": ["node_fired", "node_fired"]}
    output_digest = bodies.store(canonical_bytes(output)).digest
    manifest_digest = exhaust_receipt_set_manifest_digest(manifest)
    assert bodies.store(canonical_bytes(manifest.model_dump(mode="json"))).digest == manifest_digest
    pins = tuple(
        sorted(
            (
                _pin(
                    "procedure",
                    "Procedure",
                    accepted_procedure.procedure.identity.name,
                    accepted_procedure.artifact_digest,
                ),
                _pin("reducer", "ExhaustReducer", "count-events", reducer.reducer_digest),
                _pin(
                    "receipt-set-manifest",
                    "ReceiptSetManifest",
                    "run-a",
                    manifest_digest,
                ),
            ),
            key=lambda pin: (
                pin.role.encode(),
                pin.target.qualified.encode(),
                pin.artifact_digest.encode(),
            ),
        )
    )
    promotion = ExhaustPromotionV1(
        identity=ArtifactIdentity(kind="ExhaustPromotion", name="run-a"),
        stream_id=stream.stream_id,
        partition_id="runs",
        first_sequence=1,
        last_sequence=2,
        chain_head_digest=records[-1].record_digest,
        receipt_set_manifest_digest=manifest_digest,
        reducer_digest=reducer.reducer_digest,
        output_digest=output_digest,
        bound_generation_digests=(coordinate.generation_root,),
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        pins=pins,
    )
    verifier = LocalExhaustPromotionVerifier(
        instance_id=instance.descriptor.instance_id,
        journal=journal,
        bodies=bodies,
        reducers=ExhaustReducerRegistry({reducer.reducer_digest: reducer}),
    )
    instance = PlaybillInstance.open(
        instance.root,
        trust_root=instance.trust_root,
        promotion_verifier=verifier,
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            exhaust_promotion_path(promotion.identity.name): render_exhaust_promotion(promotion),
        },
        timestamp="2026-08-17T16:00:00.000000Z",
        proposal_name="run-a-promotion",
    )

    publication = Path(instance.inspect().storage_directories["projections"])
    with bind_current_projection(publication, expected=instance.accepted_coordinate()) as handle:
        connection = sqlite3.connect(handle.index_path)
        try:
            row = connection.execute(
                "SELECT value_json FROM semantic_facts "
                "WHERE schema_id = 'playbill.procedure.track_record' "
                "AND subject_identity = ?",
                (accepted_procedure.procedure.identity.qualified,),
            ).fetchone()
        finally:
            connection.close()
    assert row is not None
    projected = json.loads(row[0])
    assert projected["output"] == output
    assert projected["output_digest"] == {"$digest": output_digest}
