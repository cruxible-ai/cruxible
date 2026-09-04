"""The published-copy world several `next` suites are written against.

It lived in the reverse-drift tests, whose subject -- a `next` row for a stale
`self_published` copy -- went with that citation origin: no code path in the
product ever wrote it, so the row it fed could never fire on anything a user
could produce. The WORLD is still worth having, and every other suite that
borrowed it is about something real, so it moves here and publishes an ordinary
independent copy instead.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    build_working_selection_capture,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    ClaimBackingV2,
    build_claim_citation,
    claim_artifact_digest,
    claim_path,
    claim_statement_address,
    merge_claim_citations,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.declared_blocks import PlaybillPresentationPolicyV1
from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest
from cruxible_client.contracts.semantic import ContentSpan, SourceMapping
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageLineOverlayV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.coverage.indexes import (
    WorkingOccurrenceV1,
    occurrence_identity_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationV1,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_insertions_v2 import (
    _activate,
)
from tests.test_playbill.test_authoring_preflight import (
    _seed_claim_surface,
    _working_payload,
)

NOW = datetime(2026, 8, 21, 13, tzinfo=UTC)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def publish_copy_claim(
    instance,  # type: ignore[no-untyped-def]
    owner: object,
    *,
    timestamp: str,
    successor_timestamp: str,
    proposal_suffix: str,
) -> str:
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _working_payload(occurrence_count=1).model_copy(
        update={
            "citation_role": "copy",
            "rationale": f"Published-copy fixture {proposal_suffix}.",
        }
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=timestamp,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )

    base = instance.accepted_coordinate()
    path = claim_path(intent.semantic_identity)
    tree = instance.tree_at(base.git_oid)
    claim = parse_claim(tree[path], path=path)
    assert isinstance(claim, ClaimArtifactV2)
    assert isinstance(claim.backing, ClaimBackingV2)
    selected = b"ready"
    selected_digest = _digest(selected)
    observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    built = build_working_selection_capture(
        store=instance.body_store(),
        actor_id=actor.actor_id,
        claim_id=intent.semantic_identity,
        rationale="Test fixture for an explicit published copy.",
        observed_at=observed_at,
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
        source_id="repo.work-items",
        coordinate={
            "kind": "observed_digest",
            "source_content_digest": selected_digest,
            "source_byte_length": len(selected),
        },
        selector={
            "anchor": "ready",
            "start_byte": 0,
            "end_byte": len(selected),
            "observed_occurrence_count": 1,
        },
        selected_content=selected,
    )
    published = build_claim_citation(
        claim.identity,
        capture_digest=built.capture_digest,
        role="copy",
        origin="independent",
    )
    mapping = SourceMapping(
        subject=claim_statement_address(path),
        spans=(
            ContentSpan(
                content_digest=built.source_body_digest,
                start_byte=0,
                end_byte=len(selected),
            ),
        ),
    )
    mappings = {
        canonical_bytes(item.model_dump(mode="json")): item
        for item in (*claim.backing.source_mappings, mapping)
    }
    successor = claim.model_copy(
        update={
            "backing": claim.backing.model_copy(
                update={
                    "capture_digests": tuple(
                        sorted(
                            {*claim.backing.capture_digests, built.capture_digest},
                            key=lambda item: item.encode("ascii"),
                        )
                    ),
                    "citations": merge_claim_citations(
                        claim.backing.citations,
                        (published,),
                    ),
                    "source_mappings": tuple(mappings[key] for key in sorted(mappings)),
                }
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_artifact_digest(claim).tagged),
        }
    )
    tree[path] = render_claim(successor)
    promoted = instance.proposal_service().submit(
        actor=actor,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{proposal_suffix}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=successor_timestamp,
    )
    assert promoted.evaluation.candidate_digest is not None, promoted.evaluation.diagnostics
    _activate(
        instance,
        owner,
        proposal_id=promoted.admission.proposal_id,
        candidate_digest=promoted.evaluation.candidate_digest,
    )
    return intent.semantic_identity


def published_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract("repo.work-items"),
    )
    claim_id = publish_copy_claim(
        instance,
        owner,
        timestamp="2026-08-21T12:00:00.000000Z",
        successor_timestamp="2026-08-21T12:00:01.000000Z",
        proposal_suffix="mark-published-copy",
    )
    return instance, owner, claim_id


def retire_claim(instance, owner, claim_id: str) -> None:  # type: ignore[no-untyped-def]
    base = instance.accepted_coordinate()
    path = claim_path(claim_id)
    tree = instance.tree_at(base.git_oid)
    claim = parse_claim(tree[path], path=path)
    assert isinstance(claim, ClaimArtifactV2)
    retired = claim.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=claim_artifact_digest(claim).tagged,
            )
        }
    )
    tree[path] = render_claim(retired)
    submitted = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/retire-published-copy",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-21T12:01:00.000000Z",
    )
    assert submitted.evaluation.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.admission.proposal_id,
        candidate_digest=submitted.evaluation.candidate_digest,
    )


def next_request(instance, *, archival: bool = False) -> PlaybillNextRequestV1:  # type: ignore[no-untyped-def]
    source = LogicalSourceIdentityV1(plane="external", identity="repo.work-items")
    commitment = _digest(b"ready")
    occurrence = WorkingOccurrenceV1(
        source=source,
        observed_commitment_digest=commitment,
        byte_length=5,
        ordinal=0,
        identity_digest=occurrence_identity_digest(
            source=source,
            observed_commitment_digest=commitment,
            ordinal=0,
        ),
        line_overlay=CoverageLineOverlayV1(
            start_byte=8,
            end_byte=13,
            start_line=1,
            end_line=1,
        ),
    )
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    return PlaybillNextRequestV1(
        at=coordinate,
        evaluation_time=NOW,
        access_profile=CoverageAccessProfileV1(
            profile_id="published-copy-world",
            permitted_access_classes=("instance", "public"),
        ),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            source_observations=(
                PlaybillNextSourceObservationV3(
                    tag="playbill-next-source-observation-v3",
                    source_id="repo.work-items",
                    observed_source_digest=_digest(b"status: ready"),
                    byte_length=13,
                    marker_summaries=(),
                    occurrences=(occurrence,),
                    scanned_commitment_digests=(commitment,),
                    scan_complete=True,
                    scan_notes=(),
                    marker_notes=(),
                ),
            ),
            presentation_policy=PlaybillPresentationPolicyV1(
                archival_source_ids=("repo.work-items",) if archival else ()
            ),
        ),
    )
