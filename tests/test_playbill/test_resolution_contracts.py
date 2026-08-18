"""Semantic ResolutionContract activation, replay, and authority laws."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    typed_digest,
)
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_core.playbill.errors import PlaybillExecutionError
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
)
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.measurements import (
    AcceptedQueryProcedureMeasurementV1,
    ProcedureMeasurementDeclarationV1,
    ProcedureMeasurementExpectationV1,
)
from cruxible_core.playbill.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProjectNodeV3,
    StateTapNodeV3,
    iter_pin_bindings,
)
from cruxible_core.playbill.procedures.resolution import (
    AcceptedAuthorityBasisV1,
    ProcedureProofReferenceV1,
    ProcedureResolutionBook,
    append_procedure_resolution,
    append_resolution_disposition,
    build_procedure_resolution,
    build_resolution_disposition,
    derive_resolution_activations,
    evaluate_procedure_resolution,
    resolution_contract_partition_id,
    resolve_authority_basis,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.serving import bind_current_projection
from cruxible_core.playbill.settlement import ChangeActorBinding
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign

NOW = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-resolution-test-v1",
        {"label": label},
    ).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _duration(value: int) -> CanonicalDurationV1:
    return CanonicalDurationV1(microseconds=value)


def _coordinate(label: str = "accepted") -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root=typed_digest(
            SemanticRoot, "playbill-resolution-semantic-v1", {"label": label}
        ).tagged,
        generation_root=typed_digest(
            GenerationRoot, "playbill-resolution-generation-v1", {"label": label}
        ).tagged,
        compiler_digest=_digest("compiler"),
    )


def _actor(actor_id: str = "operator") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="instance-a",
        operation_id=f"operation-{actor_id}",
        timestamp=NOW,
    )


def _measurement(
    name: str,
    grain: str,
    *,
    node_id: str | None = None,
    from_node_id: str | None = None,
    arm_label: str | None = None,
) -> ProcedureMeasurementDeclarationV1:
    return ProcedureMeasurementDeclarationV1(
        name=name,
        subject_grain=grain,  # type: ignore[arg-type]
        node_id=node_id,
        from_node_id=from_node_id,
        arm_label=arm_label,  # type: ignore[arg-type]
        measurement=AcceptedQueryProcedureMeasurementV1(
            query=_pin("query", "QueryDefinition", f"measure-{name}"),
            expect=ProcedureMeasurementExpectationV1(min_count=1),
        ),
        check_after=_duration(1_000_000),
        expires_after=_duration(10_000_000),
    )


def _accepted() -> AcceptedProcedureV1:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    definition = ProcedureDefinitionV3(
        name="measured-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=_pin("query", "QueryDefinition", "state"),
                as_="rows",
            ),
            GuardNodeV3(
                node_id="gate",
                predicate=GuardPredicateV1(
                    left=PredicateOperandV1(kind="count", alias="rows"),
                    operator="gt",
                    right=PredicateOperandV1(kind="literal", value=0),
                ),
                on_true="hot",
                on_false="cold",
                refusal_code="empty",
                message="No rows.",
            ),
            ProjectNodeV3(
                node_id="hot",
                fields={"arm": "hot"},
                contract_out=contract_out,
                as_="hot_result",
                next="finish",
            ),
            ProjectNodeV3(
                node_id="cold",
                fields={"arm": "cold"},
                contract_out=contract_out,
                as_="cold_result",
                next="finish",
            ),
            ProjectNodeV3(
                node_id="finish",
                fields={"status": "done"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        measurements=(
            _measurement(
                "arm-health", "arm", node_id="hot", from_node_id="gate", arm_label="on_true"
            ),
            _measurement("node-health", "node", node_id="hot"),
            _measurement("unit-health", "procedure_unit"),
        ),
        budget=ProcedureBudgetV3(
            wall_clock=_duration(1_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=_duration(2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )
    pins = tuple(
        sorted(
            {
                binding
                for binding in iter_pin_bindings(definition)
                if isinstance(binding, ArtifactPin)
            },
            key=lambda pin: (
                pin.role.encode(),
                pin.target.qualified.encode(),
                pin.artifact_digest.encode(),
            ),
        )
    )
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        authority=ArtifactAuthority(
            propose_roles=("author",),
            approve_roles=("reviewer",),
        ),
        pins=pins,
        activation_policy="snapshot",
    )
    return AcceptedProcedureV1(
        path=procedure_path(definition.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def test_accepted_measurements_derive_exact_semantic_grains_and_windows() -> None:
    accepted = _accepted()
    activations = derive_resolution_activations(
        accepted,
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )

    arm, node, unit = activations
    assert arm.subject.address.selector.scheme == "procedure-arm-v1"
    assert arm.subject.address.selector.value == "gate:on_true:hot"
    assert arm.arm_subtree_digest == arm.subject.content_digest
    assert node.subject.address.selector.scheme == "procedure-node-v1"
    assert node.node_local_digest == node.subject.content_digest
    assert unit.subject.address.selector.scheme == "procedure-unit-v1"
    assert unit.subject.content_digest == accepted.procedure.definition_digest
    assert all(item.check_at == NOW + timedelta(seconds=1) for item in activations)
    assert all(item.expires_at == NOW + timedelta(seconds=10) for item in activations)
    assert (
        derive_resolution_activations(
            accepted,
            accepted_coordinate=_coordinate(),
            activated_at=NOW,
        )
        == activations
    )


def test_resolution_and_latest_disposition_replay_from_exhaust(tmp_path) -> None:
    accepted = _accepted()
    activation = derive_resolution_activations(
        accepted,
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[2]
    evidence = (ProcedureProofReferenceV1(kind="query_receipt", digest=_digest("query-receipt")),)
    resolution = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"count": 2},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=3),
        actor_context=_actor(),
    )
    disposition = build_resolution_disposition(
        resolution,
        sequence=1,
        verdict="overturned",
        reviewer_actor_context=_actor("reviewer"),
        recorded_at=NOW + timedelta(seconds=4),
        note="Query result was invalidated.",
    )
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
    append_procedure_resolution(
        writer,
        activation=activation,
        resolution=resolution,
        stream=stream,
    )
    book = ProcedureResolutionBook((activation,))
    book.replay(journal.all_records(stream, partition_id), bodies=bodies)
    assert book.latest_non_overturned(activation.contract_id) == resolution

    append_resolution_disposition(
        writer,
        activation=activation,
        resolution=resolution,
        disposition=disposition,
        stream=stream,
    )
    book.replay(journal.all_records(stream, partition_id), bodies=bodies)
    assert book.latest_non_overturned(activation.contract_id) is None


def test_authority_resolver_excludes_absent_expired_superseded_and_overturned() -> None:
    current = AcceptedAuthorityBasisV1(
        kind="standing_mandate",
        basis_digest=_digest("mandate-current"),
        accepted_coordinate=_coordinate(),
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        current_artifact_digest=_digest("mandate-current"),
        artifact_digest=_digest("mandate-current"),
    )
    expired = current.model_copy(
        update={
            "basis_digest": _digest("expired"),
            "artifact_digest": _digest("expired"),
            "current_artifact_digest": _digest("expired"),
            "valid_until": NOW,
        }
    )
    superseded = current.model_copy(
        update={
            "basis_digest": _digest("superseded"),
            "artifact_digest": _digest("superseded"),
            "current_artifact_digest": _digest("successor"),
        }
    )
    overturned = AcceptedAuthorityBasisV1(
        kind="resolution",
        basis_digest=_digest("resolution"),
        accepted_coordinate=_coordinate(),
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        current_artifact_digest=_digest("promotion"),
        artifact_digest=_digest("promotion"),
        resolution_verdict="satisfied",
        resolution_overturned=True,
        accepted_promotion_digest=_digest("promotion"),
    )
    candidates = {item.basis_digest: item for item in (current, expired, superseded, overturned)}
    requested = tuple(sorted((*candidates, _digest("absent"))))

    assert resolve_authority_basis(
        requested,
        accepted_basis=candidates,
        evaluation_time=NOW,
    ) == (current.basis_digest,)


def test_resolution_law_refuses_goodhart_clock_and_expectation_mismatches() -> None:
    activation = derive_resolution_activations(
        _accepted(),
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[2]
    evidence = (ProcedureProofReferenceV1(kind="query_receipt", digest=_digest("query")),)
    early = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"count": 1},
        evidence_refs=evidence,
        observed_at=NOW,
        recorded_at=NOW,
        actor_context=_actor(),
    )
    assert evaluate_procedure_resolution(activation, early).refusal_code == (
        "resolution.before_check_at"
    )

    false_success = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"count": 0},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=2),
        actor_context=_actor(),
    )
    assert evaluate_procedure_resolution(activation, false_success).refusal_code == (
        "resolution.expectation_not_satisfied"
    )


def test_resolution_contract_reopens_only_after_latest_answer_is_overturned(tmp_path) -> None:
    accepted = _accepted()
    activation = derive_resolution_activations(
        accepted,
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[2]
    evidence = (ProcedureProofReferenceV1(kind="query_receipt", digest=_digest("query")),)
    first = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"count": 1},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=2),
        actor_context=_actor(),
    )
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
    partition_id = resolution_contract_partition_id(activation)
    journal.activate_writer(
        stream,
        partition_id,
        fencing_token="writer",
        expected_head=journal.read_head(stream, partition_id),
    )
    writer = ProcedureExhaustWriter(journal=journal, bodies=bodies, fencing_token="writer")
    append_procedure_resolution(
        writer,
        activation=activation,
        resolution=first,
        stream=stream,
    )
    second = build_procedure_resolution(
        activation,
        sequence=2,
        verdict="satisfied",
        value={"count": 1},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=3),
        recorded_at=NOW + timedelta(seconds=3),
        actor_context=_actor(),
    )
    with pytest.raises(PlaybillExecutionError, match="closed until"):
        append_procedure_resolution(
            writer,
            activation=activation,
            resolution=second,
            stream=stream,
        )


def _accept_tree(instance, owner, tree, *, timestamp: str, proposal_name: str) -> None:
    base = instance.accepted_coordinate()
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{proposal_name}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=timestamp,
    )
    assert proposed.candidate is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=tree,
        candidate=proposed.candidate,
        approvals=(_sign(owner, proposed.candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=len(instance.accepted_history()),
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def test_derived_activation_remains_bound_to_its_accepting_generation(tmp_path) -> None:
    instance, owner = initialize_local(tmp_path)
    accepted = _accepted()
    procedure = accepted.procedure.model_copy(
        update={
            "authority": ArtifactAuthority(
                propose_roles=("owner",),
                approve_roles=("owner",),
            )
        }
    )
    procedure_path_value = procedure_path(procedure.identity.name)
    first_tree = {
        **instance.tree_at(instance.accepted_coordinate().git_oid),
        procedure_path_value: render_procedure(procedure),
    }
    _accept_tree(
        instance,
        owner,
        first_tree,
        timestamp="2026-08-17T15:00:00.000000Z",
        proposal_name="measured-procedure",
    )
    accepting_coordinate = instance.accepted_coordinate()

    body = instance.store_document_body(b"unrelated accepted state\n")
    document = DocumentShell(
        identity="document:unrelated",
        document_kind="note",
        title="Unrelated",
        media_type="text/plain",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="governed_write", approval_roles=("owner",)),
        governance_scope=("project:test",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    second_tree = {
        **instance.tree_at(accepting_coordinate.git_oid),
        "documents/unrelated.yaml": render_document(document),
    }
    _accept_tree(
        instance,
        owner,
        second_tree,
        timestamp="2026-08-17T16:00:00.000000Z",
        proposal_name="unrelated-document",
    )
    assert instance.accepted_coordinate().git_oid != accepting_coordinate.git_oid

    publication = Path(instance.inspect().storage_directories["projections"])
    with bind_current_projection(publication, expected=instance.accepted_coordinate()) as handle:
        connection = sqlite3.connect(handle.index_path)
        try:
            rows = connection.execute(
                "SELECT value_json FROM semantic_facts "
                "WHERE schema_id = 'playbill.procedure.resolution_activation' "
                "AND subject_identity = ? ORDER BY fact_key",
                (procedure.identity.qualified,),
            ).fetchall()
        finally:
            connection.close()
    assert len(rows) == 3
    activation_coordinate = json.loads(rows[0][0])["subject"]["accepted_coordinate"]
    assert activation_coordinate["git_oid"] == accepting_coordinate.git_oid
    assert activation_coordinate["semantic_root"] == accepting_coordinate.semantic_root
    assert activation_coordinate["generation_root"] == accepting_coordinate.generation_root
