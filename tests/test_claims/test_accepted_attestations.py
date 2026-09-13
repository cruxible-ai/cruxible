"""Standalone signed attestations travel through ordinary governed acceptance."""

from pathlib import Path

from cruxible_client.contracts.accepted_attestations import (
    attestation_artifact_digest,
    attestation_identity,
    attestation_path,
    parse_accepted_attestation,
    render_accepted_attestation,
)
from cruxible_client.contracts.claim_attestations import claim_attestation_v2_envelope_digest
from tests.test_claims.test_claim_attestation_service import _request
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
from tests.test_indexes.test_resolution_contracts import _accept_tree


def test_signed_attestation_is_accepted_without_revising_its_claim(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    before = instance.accepted_coordinate()
    envelope = _request(instance, owner, claim_id, tmp_path, stance="contradict").attestation
    digest = claim_attestation_v2_envelope_digest(envelope)
    path = attestation_path(digest)
    content = render_accepted_attestation(envelope)
    assert parse_accepted_attestation(content, path=path) == envelope
    tree = instance.tree_at(before.git_oid)
    tree[path] = content
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name="attestation"
    )
    after = instance.accepted_coordinate()
    assert after != before
    assert instance.blob_at(after.git_oid, path) == content
    assert instance.blob_at(after.git_oid, f"claims/{claim_id}.json") == instance.blob_at(
        before.git_oid, f"claims/{claim_id}.json"
    )
    with instance.bind_accepted_projection(after) as projection:
        row = projection.typed.envelope(attestation_identity(envelope).qualified)
        assert row is not None
        assert row.artifact_digest == attestation_artifact_digest(envelope).tagged
        connection = projection.typed.connection
        assert [
            tuple(row) for row in connection.execute("SELECT envelope_digest FROM attestations")
        ] == [(digest,)]
        fields = {row[1] for row in connection.execute("PRAGMA table_info(attestations)")}
        assert not fields.intersection({"revision", "predecessor_digest", "lifecycle"})
        uses = connection.execute(
            "SELECT owner_kind,owner_key,capture_digest,role,origin FROM citation_uses "
            "WHERE owner_kind='attestation'"
        ).fetchall()
        assert [tuple(row) for row in uses] == [
            ("attestation", digest, capture, None, None)
            for capture in envelope.statement.cited_capture_digests
        ]


def test_typed_batch_pending_and_historical_verdict_boundaries(tmp_path: Path) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.authoring.models import (
        AttestationAuthoringPayloadV1,
        ChangeSetAuthoringPayloadV1,
        authoring_member_identity,
    )
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.evidence.claim_attestations import service_append_claim_attestation
    from cruxible_core.service.evidence.evidence import service_evaluate_playbill_claim_verdict
    from tests.test_authoring.test_authoring_existing_capture import _activate
    from tests.test_claims.test_claim_attestation_service import RECORDED_AT

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    before = instance.accepted_coordinate()
    request = _request(instance, owner, claim_id, tmp_path, stance="contradict")
    at = RECORDED_AT + timedelta(minutes=2)
    old = service_evaluate_playbill_claim_verdict(
        instance, claim_identity=claim_id, evaluation_time=at
    )
    service_append_claim_attestation(
        instance, request=request, actor_id="owner", recorded_at=RECORDED_AT
    )
    assert (
        service_evaluate_playbill_claim_verdict(
            instance, claim_identity=claim_id, evaluation_time=at
        )
        == old
    )
    assert instance.accepted_coordinate() == before
    second = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        stance="unsure",
        attested_at=RECORDED_AT + timedelta(seconds=1),
    )
    payload = ChangeSetAuthoringPayloadV1(
        members=tuple(
            sorted(
                (
                    AttestationAuthoringPayloadV1(attestation=item.attestation)
                    for item in (request, second)
                ),
                key=authoring_member_identity,
            )
        )
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor, payload=payload, canonical_timestamp="2026-08-28T15:01:00.000000Z"
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.state == "ready_to_activate", submitted.model_dump(mode="json")
    _activate(instance, submitted)
    after = instance.accepted_coordinate()
    current = service_evaluate_playbill_claim_verdict(
        instance, claim_identity=claim_id, evaluation_time=at
    )
    assert (
        claim_attestation_v2_envelope_digest(request.attestation)
        in current.verdict.contradicting_evidence_digests
    )
    assert current.verdict != old.verdict
    assert (
        service_evaluate_playbill_claim_verdict(
            instance,
            claim_identity=claim_id,
            evaluation_time=at,
            at=AcceptedCoordinate.from_internal(before),
        )
        == old
    )
    from cruxible_core.service.claims.claims import service_explain_playbill_claim
    from cruxible_core.service.discovery.query import build_accepted_query_facts

    facts = build_accepted_query_facts(instance, coordinate=after)
    fact = next(row for row in facts.claims if row.accepted.claim.identity.name == claim_id)
    explanation = service_explain_playbill_claim(instance, identity=claim_id, evaluation_time=at)
    expected = {claim_attestation_v2_envelope_digest(v.attestation) for v in (request, second)}
    assert {item.attestation_digest for item in fact.attestations} == expected
    assert {item.attestation_digest for item in explanation.exact_attestations} == expected
    with instance.bind_accepted_projection(after) as projection:
        values = projection.typed.claim_attestations(
            request.attestation.statement.claim_identity.qualified,
            request.attestation.statement.claim_artifact_digest,
        )
        assert len(values) == 2


def test_invalid_envelope_refused_before_acceptance(tmp_path: Path) -> None:
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from tests.test_claims.test_claim_attestation_service import _resign

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    base = instance.accepted_coordinate()
    request = _request(instance, owner, claim_id, tmp_path)
    invalid = [
        request.attestation.model_copy(update={"signature": "0" * 128}),
        _resign(request, owner, claim_artifact_digest="sha256:" + "1" * 64).attestation,
        _resign(request, owner, subject_shell_digest="sha256:" + "2" * 64).attestation,
    ]
    for index, envelope in enumerate(invalid):
        tree = instance.tree_at(base.git_oid)
        tree[attestation_path(claim_attestation_v2_envelope_digest(envelope))] = (
            render_accepted_attestation(envelope)
        )
        result = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref=f"refs/proposals/owner/invalid-{index}", proposed_base_oid=base.git_oid
            ),
            candidate_tree=tree,
            timestamp="2026-08-28T15:01:00.000000Z",
        )
        assert result.candidate is None
        assert result.evaluation.diagnostics
        assert instance.accepted_coordinate() == base


def test_exact_version_survives_successor_and_projection_rebuild(tmp_path: Path) -> None:
    import shutil

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claims import (
        claim_artifact_digest,
        claim_path,
        parse_claim,
        render_claim,
    )
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.evidence.evidence import service_evaluate_playbill_claim_verdict
    from tests.test_claims.test_claim_attestation_service import RECORDED_AT

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    original = instance.accepted_coordinate()
    envelope = _request(instance, owner, claim_id, tmp_path, stance="contradict").attestation
    path = attestation_path(claim_attestation_v2_envelope_digest(envelope))
    tree = instance.tree_at(original.git_oid)
    tree[path] = render_accepted_attestation(envelope)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name="attest-old"
    )
    attested = instance.accepted_coordinate()
    old_verdict = service_evaluate_playbill_claim_verdict(
        instance, claim_identity=claim_id, evaluation_time=RECORDED_AT
    )
    from cruxible_core.indexes.typed_sqlite import logical_export

    with instance.bind_accepted_projection(attested) as projection:
        incremental_rows = logical_export(projection.typed.connection)
    # Rebuild from Git at the attested revision, before moving the head. That
    # rebuilt snapshot must remain usable after a later Claim correction.
    assert not instance.claim_attestation_evidence_store().events()
    projection_root = instance.root / instance.descriptor.storage.projections
    shutil.rmtree(projection_root)
    projection_root.mkdir()
    instance = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert instance.accepted_coordinate() == attested
    with instance.bind_accepted_projection(attested) as projection:
        assert logical_export(projection.typed.connection) == incremental_rows
    claim_file = claim_path(claim_id)
    prior = parse_claim(tree[claim_file], path=claim_file)
    successor = prior.model_copy(
        update={
            "statement": prior.statement.model_copy(update={"qualifier": "corrected"}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_artifact_digest(prior).tagged),
        }
    )
    tree = instance.tree_at(attested.git_oid)
    tree[claim_file] = render_claim(successor)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:02:00.000000Z", proposal_name="correct"
    )
    corrected = instance.accepted_coordinate()
    assert not service_evaluate_playbill_claim_verdict(
        instance, claim_identity=claim_id, evaluation_time=RECORDED_AT
    ).verdict.contradicting_evidence_digests
    assert instance.blob_at(corrected.git_oid, path) == render_accepted_attestation(envelope)
    reproduced = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=claim_id,
        evaluation_time=RECORDED_AT,
        at=AcceptedCoordinate.from_internal(attested),
    )
    assert reproduced == old_verdict
    with instance.bind_accepted_projection(corrected) as projection:
        assert projection.typed.claim_attestations(current_claims_only=True) == ()
        assert projection.typed.claim_attestations(
            prior.identity.qualified, claim_artifact_digest(prior).tagged
        ) == (envelope,)
        assert (
            projection.typed.claim_attestations(
                prior.identity.qualified, claim_artifact_digest(successor).tagged
            )
            == ()
        )
        assert (
            projection.typed.dependency_state(attestation_identity(envelope).qualified).pins == ()
        )


def test_new_kind_does_not_change_previous_compiler_registration() -> None:
    import pytest

    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_core.compiler.compiler import (
        P2_B5_COMPILER,
        artifact_kinds_for_compiler,
        current_compiler_coordinate,
        projection_registry_for_compiler,
    )

    path = attestation_path("sha256:" + "a" * 64)
    with pytest.raises(ProjectionFormatError):
        artifact_kinds_for_compiler(P2_B5_COMPILER).resolve_path(path)
    assert (
        artifact_kinds_for_compiler(current_compiler_coordinate()).resolve_path(path)
        == "attestation"
    )
    assert not projection_registry_for_compiler(P2_B5_COMPILER).supports_artifact_kind(
        "attestation"
    )
    assert projection_registry_for_compiler(current_compiler_coordinate()).supports_artifact_kind(
        "attestation"
    )


def test_new_capture_acceptance_uses_historical_binding_and_discovery(tmp_path: Path) -> None:
    from cruxible_client.contracts.claims import claim_path
    from cruxible_core.coverage.contracts import CoverageAccessProfileV1
    from cruxible_core.service.discovery.next import PlaybillNextRequestV1, service_playbill_next
    from tests.test_claims.test_claim_attestation_service import (
        RECORDED_AT,
        _coordinator_new_capture,
    )

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    before = instance.accepted_coordinate()
    capture = _coordinator_new_capture(instance, claim_id)
    envelope = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        basis="new_capture",
        captures=(capture.capture_digest,),
        stance="support",
    ).attestation
    # Advance accepted state after signing, without changing the bound Claim.
    other = _request(instance, owner, claim_id, tmp_path, stance="unsure").attestation
    tree = instance.tree_at(before.git_oid)
    tree[attestation_path(claim_attestation_v2_envelope_digest(other))] = (
        render_accepted_attestation(other)
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name="advance"
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    digest = claim_attestation_v2_envelope_digest(envelope)
    tree[attestation_path(digest)] = render_accepted_attestation(envelope)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:02:00.000000Z", proposal_name="new-evidence"
    )
    after = instance.accepted_coordinate()
    assert not instance.claim_attestation_evidence_store().events()
    assert instance.blob_at(after.git_oid, claim_path(claim_id)) == instance.blob_at(
        before.git_oid, claim_path(claim_id)
    )
    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=RECORDED_AT,
            access_profile=CoverageAccessProfileV1(
                profile_id="test", permitted_access_classes=("instance", "public")
            ),
        ),
    )
    rows = [item for item in result.items if item.reason == "claim_new_evidence_supporting"]
    assert len(rows) == 1
    assert rows[0].detail["attestation_envelope_digest"] == digest
    assert rows[0].detail["attestation_event_digest"] is None
    assert rows[0].detail["capture_digest"] == capture.capture_digest
    with instance.bind_accepted_projection(after) as projection:
        assert projection.typed.claim_attestations(basis="new_capture") == (envelope,)


def test_accepted_only_attestation_is_usable_by_procedure_measurement(tmp_path: Path) -> None:
    from tests.test_procedures.test_procedure_measurement_readings import (
        RUN_TIME,
        _accepted_claim,
        _measure,
        _rows,
        _world,
    )

    instance, owner, procedure = _world(tmp_path)
    _, claim = _accepted_claim(instance)
    envelope = _request(
        instance, owner, claim.identity.name, tmp_path, stance="support", attested_at=RUN_TIME
    ).attestation
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[attestation_path(claim_attestation_v2_envelope_digest(envelope))] = (
        render_accepted_attestation(envelope)
    )
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-08-24T16:01:00.000000Z",
        proposal_name="measurement-attestation",
    )
    assert not instance.claim_attestation_evidence_store().events()
    row = _rows(_measure(instance, procedure, names=("hot-arm-attested",)))["hot-arm-attested"]
    assert row.resolution is not None and row.resolution.verdict == "satisfied"
    assert row.resolution.value["count"] == 1
    assert row.resolution.value["items"][0]["source"] == "acceptance"
    assert row.resolution.value["items"][0][
        "attestation_digest"
    ] == claim_attestation_v2_envelope_digest(envelope)


def test_immutable_attestation_cannot_be_deleted_or_renamed(tmp_path: Path) -> None:
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    envelope = _request(instance, owner, claim_id, tmp_path).attestation
    path = attestation_path(claim_attestation_v2_envelope_digest(envelope))
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[path] = render_accepted_attestation(envelope)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name="immutable"
    )
    base = instance.accepted_coordinate()
    for action in ("delete", "rename"):
        tree = instance.tree_at(base.git_oid)
        content = tree.pop(path)
        if action == "rename":
            tree[attestation_path("sha256:" + "a" * 64)] = content
        result = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref=f"refs/proposals/owner/{action}", proposed_base_oid=base.git_oid
            ),
            candidate_tree=tree,
            timestamp="2026-08-28T15:02:00.000000Z",
        )
        assert result.candidate is None
        assert result.evaluation.diagnostics
        assert instance.accepted_coordinate() == base


def test_current_threshold_uses_signed_artifacts_not_embedded_v1_evidence(
    tmp_path, monkeypatch
) -> None:
    from tests.core_support._support import client_material
    from tests.test_evidence.test_attestation_consequence_next import (
        EVALUATION_TIME,
        _rows,
        threshold_world,
    )

    instance, owner, claim = threshold_world(tmp_path, monkeypatch, frozen=False)
    assert _rows(instance) == ()  # Old embedded V1 fixture evidence is ignored.
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    digests = []
    for signer in (owner, client_material(tmp_path, instance)):
        envelope = _request(
            instance,
            signer,
            claim.identity.name,
            tmp_path,
            stance="unsure",
            attested_at=EVALUATION_TIME,
        ).attestation
        digest = claim_attestation_v2_envelope_digest(envelope)
        digests.append(digest)
        tree[attestation_path(digest)] = render_accepted_attestation(envelope)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=EVALUATION_TIME.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        proposal_name="threshold-attestations",
    )
    rows = _rows(instance)
    assert len(rows) == 1
    assert rows[0].detail["attestation_digests"] == sorted(digests)
    assert rows[0].detail["independent_control_component_count"] == 2
    assert not instance.claim_attestation_evidence_store().events()


def test_current_claim_authoring_rejects_embedded_attestation_producer(tmp_path: Path) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claims import (
        claim_artifact_digest,
        claim_path,
        parse_claim,
        render_claim,
    )
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    path = claim_path(claim_id)
    prior = parse_claim(tree[path], path=path)
    successor = prior.model_copy(
        update={
            "backing": prior.backing.model_copy(
                update={"attestation_digests": ("sha256:" + "a" * 64,)}
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_artifact_digest(prior).tagged),
        }
    )
    tree[path] = render_claim(successor)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/old-producer", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-08-28T15:02:00.000000Z",
    )
    assert result.candidate is None
    assert any(
        d.code == "playbill.claim.embedded_attestations_retired"
        for d in result.evaluation.diagnostics
    )
