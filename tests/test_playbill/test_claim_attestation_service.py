from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
)
from cruxible_client.contracts.captures import (
    build_coordinator_self_source_capture,
    build_working_selection_capture,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationCaptureReferenceV1,
    ClaimAttestationStatementV2,
)
from cruxible_client.contracts.claims import (
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.signing import LocalEd25519ClaimAttestationSigner
from cruxible_core.service.playbill_claim_attestations import (
    ClaimAttestationRefusal,
    service_append_claim_attestation,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextRequestV2,
    service_playbill_next,
)
from tests.test_playbill.test_authoring_existing_capture import _activate, shared_capture_world
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world

RECORDED_AT = datetime(2026, 8, 28, 15, tzinfo=UTC)


def _request(
    instance,
    owner,
    claim_id: str,
    root: Path,
    *,
    basis: str = "examined_existing",
    stance: str = "support",
    captures: tuple[str, ...] | None = None,
    attested_at: datetime = RECORDED_AT,
    valid_until: datetime | None = None,
):  # type: ignore[no-untyped-def]
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    tree = instance.tree_at(coordinate.git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    subject_content = tree[claim.statement.subject.artifact_path]
    object_shell_digest = None
    if isinstance(claim.statement.object, SubjectClaimObject):
        object_path = claim.statement.object.address.artifact_path
        object_shell_digest = subject_digest(
            parse_subject(tree[object_path], path=object_path)
        ).tagged
    evidence_captures = tuple(
        sorted(
            {
                citation.capture_digest
                for citation in claim.backing.citations
                if citation.role == "evidence"
            },
            key=lambda item: item.encode("ascii"),
        )
    )
    cited = evidence_captures if captures is None else captures
    statement = ClaimAttestationStatementV2(
        instance_id=instance.descriptor.instance_id,
        referent_coordinate=coordinate,
        claim_identity=claim.identity,
        claim_artifact_digest=claim_artifact_digest(claim).tagged,
        claim_statement_digest=claim_statement_digest(claim.statement).tagged,
        subject_shell_digest=subject_digest(
            parse_subject(subject_content, path=claim.statement.subject.artifact_path)
        ).tagged,
        object_shell_digest=object_shell_digest,
        attesting_principal_id=owner.principal.principal_id,
        signing_key_digest=owner.principal.public_key_digest,
        attestation_basis=basis,
        stance=stance,
        cited_capture_digests=cited,
        attested_at=attested_at,
        valid_until=valid_until,
    )
    signer = LocalEd25519ClaimAttestationSigner.open(
        signer=owner.principal.principal_id,
        signing_key_id=owner.principal.public_key_digest,
        private_key_path=owner.private_key_path,
        expected_public_key=owner.principal.public_key,
        forbidden_roots=(root / "workspace", instance.root),
    )
    return ClaimAttestationAppendRequestV1(
        attestation=signer.sign_claim_attestation_v2(statement),
        capture_references=(
            tuple(ClaimAttestationCaptureReferenceV1(capture_digest=digest) for digest in cited)
            if basis == "new_capture"
            else ()
        ),
    )


def test_served_append_verifies_and_duplicate_is_an_identical_read(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)

    first = service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    retry = service_append_claim_attestation(
        instance,
        request=request.model_copy(update={"note": "ignored on duplicate"}),
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )

    assert retry == first
    assert len(instance.claim_attestation_evidence_store().events()) == 1
    assert first.recorded_head == first.current_head


def test_served_append_refuses_actor_relay_before_store_disclosure(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)

    with pytest.raises(ClaimAttestationRefusal) as error:
        service_append_claim_attestation(
            instance,
            request=request,
            actor_id="reviewer",
            recorded_at=RECORDED_AT,
        )

    assert error.value.error_code == "playbill.claim_attestation.actor_signer_mismatch"


def test_next_v2_reads_one_exact_evidence_head_while_v1_stays_legacy(
    tmp_path: Path,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    capture = build_coordinator_self_source_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        body=b"new supporting observation\n",
        observed_at=RECORDED_AT,
        accepted_coordinate=coordinate,
    )
    request = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        basis="new_capture",
        captures=(capture.capture_digest,),
    )
    appended = service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    access = CoverageAccessProfileV1(
        profile_id="attestation-door-test",
        permitted_access_classes=("instance", "public"),
    )

    legacy = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=RECORDED_AT,
            access_profile=access,
        ),
    )
    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV2(
            evaluation_time=RECORDED_AT,
            access_profile=access,
            at_attestation_head_digest=appended.current_head,
        ),
    )

    assert result.tag == "playbill-next-result-v2"
    assert result.attestation_head_digest == appended.current_head
    assert result == service_playbill_next(
        instance,
        request=PlaybillNextRequestV2(
            evaluation_time=RECORDED_AT,
            access_profile=access,
            at_attestation_head_digest=appended.current_head,
        ),
    )
    assert not any(item.reason == "claim_new_evidence_supporting" for item in legacy.items)
    rows = tuple(item for item in result.items if item.reason == "claim_new_evidence_supporting")
    assert len(rows) == 1
    assert set(rows[0].detail) == {
        "claim_id",
        "claim_artifact_digest",
        "capture_digest",
        "attestation_event_digest",
        "attestation_basis",
        "stance",
        "attesting_principal",
        "current_at_append",
        "lineage_status",
    }
    assert rows[0].detail["lineage_status"] == "proven"
    assert rows[0].repair.command.endswith(
        f"--claim-id {claim_id} --capture-digest {capture.capture_digest}"
    )


def _assert_successor_resolves_attestation_membership(
    tmp_path: Path,
    *,
    stance: str,
    reason: str,
) -> None:
    (
        instance,
        owner,
        _actor,
        _first_claim_id,
        claim_id,
        _coordinator,
        shared_payload,
    ) = shared_capture_world(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    selected = b"new evidence for the accepted claim"
    source = instance.body_store().store(selected)
    capture = build_working_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        rationale="A new independent source observation awaits adjudication.",
        observed_at=RECORDED_AT,
        accepted_coordinate=coordinate,
        source_id="repo.work-items",
        coordinate={
            "source_byte_length": len(selected),
            "source_content_digest": source.digest,
        },
        selector={"anchor": "new evidence", "start_byte": 0, "end_byte": len(selected)},
        selected_content=selected,
    )
    request = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        basis="new_capture",
        stance=stance,
        captures=(capture.capture_digest,),
    )
    service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    access = CoverageAccessProfileV1(
        profile_id="attestation-door-resolution",
        permitted_access_classes=("instance", "public"),
    )

    def rows() -> tuple:  # type: ignore[no-untyped-def]
        return tuple(
            item
            for item in service_playbill_next(
                instance,
                request=PlaybillNextRequestV2(
                    evaluation_time=RECORDED_AT,
                    access_profile=access,
                ),
            ).items
            if item.reason == reason
        )

    def cite(role: str, timestamp: str) -> None:
        coordinator = AuthoringIntentCoordinator(
            instance=instance,
            store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        )
        payload = ClaimAuthoringPayloadV3(
            statement=shared_payload.statement,
            rationale="Adjudicate the newly observed Capture through the shipped citation path.",
            source=ExistingCaptureCitationSourceV1(capture_digest=capture.capture_digest),
            citation_role=role,  # type: ignore[arg-type]
            claim_ref=claim_id,
            dependency_drafts=ClaimDependencyDraftsV1(),
        )
        actor = AuthenticatedActor(actor_id="owner")
        intent = coordinator.create(
            actor=actor,
            payload=payload,
            canonical_timestamp=timestamp,
        ).intent
        submitted = coordinator.submit(intent.intent_id, actor=actor)
        assert submitted.status.proposal_id is not None, (
            None
            if submitted.intent.last_preflight is None
            else submitted.intent.last_preflight.frontier.diagnostics
        )
        _activate(instance, submitted)
        instance.refresh()

    assert len(rows()) == 1
    cite("copy", "2026-08-28T15:01:00.000000Z")
    assert len(rows()) == 1
    cite("evidence", "2026-08-28T15:02:00.000000Z")
    assert rows() == ()


def test_copy_successor_keeps_membership_and_evidence_successor_resolves_it(
    tmp_path: Path,
) -> None:
    _assert_successor_resolves_attestation_membership(
        tmp_path,
        stance="contradict",
        reason="claim_contradicting_evidence_available",
    )
