"""Donor-independent laws for retained Line track-record projection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpecV1,
    LineSpecV1,
    ManualTriggerPolicyV1,
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    InboxEgressNodeV3,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    StateTapNodeV3,
)
from cruxible_client.contracts.projection_extensions import playbill_runtime_extension_registry
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    ExhaustPromotionV1,
    ExhaustReceiptSetManifestV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
    VerifiedExhaustRecordV1,
    exhaust_promotion_output_digest,
    exhaust_promotion_path,
    exhaust_receipt_set_manifest_digest,
    parse_journal_payload,
    render_exhaust_promotion,
)
from cruxible_core.playbill.exhaust.line_track_records import (
    LINE_TRACK_RECORD_TAG,
    LineTrackRecordError,
    LineTrackRecordReducer,
    LineTrackRecordV1,
    line_track_record_facts,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.procedures.egress import EFFECTIVE_RUNG_TERMS
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.serving import bind_current_projection
from cruxible_core.service.playbill_floor import service_export_playbill_floor
from cruxible_core.service.playbill_procedures import (
    ExhaustReducerRegistry,
    LocalExhaustPromotionVerifier,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_resolution_contracts import _accept_tree, _actor

NOW = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
ACCESS = BodyAccessContext(principal_id="line-track-record-test", can_read_body=True)


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-line-track-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str, *, digest: str | None = None) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=digest or _digest(name),
    )


def _artifacts() -> tuple[AcceptedProcedureV1, AcceptedLineSpecV1]:
    contract_in = _pin("contract-in", "Contract", "run-input")
    contract_out = _pin("contract-out", "Contract", "run-output")
    query = _pin("query", "QueryDefinition", "open-orders")
    definition = ProcedureDefinitionV3(
        name="orders-triage",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(node_id="read", query=query, parameters={}, as_="rows", next="emit"),
            InboxEgressNodeV3(node_id="emit", input={"items": "$steps.rows.items"}),
        ),
        returns="rows",
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=5_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=10_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=2,
        ),
        terminal_capability=1,
    )
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=tuple(sorted((contract_in, contract_out, query), key=lambda pin: pin.role)),
        activation_policy="drain",
    )
    accepted_procedure = AcceptedProcedureV1(
        path=procedure_path(definition.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    procedure_pin = _pin(
        "procedure", "Procedure", definition.name, digest=accepted_procedure.artifact_digest
    )
    line = LineSpecV1(
        identity=ArtifactIdentity(kind="Line", name="orders-triage"),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        slot_bindings=(),
        trigger_policy=ManualTriggerPolicyV1(),
        requested_terminal_rung=1,
        budgets={
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": 5_000_000,
        },
        epsilon={"$decimal": "0.1"},
        pins=(procedure_pin,),
    )
    return accepted_procedure, AcceptedLineSpecV1(
        path=line_spec_path(line.identity.name),
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


def _terms() -> list[dict[str, object]]:
    return [
        {
            "tag": "playbill-effective-rung-term-v1",
            "term": term,
            "rung": 1 if term == "line_requested_rung" else 3,
            "reason": f"{term} permits the run",
            "basis_digest": None,
        }
        for term in EFFECTIVE_RUNG_TERMS
    ]


def _verified_records(
    *, procedure: AcceptedProcedureV1, line: AcceptedLineSpecV1
) -> tuple[VerifiedExhaustRecordV1, ...]:
    common = {
        "generation_digest": _digest("generation"),
        "procedure_artifact_digest": procedure.artifact_digest,
        "definition_digest": procedure.procedure.definition_digest,
        "run_id": "line-run-1",
        "occurrence_id": _digest("occurrence"),
        "attempt": 1,
        "line_spec_digest": line.artifact_digest,
    }
    payloads = (
        ("admission_bound", {"deployment_snapshot_digest": _digest("deployment")}),
        (
            "terminal_egress",
            {
                "node_id": "emit",
                "kind": "post_inbox",
                "required_rung": 1,
                "children": [
                    {
                        "child_index": 0,
                        "item_key": "00000000.order",
                        "manifest_digest": _digest("manifest"),
                    }
                ],
                "verdict": "delivered",
                "effective_rung": 1,
                "effective_rung_digest": _digest("effective-rung"),
                "limiting_term": "line_requested_rung",
                "terms": _terms(),
            },
        ),
    )
    return tuple(
        VerifiedExhaustRecordV1(
            record_digest=_digest(f"record-{sequence}"),
            sequence=sequence,
            event_kind=event_kind,
            payload_digest=_digest(f"payload-{sequence}"),
            payload=payload,
            **common,  # type: ignore[arg-type]
        )
        for sequence, (event_kind, payload) in enumerate(payloads, start=1)
    )


def _accepted_promotion(output: object):
    procedure, line = _artifacts()
    reducer = LineTrackRecordReducer(accepted_line=line, accepted_procedure=procedure)
    promotion = ExhaustPromotionV1(
        identity=ArtifactIdentity(kind="ExhaustPromotion", name="orders-triage-window"),
        stream_id="lines",
        partition_id="line-runs",
        first_sequence=1,
        last_sequence=2,
        chain_head_digest=_digest("head"),
        receipt_set_manifest_digest=_digest("receipt-manifest"),
        reducer_digest=reducer.reducer_digest,
        output_digest=exhaust_promotion_output_digest(output),
        bound_generation_digests=(_digest("generation"),),
        pins=tuple(
            sorted(
                (
                    _pin(
                        "procedure", "Procedure", "orders-triage", digest=procedure.artifact_digest
                    ),
                    _pin("line", "Line", "orders-triage", digest=line.artifact_digest),
                    _pin("reducer", "ExhaustReducer", "line-track", digest=reducer.reducer_digest),
                    _pin(
                        "receipt-set-manifest",
                        "ReceiptSetManifest",
                        "orders-triage-window",
                        digest=_digest("receipt-manifest"),
                    ),
                ),
                key=lambda pin: (pin.role, pin.target.qualified),
            )
        ),
    )
    from cruxible_core.playbill.exhaust import AcceptedExhaustPromotionV1, exhaust_promotion_digest

    accepted = AcceptedExhaustPromotionV1(
        path=exhaust_promotion_path(promotion.identity.name),
        promotion=promotion,
        artifact_digest=exhaust_promotion_digest(promotion),
        accepted_coordinate=AcceptedCoordinate(
            git_oid="a" * 40,
            semantic_root=_digest("semantic"),
            generation_root=_digest("generation"),
            compiler_digest=_digest("compiler"),
        ),
    )
    return accepted, procedure, line, reducer


def test_the_runtime_projection_registry_declares_the_line_grain() -> None:
    registry = playbill_runtime_extension_registry()
    assert registry.supports("playbill.line.track_record", 1, classification="semantic")


def test_a_promotion_that_declares_no_line_track_record_emits_no_line_fact() -> None:
    accepted, _, _, _ = _accepted_promotion({"count": 2})
    assert line_track_record_facts(accepted, output={"count": 2}) == ()


def test_a_malformed_declared_track_record_refuses() -> None:
    _, procedure, line, reducer = _accepted_promotion({"count": 2})
    output = reducer.reduce(_verified_records(procedure=procedure, line=line))
    assert isinstance(output, dict) and output["tag"] == LINE_TRACK_RECORD_TAG
    output["occurrence_epoch"] = 0
    accepted, _, _, _ = _accepted_promotion(output)
    with pytest.raises(LineTrackRecordError, match="malformed Line track record"):
        line_track_record_facts(accepted, output=normalize_canonical(output))


def test_an_accepted_promotion_projects_its_line_track_record_through_the_floor(tmp_path) -> None:
    instance, owner = initialize_local(tmp_path)
    procedure, line = _artifacts()
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            procedure.path: render_procedure(procedure.procedure),
            line.path: render_line_spec(line.line),
        },
        timestamp="2026-08-18T15:00:00.000000Z",
        proposal_name="line-track-artifacts",
    )

    journal_root = tmp_path / "track-record-journal"
    journal_root.mkdir()
    journal = LocalJournalBackend(journal_root)
    bodies = instance.body_store()
    stream = JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="lines",
    )
    journal.activate_writer(
        stream,
        "line-runs",
        fencing_token="writer",
        expected_head=journal.read_head(stream, "line-runs"),
    )
    writer = ProcedureExhaustWriter(journal=journal, bodies=bodies, fencing_token="writer")
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    common = {
        "stream": stream,
        "partition_id": "line-runs",
        "accepted_coordinate": coordinate,
        "procedure_artifact_digest": procedure.artifact_digest,
        "definition_digest": procedure.procedure.definition_digest,
        "actor_context": _actor(),
        "recorded_at": NOW,
        "run_id": "line-run-1",
        "line_spec_digest": line.artifact_digest,
        "occurrence_id": _digest("occurrence"),
        "attempt": 1,
        "admission_binding_digest": _digest("admission"),
    }
    writer.append(
        event_kind="admission_bound",
        payload={"deployment_snapshot_digest": _digest("deployment")},
        **common,  # type: ignore[arg-type]
    )
    writer.append(
        event_kind="terminal_egress",
        payload={
            "node_id": "emit",
            "kind": "post_inbox",
            "required_rung": 1,
            "children": [
                {
                    "child_index": 0,
                    "item_key": "00000000.order",
                    "manifest_digest": _digest("manifest"),
                }
            ],
            "verdict": "delivered",
            "effective_rung": 1,
            "effective_rung_digest": _digest("effective-rung"),
            "limiting_term": "line_requested_rung",
            "terms": _terms(),
        },
        **common,  # type: ignore[arg-type]
    )
    stored = journal.all_records(stream, "line-runs")
    manifest = ExhaustReceiptSetManifestV1(
        stream_id="lines",
        partition_id="line-runs",
        first_sequence=1,
        last_sequence=2,
        record_digests=tuple(item.record_digest for item in stored),
        payload_digests=tuple(item.record.payload_digest for item in stored),
    )
    manifest_digest = exhaust_receipt_set_manifest_digest(manifest)
    assert bodies.store(canonical_bytes(manifest.model_dump(mode="json"))).digest == manifest_digest
    reducer = LineTrackRecordReducer(accepted_line=line, accepted_procedure=procedure)
    verified = tuple(
        VerifiedExhaustRecordV1(
            record_digest=item.record_digest,
            sequence=item.record.sequence,
            event_kind=item.record.event_kind,
            generation_digest=item.record.accepted_coordinate.generation_root,
            payload_digest=item.record.payload_digest,
            payload=parse_journal_payload(bodies.read(item.record.payload_digest, access=ACCESS)),
            procedure_artifact_digest=item.record.procedure_artifact_digest,
            definition_digest=item.record.definition_digest,
            run_id=item.record.run_id,
            occurrence_id=item.record.occurrence_id,
            attempt=item.record.attempt,
            line_spec_digest=item.record.line_spec_digest,
        )
        for item in stored
    )
    output = normalize_canonical(reducer.reduce(verified))
    output_digest = bodies.store(canonical_bytes(output)).digest
    promotion = ExhaustPromotionV1(
        identity=ArtifactIdentity(kind="ExhaustPromotion", name="orders-triage-window"),
        stream_id="lines",
        partition_id="line-runs",
        first_sequence=1,
        last_sequence=2,
        chain_head_digest=stored[-1].record_digest,
        receipt_set_manifest_digest=manifest_digest,
        reducer_digest=reducer.reducer_digest,
        output_digest=output_digest,
        bound_generation_digests=(coordinate.generation_root,),
        pins=tuple(
            sorted(
                (
                    _pin(
                        "procedure", "Procedure", "orders-triage", digest=procedure.artifact_digest
                    ),
                    _pin("line", "Line", "orders-triage", digest=line.artifact_digest),
                    _pin("reducer", "ExhaustReducer", "line-track", digest=reducer.reducer_digest),
                    _pin(
                        "receipt-set-manifest",
                        "ReceiptSetManifest",
                        "orders-triage-window",
                        digest=manifest_digest,
                    ),
                ),
                key=lambda pin: (pin.role, pin.target.qualified),
            )
        ),
    )
    verifier = LocalExhaustPromotionVerifier(
        instance_id=instance.descriptor.instance_id,
        journal=journal,
        bodies=bodies,
        reducers=ExhaustReducerRegistry({reducer.reducer_digest: reducer}),
    )
    instance = PlaybillInstance.open(
        instance.root, trust_root=instance.trust_root, promotion_verifier=verifier
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            exhaust_promotion_path(promotion.identity.name): render_exhaust_promotion(promotion),
        },
        timestamp="2026-08-18T16:00:00.000000Z",
        proposal_name="line-track-promotion",
    )

    floor = service_export_playbill_floor(instance)
    assert "procedures/orders-triage.card.json" in floor
    publication = Path(instance.inspect().storage_directories["projections"])
    with bind_current_projection(publication, expected=instance.accepted_coordinate()) as handle:
        with sqlite3.connect(handle.index_path) as connection:
            row = connection.execute(
                "SELECT value_json FROM semantic_facts "
                "WHERE schema_id = 'playbill.line.track_record'",
            ).fetchone()
    assert row is not None
    record = LineTrackRecordV1.model_validate(json.loads(row[0])["track_record"])
    assert record.line_id == "orders-triage"
    assert record.tally.delivered == 1
