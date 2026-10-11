"""Many whole values in few batched reads, each pinned where it was previewed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cruxible_client import Cruxible
from cruxible_client import contracts as api
from cruxible_client.authoring.sdk import WHOLE_READ_BATCH_TEXT
from cruxible_client.authoring.sdk_types import WholeValueUnavailable
from cruxible_client.contracts.claim_reads import (
    MAX_CLAIM_READ_BATCH,
    ClaimReadBatchRequest,
    ClaimReadBatchResult,
)
from cruxible_client.contracts.read_values import TruncatedText, WholeValueRead
from cruxible_client.errors import DataValidationError
from tests.test_client.test_playbill_sdk_world import (
    _COORDINATE,
    _MOVED_COORDINATE,
    _workspace,
    _WorldClient,
)

_SUBJECT = "subjects/sec.vulnerability/cve-2026-69247.json"


def _claim(index: int) -> str:
    return f"CLM-{index:032x}"


def _view(identity: str, value: Any, *, kind: str = "literal", at: Any = _COORDINATE) -> Any:
    item = {"kind": kind, "value": value} if kind == "literal" else {"kind": kind}
    if kind == "exact_content":
        item["content_digest"] = "sha256:" + "c" * 64
    return api.ClaimViewRecord(
        tag="playbill-claim-read-v2",
        coordinate_kind="canonical",
        coordinate=at,
        envelope={"identity": f"Claim:{identity}", "revision": 1},
        admission_evaluation_time="2026-09-07T12:00:00Z",
        statement=api.ClaimStatementCard(
            subject={
                "artifact_path": _SUBJECT,
                "selector": {"scheme": "artifact-v1", "value": ""},
            },
            predicate="sec.vulnerability.summary",
            object=item,
            role="observation",
            qualifier=None,
            lifecycle="live",
        ),
        facts=[
            {
                "schema_id": "cruxible.claim.statement",
                "value": {
                    "subject": {"artifact_path": _SUBJECT},
                    "predicate": "sec.vulnerability.summary",
                    "qualifier": None,
                    "role": "observation",
                    "object": item,
                },
            },
            {"schema_id": "cruxible.claim.lifecycle", "value": {"lifecycle": {"state": "live"}}},
        ],
        admission_accounts=[],
    )


def _preview(identity: str, value: str, *, at: str = _COORDINATE.git_oid) -> TruncatedText:
    return TruncatedText(
        truncated=True,
        preview=value[:500],
        length=len(value),
        read_whole=WholeValueRead(ref=identity, at=at),
    )


class _Batches:
    """The batched Claim read, served from whole values, recording each request."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values
        self.requests: list[ClaimReadBatchRequest] = []
        self.answer_at: Any = None

    def __call__(self, _instance_id: str, *, request: ClaimReadBatchRequest) -> Any:
        self.requests.append(request)
        at = next(
            item
            for item in (_COORDINATE, _MOVED_COORDINATE)
            if item.git_oid == (request.at if isinstance(request.at, str) else None)
        )
        at = self.answer_at or at
        return ClaimReadBatchResult(
            coordinate=at,
            claims=tuple(_view(claim, self.values[claim], at=at) for claim in request.claim_ids),
        )


@pytest.fixture
def connection(tmp_path: Any) -> tuple[Cruxible, _WorldClient]:
    _workspace(tmp_path)
    client = _WorldClient()
    pb = Cruxible._from_client(  # noqa: SLF001
        client,  # type: ignore[arg-type]
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    return pb, client


def test_four_hundred_long_values_read_whole_in_two_batches(
    connection: tuple[Cruxible, _WorldClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    pb, client = connection
    values = {_claim(index): f"summary {index} " + "long text " * 120 for index in range(400)}
    batches = _Batches(values)
    monkeypatch.setattr(client, "read_claim_batch", batches, raising=False)

    whole = pb.read_whole_many(_preview(claim, value) for claim, value in values.items())

    assert whole == values
    assert [len(request.claim_ids) for request in batches.requests] == [
        MAX_CLAIM_READ_BATCH,
        400 - MAX_CLAIM_READ_BATCH,
    ]
    # Pinned to the git oid each preview names, never re-pinned to the head.
    assert {request.at for request in batches.requests} == {_COORDINATE.git_oid}


def test_batches_pack_previewed_text_and_group_by_generation(
    connection: tuple[Cruxible, _WorldClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    pb, client = connection
    big = "x" * (WHOLE_READ_BATCH_TEXT // 2 + 1)
    values = {_claim(index): big for index in range(3)} | {_claim(9): "y" * 600}
    batches = _Batches(values)
    monkeypatch.setattr(client, "read_claim_batch", batches, raising=False)
    previews = [_preview(_claim(index), big) for index in range(3)]
    previews.append(_preview(_claim(9), "y" * 600, at=_MOVED_COORDINATE.git_oid))

    assert pb.read_whole_many(previews) == values
    # Two half-budget values never share a read; each generation is its own read.
    assert [(request.at, len(request.claim_ids)) for request in batches.requests] == [
        (_COORDINATE.git_oid, 1),
        (_COORDINATE.git_oid, 1),
        (_COORDINATE.git_oid, 1),
        (_MOVED_COORDINATE.git_oid, 1),
    ]


def test_one_claim_at_two_generations_or_a_moved_answer_is_refused(
    connection: tuple[Cruxible, _WorldClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    pb, client = connection
    claim = _claim(1)
    batches = _Batches({claim: "z" * 600})
    monkeypatch.setattr(client, "read_claim_batch", batches, raising=False)
    with pytest.raises(WholeValueUnavailable, match="two generations"):
        pb.read_whole_many(
            [_preview(claim, "z" * 600), _preview(claim, "z" * 600, at=_MOVED_COORDINATE.git_oid)]
        )
    assert batches.requests == []
    batches.answer_at = _MOVED_COORDINATE
    with pytest.raises(WholeValueUnavailable, match="different selection"):
        pb.read_whole_many([_preview(claim, "z" * 600)])
    with pytest.raises(WholeValueUnavailable, match="names no exact whole read"):
        pb.read_whole_many([TruncatedText(truncated=True, preview="p", length=600)])


def test_a_daemon_without_git_oid_batches_says_so(
    connection: tuple[Cruxible, _WorldClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    pb, client = connection

    def older(_instance_id: str, *, request: Any) -> Any:
        raise DataValidationError(
            "Request validation failed", errors=["body.at: Input should be a valid dictionary"]
        )

    monkeypatch.setattr(client, "read_claim_batch", older, raising=False)
    with pytest.raises(WholeValueUnavailable, match="upgrade it, or read each preview"):
        pb.read_whole_many([_preview(_claim(1), "z" * 600)])


def test_exact_content_values_are_read_as_read_whole_reads_them(
    connection: tuple[Cruxible, _WorldClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    pb, client = connection
    literal, exact = _claim(1), _claim(2)

    def batch(_instance_id: str, *, request: ClaimReadBatchRequest) -> Any:
        return ClaimReadBatchResult(
            coordinate=_COORDINATE,
            claims=(_view(literal, "l" * 600), _view(exact, None, kind="exact_content")),
        )

    monkeypatch.setattr(client, "read_claim_batch", batch, raising=False)
    singles: list[str] = []

    def read_whole(preview: TruncatedText) -> object:
        assert preview.read_whole is not None
        singles.append(preview.read_whole.ref)
        return "e" * 600

    monkeypatch.setattr(pb, "read_whole", read_whole)
    whole = pb.read_whole_many([_preview(literal, "l" * 600), _preview(exact, "e" * 600)])
    assert whole == {literal: "l" * 600, exact: "e" * 600}
    assert singles == [exact]


def test_prefetch_reads_a_wide_selection_in_batches_at_one_coordinate(
    connection: tuple[Cruxible, _WorldClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    pb, client = connection
    requests: list[ClaimReadBatchRequest] = []

    def batch(_instance_id: str, *, request: ClaimReadBatchRequest) -> Any:
        requests.append(request)
        return ClaimReadBatchResult(coordinate=_COORDINATE, claims=())

    monkeypatch.setattr(client, "read_claim_batch", batch, raising=False)
    subjects = [f"sec.vulnerability/cve-{index:05d}" for index in range(300)]
    assert pb.world().prefetch(subjects=subjects) == ()
    assert [len(request.subject_paths) for request in requests] == [MAX_CLAIM_READ_BATCH, 44]
    assert {request.at for request in requests} == {_COORDINATE}
    assert len({request.evaluation_time for request in requests}) == 1
    with pytest.raises(ValueError, match="nonempty and unique"):
        pb.world().prefetch(subjects=[subjects[0], subjects[0]])
