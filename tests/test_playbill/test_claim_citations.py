"""Claim-v2 citation identity, wire succession, and legacy interpretation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_core.playbill.candidates import CandidateRecordV3
from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    DirectByteSpanSelectionV1,
    build_direct_claim_capture,
    build_direct_claim_selection_capture,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_core.playbill.claim_types import render_claim_type
from cruxible_core.playbill.claims import (
    ClaimArtifact,
    ClaimArtifactV2,
    ClaimBackingV2,
    ClaimLawEvidenceV1,
    LegacyCitationReferenceV1,
    build_claim_citation,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    merge_claim_citations,
    parse_claim,
    render_claim,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.semantic import ContentSpan
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.playbill.subjects import render_subject, subject_path
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim, _claim_type, _subject

CLAIM_ID = "CLM-0123456789abcdef0123456789abcdef"
CAPTURE_DIGEST = "sha256:" + "ab" * 32
SOURCE_DIGEST = "sha256:" + "cd" * 32


def _v1_claim() -> ClaimArtifact:
    return _claim(
        claim_id=CLAIM_ID,
        capture_digest=CAPTURE_DIGEST,
        source_digest=SOURCE_DIGEST,
        source_length=12,
    )


def _v2_claim(*, role: str = "evidence", origin: str = "independent") -> ClaimArtifactV2:
    legacy = _v1_claim()
    citation = build_claim_citation(
        legacy.identity,
        capture_digest=CAPTURE_DIGEST,
        role=role,  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
    )
    return ClaimArtifactV2(
        identity=legacy.identity,
        statement=legacy.statement,
        backing=ClaimBackingV2(
            referent_context=legacy.backing.referent_context,
            capture_digests=legacy.backing.capture_digests,
            citations=(citation,),
            source_mappings=legacy.backing.source_mappings,
        ),
        authority=legacy.authority,
        pins=legacy.pins,
    )


def test_citation_id_uses_the_exact_frozen_preimage_and_retry_is_idempotent() -> None:
    claim = _v2_claim()
    citation = claim.backing.citations[0]
    expected = typed_digest(
        Sha256Value,
        "playbill-claim-citation-v1",
        {
            "claim_identity": {"kind": "Claim", "name": CLAIM_ID},
            "capture_digest": CAPTURE_DIGEST,
            "origin": "independent",
            "role": "evidence",
        },
    ).tagged

    assert citation.citation_id == expected
    assert merge_claim_citations((citation,), (citation,)) == (citation,)


def test_claim_v2_round_trips_and_rejects_a_forged_citation_id() -> None:
    claim = _v2_claim()

    assert parse_claim(render_claim(claim), path=claim_path(CLAIM_ID)) == claim
    with pytest.raises(ValidationError, match="citation ID does not reproduce"):
        ClaimArtifactV2.model_validate(
            claim.model_copy(
                update={
                    "backing": claim.backing.model_copy(
                        update={
                            "citations": (
                                claim.backing.citations[0].model_copy(
                                    update={"citation_id": "sha256:" + "00" * 32}
                                ),
                            )
                        }
                    )
                }
            ).model_dump(mode="json")
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        claim.backing.citations[0].__class__.model_validate(
            {
                **claim.backing.citations[0].model_dump(mode="json"),
                "observation_trust": "provider_receipted",
            }
        )


def test_legacy_reference_is_derived_without_fabricating_role_or_origin() -> None:
    legacy = _v1_claim()
    references = claim_citation_references(legacy)

    assert len(references) == 1
    reference = references[0]
    assert isinstance(reference, LegacyCitationReferenceV1)
    assert reference.legacy_semantics is True
    assert reference.model_dump(mode="json") == {
        "tag": "playbill-legacy-claim-citation-v1",
        "citation_id": typed_digest(
            Sha256Value,
            "playbill-legacy-claim-citation-v1",
            {
                "claim_identity": {"kind": "Claim", "name": CLAIM_ID},
                "capture_digest": CAPTURE_DIGEST,
            },
        ).tagged,
        "claim_identity": {"kind": "Claim", "name": CLAIM_ID},
        "capture_digest": CAPTURE_DIGEST,
        "legacy_semantics": True,
    }


def test_v1_to_v2_wire_succession_keeps_legacy_capture_implicit() -> None:
    legacy = _v1_claim()
    successor = ClaimArtifactV2(
        identity=legacy.identity,
        statement=legacy.statement,
        backing=ClaimBackingV2(
            referent_context=legacy.backing.referent_context.model_copy(
                update={"observed_at": datetime(2026, 8, 20, tzinfo=timezone.utc)}
            ),
            capture_digests=legacy.backing.capture_digests,
            citations=(),
            source_mappings=legacy.backing.source_mappings,
        ),
        authority=legacy.authority,
        pins=legacy.pins,
        lifecycle=ArtifactLifecycle(predecessor_digest=claim_artifact_digest(legacy).tagged),
    )

    references = claim_citation_references(successor)
    assert len(references) == 1
    assert isinstance(references[0], LegacyCitationReferenceV1)
    assert parse_claim(render_claim(successor), path=claim_path(CLAIM_ID)) == successor


def test_one_capture_can_have_different_roles_on_different_claims() -> None:
    evidence = _v2_claim(role="evidence")
    other_identity = ArtifactIdentity(kind="Claim", name="CLM-abcdefabcdefabcdefabcdefabcdefab")
    copy = build_claim_citation(
        other_identity,
        capture_digest=CAPTURE_DIGEST,
        role="copy",
        origin="independent",
    )

    assert evidence.backing.citations[0].capture_digest == copy.capture_digest
    assert evidence.backing.citations[0].citation_id != copy.citation_id
    assert evidence.backing.citations[0].role == "evidence"
    assert copy.role == "copy"


def test_mixed_wire_succession_is_deterministic_and_citations_are_append_only(
    tmp_path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    first_capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=CLAIM_ID,
        value="ready",
        rationale="initial v1 evidence",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
    )
    assert first_capture.envelope.commitment.byte_length is not None
    legacy = _claim(
        claim_id=CLAIM_ID,
        capture_digest=first_capture.capture_digest,
        source_digest=first_capture.source_body_digest,
        source_length=first_capture.envelope.commitment.byte_length,
    )
    shell = _subject()
    claim_type = _claim_type()
    initial_tree = {
        **instance.tree_at(base.git_oid),
        subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
        "claim-types/project.work_item/status.yaml": render_claim_type(claim_type),
        capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
            render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
        ),
        claim_path(CLAIM_ID): render_claim(legacy),
    }
    initial = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/citation-v1",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=initial_tree,
        timestamp="2026-08-20T12:00:00.000000Z",
    )
    assert isinstance(initial.candidate, CandidateRecordV3)
    _activate(instance, owner, initial.candidate, initial.evaluation.evaluated_tree_oid, sequence=1)

    accepted_v1 = instance.accepted_coordinate()
    second_capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=CLAIM_ID,
        value="ready",
        rationale="new v2 backing",
        observed_at=datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(accepted_v1),
    )
    citation = build_claim_citation(
        legacy.identity,
        capture_digest=second_capture.capture_digest,
        role="evidence",
        origin="self_source",
    )
    substrate = b"status: ready\n"
    substrate_digest = instance.body_store().store(substrate).digest
    selection_capture = build_direct_claim_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=CLAIM_ID,
        rationale="existing source copy",
        observed_at=datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(accepted_v1),
        selection=DirectByteSpanSelectionV1(
            span=ContentSpan(
                content_digest=substrate_digest,
                start_byte=0,
                end_byte=len(substrate),
            )
        ),
    )
    copy_citation = build_claim_citation(
        legacy.identity,
        capture_digest=selection_capture.capture_digest,
        role="copy",
        origin="independent",
    )
    v2 = ClaimArtifactV2(
        identity=legacy.identity,
        statement=legacy.statement,
        backing=ClaimBackingV2(
            referent_context=legacy.backing.referent_context.model_copy(
                update={"observed_at": datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc)}
            ),
            capture_digests=tuple(
                sorted(
                    (
                        first_capture.capture_digest,
                        second_capture.capture_digest,
                        selection_capture.capture_digest,
                    )
                )
            ),
            citations=merge_claim_citations((citation,), (copy_citation,)),
            source_mappings=legacy.backing.source_mappings,
        ),
        authority=legacy.authority,
        pins=legacy.pins,
        lifecycle=ArtifactLifecycle(predecessor_digest=claim_artifact_digest(legacy).tagged),
    )
    successor_tree = {
        **instance.tree_at(accepted_v1.git_oid),
        claim_path(CLAIM_ID): render_claim(v2),
    }
    forged_origin = v2.model_copy(
        update={
            "backing": v2.backing.model_copy(
                update={
                    "citations": merge_claim_citations(
                        (
                            build_claim_citation(
                                legacy.identity,
                                capture_digest=second_capture.capture_digest,
                                role="copy",
                                origin="independent",
                            ),
                        ),
                        (copy_citation,),
                    )
                }
            )
        }
    )
    forged = _submit(
        instance,
        {
            **instance.tree_at(accepted_v1.git_oid),
            claim_path(CLAIM_ID): render_claim(forged_origin),
        },
        accepted_v1.git_oid,
        "citation-v2-forged-origin",
        "2026-08-20T12:01:00.000000Z",
    )
    assert {item.code for item in forged.evaluation.diagnostics} == {
        "playbill.claim.self_source_origin_mismatch"
    }
    first_evaluation = _submit(
        instance,
        successor_tree,
        accepted_v1.git_oid,
        "citation-v2-a",
        "2026-08-20T12:01:00.000000Z",
    )
    second_evaluation = _submit(
        instance,
        successor_tree,
        accepted_v1.git_oid,
        "citation-v2-b",
        "2026-08-20T12:01:00.000000Z",
    )
    assert first_evaluation.evaluation.diagnostics == second_evaluation.evaluation.diagnostics == ()
    assert first_evaluation.candidate is not None
    assert second_evaluation.candidate is not None
    assert first_evaluation.candidate.law_evidence == second_evaluation.candidate.law_evidence
    initial_evidence = _claim_evidence(initial.candidate)
    successor_evidence = _claim_evidence(first_evaluation.candidate)
    assert tuple(item.capture_digest for item in successor_evidence.verdict_captures) == (
        first_capture.capture_digest,
    )
    assert successor_evidence.evidence_basis == initial_evidence.evidence_basis
    assert successor_evidence.verdict_result is not None
    assert initial_evidence.verdict_result is not None
    assert successor_evidence.verdict_result.supporting_evidence_digests == (
        initial_evidence.verdict_result.supporting_evidence_digests
    )
    assert successor_evidence.verdict_result.provenance_grades == (
        initial_evidence.verdict_result.provenance_grades
    )
    assert successor_evidence.verdict_result.control_components == (
        initial_evidence.verdict_result.control_components
    )
    _activate(
        instance,
        owner,
        first_evaluation.candidate,
        first_evaluation.evaluation.evaluated_tree_oid,
        sequence=2,
    )

    accepted_v2 = instance.accepted_coordinate()
    dropped = v2.model_copy(
        update={
            "backing": v2.backing.model_copy(update={"citations": ()}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_artifact_digest(v2).tagged),
        }
    )
    refused = _submit(
        instance,
        {**instance.tree_at(accepted_v2.git_oid), claim_path(CLAIM_ID): render_claim(dropped)},
        accepted_v2.git_oid,
        "citation-v2-drop",
        "2026-08-20T12:02:00.000000Z",
    )
    assert {item.code for item in refused.evaluation.diagnostics} == {
        "playbill.claim.legacy_capture_set_changed"
    }


def _submit(instance, tree, base_oid: str, name: str, timestamp: str):
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}",
            proposed_base_oid=base_oid,
        ),
        candidate_tree=tree,
        timestamp=timestamp,
    )


def _activate(instance, owner, candidate, evaluated_oid, *, sequence: int) -> None:
    assert evaluated_oid is not None
    base = instance.accepted_coordinate()
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(_sign(owner, candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=sequence,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def _claim_evidence(candidate: CandidateRecordV3) -> ClaimLawEvidenceV1:
    return ClaimLawEvidenceV1.model_validate(
        next(
            item.result["claim_evidence"]
            for item in candidate.law_evidence
            if item.path == claim_path(CLAIM_ID)
        )
    )
