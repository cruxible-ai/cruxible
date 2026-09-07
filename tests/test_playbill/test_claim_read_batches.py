"""Batch reads retain single-read semantics and bounded selection at one coordinate."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.claim_reads import ClaimBackingsRequestV1, ClaimReadBatchRequestV1
from cruxible_client.contracts.errors import ClaimNotFoundError, PlaybillFormatError
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claim_reads import (
    service_read_claim_backings,
    service_read_claim_batch,
)
from cruxible_core.service.playbill_claims import service_get_playbill_claim
from tests.test_playbill._knowledge_loop_support import seed_claims

TIME = datetime(2026, 8, 21, 14, tzinfo=UTC)
PATHS = ("subjects/project.work_item/wi-42.json", "subjects/project.work_item/wi-43.json")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    instance, _ = seed_claims(tmp_path_factory.mktemp("batch-claims"))
    return instance


def request(instance, **kwargs):
    return ClaimReadBatchRequestV1(
        at=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(),
        evaluation_time=TIME,
        **kwargs,
    )


def test_selected_pages_equal_single_views_and_bind_once(seeded, monkeypatch):
    from cruxible_core.storage.playbill_projection import ProjectionHandle

    def forbidden(*args, **kwargs):
        raise AssertionError("batch must not materialize the Claim population")

    monkeypatch.setattr(ProjectionHandle, "list_claims", forbidden)
    req = request(seeded, subject_paths=PATHS, limit=1)
    first = service_read_claim_batch(seeded, request=req)
    assert first.truncated and first.cursor
    second = service_read_claim_batch(
        seeded, request=req.model_copy(update={"cursor": first.cursor})
    )
    assert not second.truncated and second.cursor is None
    assert len(first.claims) == len(second.claims) == 1
    selected = first.claims + second.claims
    assert len({view.envelope["identity"] for view in selected}) == 2
    for view in selected:
        old = service_get_playbill_claim(
            seeded, identity=view.envelope["identity"], evaluation_time=TIME
        )
        assert view.model_dump(mode="json") == old.model_dump(mode="json")
    binds = 0
    original = seeded.bind_accepted_projection

    def counted(coordinate):
        nonlocal binds
        binds += 1
        return original(coordinate)

    monkeypatch.setattr(seeded, "bind_accepted_projection", counted)
    ids = tuple(view.envelope["identity"] for view in reversed(selected))
    all_views = service_read_claim_batch(seeded, request=request(seeded, claim_ids=ids))
    assert binds == 1
    assert all_views.claims == tuple(reversed(selected))


def test_cursor_cannot_be_reused_for_other_selection(seeded):
    req = request(seeded, subject_paths=PATHS, limit=1)
    first = service_read_claim_batch(seeded, request=req)
    changed = req.model_copy(update={"cursor": first.cursor, "predicates": ("other.status",)})
    with pytest.raises(PlaybillFormatError, match="cursor"):
        service_read_claim_batch(seeded, request=changed)
    with pytest.raises(PlaybillFormatError, match="cursor"):
        service_read_claim_batch(seeded, request=req.model_copy(update={"cursor": "invalid!"}))
    empty = service_read_claim_batch(
        seeded, request=req.model_copy(update={"predicates": ("absent",)})
    )
    assert empty.claims == () and not empty.truncated


def test_latest_batch_resolves_once_and_returns_the_resolved_coordinate(seeded, monkeypatch):
    from cruxible_core.service import playbill_claim_reads

    earlier = seeded.coordinate_for_oid(seeded.accepted_history()[-2].oid)
    latest = seeded.accepted_coordinate()
    coordinates = iter((earlier, latest))
    calls = []

    def resolve(instance, at):
        assert instance is seeded
        calls.append(at)
        return next(coordinates)

    monkeypatch.setattr(playbill_claim_reads, "_resolve_coordinate", resolve)
    req = ClaimReadBatchRequestV1(subject_paths=PATHS, evaluation_time=TIME)
    first = service_read_claim_batch(seeded, request=req)
    second = service_read_claim_batch(seeded, request=req)
    assert calls == [None, None]
    assert len(first.claims) == 1 and len(second.claims) == 2
    for result, coordinate in ((first, earlier), (second, latest)):
        assert (
            result.coordinate.model_dump()
            == PlaybillAcceptedCoordinate.from_internal(coordinate).model_dump()
        )
        assert all(view.coordinate == result.coordinate for view in result.claims)


def test_latest_page_continuation_is_bound_to_returned_coordinate(seeded, monkeypatch):
    from cruxible_core.service import playbill_claim_reads

    req = ClaimReadBatchRequestV1(subject_paths=PATHS, evaluation_time=TIME, limit=1)
    first = service_read_claim_batch(seeded, request=req)
    assert first.truncated and first.cursor
    original = playbill_claim_reads._resolve_coordinate

    def pinned_only(instance, at):
        assert at is not None, "a continuation must not discover a newer head"
        return original(instance, at)

    monkeypatch.setattr(playbill_claim_reads, "_resolve_coordinate", pinned_only)
    continuation = req.model_copy(update={"at": first.coordinate, "cursor": first.cursor})
    second = service_read_claim_batch(seeded, request=continuation)
    assert second.coordinate == first.coordinate
    assert not second.truncated
    assert first.claims[0].envelope["identity"] != second.claims[0].envelope["identity"]
    earlier = type(first.coordinate).model_validate(
        PlaybillAcceptedCoordinate.from_internal(
            seeded.coordinate_for_oid(seeded.accepted_history()[-2].oid)
        ).model_dump()
    )
    with pytest.raises(PlaybillFormatError, match="cursor"):
        service_read_claim_batch(seeded, request=continuation.model_copy(update={"at": earlier}))
    empty = type(first.coordinate).model_validate(
        PlaybillAcceptedCoordinate.from_internal(
            seeded.coordinate_for_oid(seeded.accepted_history()[0].oid)
        ).model_dump()
    )
    with pytest.raises(PlaybillFormatError, match="cursor"):
        service_read_claim_batch(seeded, request=continuation.model_copy(update={"at": empty}))


def test_cursor_without_coordinate_is_rejected_before_source_work(seeded, monkeypatch):
    from cruxible_core.service import playbill_claim_reads

    with pytest.raises(ValidationError, match="continuation requires"):
        ClaimReadBatchRequestV1(subject_paths=PATHS, cursor="cursor")

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid continuation must fail before resolving accepted state")

    monkeypatch.setattr(playbill_claim_reads, "_resolve_coordinate", forbidden)
    # Internal callers using model_copy/model_construct receive the same guard.
    req = ClaimReadBatchRequestV1(subject_paths=PATHS).model_copy(update={"cursor": "cursor"})
    with pytest.raises(PlaybillFormatError, match="explicit accepted coordinate"):
        service_read_claim_batch(seeded, request=req)


def test_latest_identity_read_matches_explicit_snapshot(seeded):
    selected = service_read_claim_batch(seeded, request=request(seeded, subject_paths=PATHS))
    ids = tuple(view.envelope["identity"] for view in selected.claims)
    latest = service_read_claim_batch(
        seeded, request=ClaimReadBatchRequestV1(claim_ids=ids, evaluation_time=TIME)
    )
    assert latest == selected


def test_latest_empty_generation_returns_its_full_coordinate(tmp_path):
    from tests.test_playbill._support import initialize_local

    instance, _ = initialize_local(tmp_path)
    result = service_read_claim_batch(
        instance, request=ClaimReadBatchRequestV1(subject_paths=PATHS)
    )
    assert result.claims == () and not result.truncated and result.cursor is None
    assert (
        result.coordinate.model_dump()
        == PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump()
    )


def test_backing_reads_use_one_blob_batch_without_admission_or_projection(seeded, monkeypatch):
    selected = service_read_claim_batch(seeded, request=request(seeded, subject_paths=PATHS))
    ids = tuple(view.envelope["identity"] for view in selected.claims)

    def forbidden(*args, **kwargs):
        raise AssertionError("backing metadata must read original accepted artifacts only")

    monkeypatch.setattr(seeded, "bind_accepted_projection", forbidden)
    calls = []
    original = seeded.blobs_at

    def counted(oid, paths):
        calls.append(tuple(paths))
        return original(oid, paths)

    monkeypatch.setattr(seeded, "blobs_at", counted)
    result = service_read_claim_backings(
        seeded, request=ClaimBackingsRequestV1(at=selected.coordinate, claim_ids=ids)
    )
    assert tuple(str(item.identity) for item in result.backings)  # typed backings, same order
    assert tuple("Claim:" + item.identity.name for item in result.backings) == ids
    assert len(calls) == 1 and len(calls[0]) == 2
    with pytest.raises(ClaimNotFoundError):
        service_read_claim_backings(
            seeded,
            request=ClaimBackingsRequestV1(at=selected.coordinate, claim_ids=("CLM-" + "0" * 32,)),
        )
    with pytest.raises(PlaybillFormatError):
        service_read_claim_backings(
            seeded, request=ClaimBackingsRequestV1(at=selected.coordinate, claim_ids=("CLM-abc",))
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"claim_ids": tuple(f"CLM-{i:032x}" for i in range(257))},
        {"subject_paths": PATHS, "limit": 257},
        {"claim_ids": ("a",), "subject_paths": PATHS},
        {"subject_paths": ()},
        {"subject_paths": PATHS, "evaluation_time": datetime(2026, 8, 21)},
    ],
)
def test_bounds_and_ambiguous_selection_are_refused(seeded, updates):
    with pytest.raises(ValidationError):
        ClaimReadBatchRequestV1.model_validate(
            {"at": request(seeded, subject_paths=PATHS).at, **updates}
        )


def test_runtime_checks_read_permission_before_instance_lookup(monkeypatch):
    from cruxible_core.runtime import playbill_api

    def denied(tool, *, instance_id):
        assert tool == "cruxible_playbill_read" and instance_id == "restricted"
        raise PermissionError("denied")

    monkeypatch.setattr(playbill_api, "check_permission", denied)
    for method in (
        playbill_api.playbill_read_claim_batch,
        playbill_api.playbill_read_claim_backings,
    ):
        with pytest.raises(PermissionError):
            method("restricted", request=None)


def test_explicit_older_coordinate_does_not_read_current_head(seeded):
    earlier = seeded.coordinate_for_oid(seeded.accepted_history()[-2].oid)
    req = request(seeded, subject_paths=PATHS)
    req = ClaimReadBatchRequestV1.model_validate(
        {
            **req.model_dump(mode="json"),
            "at": PlaybillAcceptedCoordinate.from_internal(earlier).model_dump(mode="json"),
        }
    )
    result = service_read_claim_batch(seeded, request=req)
    assert len(result.claims) == 1
    assert result.coordinate.git_oid == earlier.git_oid
    assert result.coordinate.git_oid != seeded.accepted_coordinate().git_oid


def test_retired_claim_cannot_be_pinned_as_live_backing(seeded, monkeypatch):
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claims import parse_claim, render_claim

    selected = service_read_claim_batch(seeded, request=request(seeded, subject_paths=PATHS))
    identity = selected.claims[0].envelope["identity"]
    path = selected.claims[0].envelope["path"]
    claim = parse_claim(seeded.blob_at(selected.coordinate.git_oid, path), path=path)
    retired = claim.model_copy(update={"lifecycle": ArtifactLifecycle(state="retired")})
    monkeypatch.setattr(seeded, "blobs_at", lambda _oid, _paths: {path: render_claim(retired)})
    with pytest.raises(PlaybillFormatError, match="live Claim"):
        service_read_claim_backings(
            seeded,
            request=ClaimBackingsRequestV1(
                at=selected.coordinate,
                claim_ids=(identity,),
            ),
        )
