"""G8b reverse drift: explicit published copies remain judgments, never edits."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    claim_artifact_digest,
    claim_path,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.declared_blocks import PlaybillPresentationPolicyV1
from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest
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
    PlaybillNextSourceObservationV2,
    PlaybillNextWorkspaceObservationV1,
    _self_published_source_items,
    _SourceAssociation,
    service_playbill_next,
)
from tests.test_playbill.test_authoring_insertions import (
    _activate,
    _observation,
    _submitted_insertion,
)

NOW = datetime(2026, 8, 21, 13, tzinfo=UTC)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _published_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner, coordinator, actor, intent_id = _submitted_insertion(tmp_path)
    expectation = coordinator.resume(intent_id, actor=actor).intent.insertion_expectation
    assert expectation is not None
    observation = _observation(expectation.expectation_id)
    pending = coordinator.confirm_insertion(intent_id, actor=actor, observation=observation)
    assert pending.successor_status is not None
    assert pending.successor_status.proposal_id is not None
    assert pending.successor_status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=pending.successor_status.proposal_id,
        candidate_digest=pending.successor_status.candidate_digest,
    )
    bound = coordinator.confirm_insertion(intent_id, actor=actor, observation=observation)
    assert bound.outcome == "bound"
    return instance, owner, bound.intent.semantic_identity


def _retire(instance, owner, claim_id: str) -> None:  # type: ignore[no-untyped-def]
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


def _request(instance, *, archival: bool = False) -> PlaybillNextRequestV1:  # type: ignore[no-untyped-def]
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
            profile_id="reverse-drift-test",
            permitted_access_classes=("instance", "public"),
        ),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            source_observations=(
                PlaybillNextSourceObservationV2(
                    tag="playbill-next-source-observation-v2",
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


def _reverse_items(
    monkeypatch,  # type: ignore[no-untyped-def]
    request: PlaybillNextRequestV1,
    associations: tuple[_SourceAssociation, ...],
):  # type: ignore[no-untyped-def]
    assert request.at is not None
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._source_associations",
        lambda *_args, **_kwargs: associations,
    )
    return _self_published_source_items(
        object(),  # type: ignore[arg-type]
        coordinate=request.at,
        evaluation_time=request.evaluation_time,
        access_profile=request.access_profile,
        observation=request.workspace_observation,
    )


def _association(
    *,
    citation_id: str = "sha256:" + "1" * 64,
    claim_identity: str = "Claim:CLM-11111111111111111111111111111111",
    source_id: str = "repo.work-items",
    qualifying: bool = True,
    stale: bool = True,
) -> _SourceAssociation:
    return _SourceAssociation(
        citation_id=citation_id,
        claim_identity=claim_identity,
        commitment_digest=_digest(b"ready"),
        source_id=source_id,
        qualifying_publication=qualifying,
        stale_publication=stale,
    )


def test_retired_sole_self_published_copy_yields_one_deterministic_judgment(
    tmp_path: Path,
) -> None:
    instance, owner, claim_id = _published_world(tmp_path)
    assert not [
        item
        for item in service_playbill_next(instance, request=_request(instance)).items
        if item.reason == "self_published_source_stale"
    ]
    _retire(instance, owner, claim_id)

    first = tuple(
        item
        for item in service_playbill_next(instance, request=_request(instance)).items
        if item.reason == "self_published_source_stale"
    )
    repeat = tuple(
        item
        for item in service_playbill_next(instance, request=_request(instance)).items
        if item.reason == "self_published_source_stale"
    )

    assert first == repeat
    assert len(first) == 1
    (item,) = first
    assert item.severity == "warning"
    assert item.subject_identity == "repo.work-items"
    assert item.repair.operation == "playbill.authoring.create"
    assert item.repair.required_change == "review_self_published_passage"
    assert item.detail["occurrence_identity_digest"].startswith("sha256:")

    assert not [
        item
        for item in service_playbill_next(instance, request=_request(instance, archival=True)).items
        if item.reason == "self_published_source_stale"
    ]


def test_invalid_presentation_policy_fails_closed_only_for_reverse_drift(
    tmp_path: Path,
) -> None:
    instance, owner, claim_id = _published_world(tmp_path)
    _retire(instance, owner, claim_id)
    request = _request(instance)
    assert request.workspace_observation is not None
    request = request.model_copy(
        update={
            "workspace_observation": request.workspace_observation.model_copy(
                update={
                    "floor_status": "missing",
                    "presentation_policy": None,
                    "presentation_policy_notes": ("presentation_policy_malformed",),
                }
            )
        }
    )

    rows = service_playbill_next(instance, request=request).items

    assert not [item for item in rows if item.reason == "self_published_source_stale"]
    assert [item for item in rows if item.reason == "floor_missing"]


def test_contradicted_sole_copy_is_stale_at_the_explicit_evaluation_instant(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner, _claim_id = _published_world(tmp_path)
    observed_times: list[datetime] = []

    def contradicted(*_args, evaluation_time: datetime, **_kwargs):  # type: ignore[no-untyped-def]
        observed_times.append(evaluation_time)
        return SimpleNamespace(verdict=SimpleNamespace(verdict="contradicted"))

    monkeypatch.setattr(
        "cruxible_core.service.playbill_next.service_evaluate_playbill_claim_verdict",
        contradicted,
    )

    rows = service_playbill_next(instance, request=_request(instance)).items

    assert observed_times
    assert set(observed_times) == {NOW}
    assert len([item for item in rows if item.reason == "self_published_source_stale"]) == 1


def test_independent_or_legacy_association_suppresses_a_stale_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner, _claim_id = _published_world(tmp_path)
    request = _request(instance)

    rows = _reverse_items(
        monkeypatch,
        request,
        (
            _association(),
            _association(
                citation_id="sha256:" + "2" * 64,
                claim_identity="Claim:CLM-22222222222222222222222222222222",
                qualifying=False,
                stale=False,
            ),
        ),
    )

    assert rows == ()


def test_one_current_publisher_suppresses_a_retired_publisher(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner, _claim_id = _published_world(tmp_path)
    request = _request(instance)

    rows = _reverse_items(
        monkeypatch,
        request,
        (
            _association(),
            _association(
                citation_id="sha256:" + "2" * 64,
                claim_identity="Claim:CLM-22222222222222222222222222222222",
                stale=False,
            ),
        ),
    )

    assert rows == ()


def test_ambiguous_duplicate_occurrence_suppresses_reverse_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner, _claim_id = _published_world(tmp_path)
    request = _request(instance)
    assert request.workspace_observation is not None
    (source,) = request.workspace_observation.source_observations or ()
    assert isinstance(source, PlaybillNextSourceObservationV2)
    first = source.occurrences[0]
    duplicate = first.model_copy(
        update={
            "ordinal": 1,
            "identity_digest": occurrence_identity_digest(
                source=first.source,
                observed_commitment_digest=first.observed_commitment_digest,
                ordinal=1,
            ),
        }
    )
    observation = request.workspace_observation.model_copy(
        update={
            "source_observations": (source.model_copy(update={"occurrences": (first, duplicate)}),)
        }
    )

    rows = _reverse_items(
        monkeypatch,
        request.model_copy(update={"workspace_observation": observation}),
        (_association(),),
    )

    assert rows == ()


def test_declared_block_overlap_suppresses_reverse_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner, _claim_id = _published_world(tmp_path)
    request = _request(instance)
    assert request.workspace_observation is not None
    (source,) = request.workspace_observation.source_observations or ()
    assert isinstance(source, PlaybillNextSourceObservationV2)
    marker = SimpleNamespace(start_byte=7, end_byte=10)
    observation = request.workspace_observation.model_copy(
        update={"source_observations": (source.model_copy(update={"marker_summaries": (marker,)}),)}
    )

    rows = _reverse_items(
        monkeypatch,
        request.model_copy(update={"workspace_observation": observation}),
        (_association(),),
    )

    assert rows == ()


def test_identical_bytes_in_another_source_do_not_poison_the_publication_group(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner, _claim_id = _published_world(tmp_path)
    request = _request(instance)

    rows = _reverse_items(
        monkeypatch,
        request,
        (
            _association(),
            _association(
                citation_id="sha256:" + "2" * 64,
                claim_identity="Claim:CLM-22222222222222222222222222222222",
                source_id="repo.other",
                qualifying=False,
                stale=False,
            ),
        ),
    )

    assert len(rows) == 1
    assert rows[0].subject_identity == "repo.work-items"
