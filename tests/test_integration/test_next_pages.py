"""Bounded next pages: a cursor continues exactly the whole queue it began."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts import PLAYBILL_NEXT_DEFAULT_LIMIT, PLAYBILL_NEXT_MAX_LIMIT
from cruxible_core.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.service.discovery import next as next_module
from cruxible_core.service.discovery.next import (
    PlaybillNextAcceptedStateInvalid,
    PlaybillNextCursorMismatch,
    PlaybillNextItemV1,
    PlaybillNextRepairV1,
    PlaybillNextRequestV1,
    PlaybillNextRequestV2,
    PlaybillNextResultV2,
    _item,
    service_playbill_next,
    validate_playbill_next_request,
)
from tests.core_support._knowledge_loop_support import seed_claims

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)
_SEVERITIES = ("warning", "repair", "blocking")


def _row(name: str) -> PlaybillNextItemV1:
    return _item(
        severity=_SEVERITIES[sum(name.encode()) % len(_SEVERITIES)],
        reason="document_modified",
        subject_identity=f"Document:{name}",
        detail={"document": name},
        repair=PlaybillNextRepairV1(
            operation="hand_edit",
            target=f"docs/{name}.md",
            required_change="repropose_the_document",
        ),
    )


class _Queue:
    """A real instance whose document fold reports exactly the rows named here."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names

    def items(self, *_args: object, **_kwargs: object) -> tuple[PlaybillNextItemV1, ...]:
        return tuple(_row(name) for name in self.names)


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Queue]:
    instance, _owner = seed_claims(tmp_path)
    rows = _Queue(tuple(f"doc-{number:02d}" for number in range(7)))
    monkeypatch.setattr(next_module, "_document_items", rows.items)
    return instance, rows


def _request(**values: Any) -> PlaybillNextRequestV2:
    return PlaybillNextRequestV2(
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfileV1(
            profile_id="next-pages",
            permitted_access_classes=("instance", "public"),
        ),
        **values,
    )


def _pages(instance: Any, *, limit: int, **values: Any) -> list[PlaybillNextResultV2]:
    pages = [service_playbill_next(instance, request=_request(limit=limit, **values))]
    while pages[-1].next_cursor is not None:
        # A caller's clock moves between pages; the cursor pins the first one.
        later = EVALUATION_TIME + timedelta(minutes=len(pages))
        request = _request(limit=limit, cursor=pages[-1].next_cursor, **values)
        pages.append(
            service_playbill_next(
                instance, request=request.model_copy(update={"evaluation_time": later})
            )
        )
    assert all(isinstance(page, PlaybillNextResultV2) for page in pages)
    return pages  # type: ignore[return-value]


def test_concatenated_pages_are_the_whole_queue_in_order(queue: tuple[Any, _Queue]) -> None:
    instance, _rows = queue
    whole = service_playbill_next(instance, request=_request(limit=PLAYBILL_NEXT_MAX_LIMIT))
    assert whole.total_items == len(whole.items) == 7
    assert whole.next_cursor is None and whole.whole_queue

    pages = _pages(instance, limit=3)

    assert [len(page.items) for page in pages] == [3, 3, 1]
    assert tuple(item for page in pages for item in page.items) == whole.items
    assert {page.result_digest for page in pages} == {whole.result_digest}
    assert {page.total_items for page in pages} == {7}
    assert {page.evaluation_time for page in pages} == {EVALUATION_TIME}
    assert [page.next_cursor is None for page in pages] == [False, False, True]
    # Paging again yields byte-identical pages and cursors.
    assert [page.model_dump_json() for page in _pages(instance, limit=3)] == [
        page.model_dump_json() for page in pages
    ]
    # A page is a valid result on its own; its digest still names the whole queue.
    for page in pages:
        assert PlaybillNextResultV2.model_validate(page.model_dump(mode="json")) == page


def test_the_default_page_is_the_whole_small_queue(queue: tuple[Any, _Queue]) -> None:
    instance, _rows = queue

    result = service_playbill_next(instance, request=_request())

    assert result.whole_queue and result.next_cursor is None
    assert PlaybillNextResultV2.model_validate(result.model_dump(mode="json")) == result


def test_page_size_is_bounded_by_the_shared_convention() -> None:
    assert (PLAYBILL_NEXT_DEFAULT_LIMIT, PLAYBILL_NEXT_MAX_LIMIT) == (100, 1000)
    assert _request().limit == PLAYBILL_NEXT_DEFAULT_LIMIT
    assert _request(limit=PLAYBILL_NEXT_MAX_LIMIT).limit == PLAYBILL_NEXT_MAX_LIMIT
    body = _request().model_dump(mode="json")
    for limit in (0, PLAYBILL_NEXT_MAX_LIMIT + 1):
        with pytest.raises(PlaybillNextAcceptedStateInvalid):
            validate_playbill_next_request(body | {"limit": limit})
    with pytest.raises(PlaybillNextCursorMismatch):
        validate_playbill_next_request(body | {"cursor": "x" * 2049})


def test_a_cursor_from_another_queue_is_refused(queue: tuple[Any, _Queue]) -> None:
    instance, rows = queue
    first = service_playbill_next(instance, request=_request(limit=2))
    assert first.next_cursor is not None

    rows.names = (*rows.names, "doc-new")
    with pytest.raises(PlaybillNextCursorMismatch, match="queue moved"):
        service_playbill_next(instance, request=_request(limit=2, cursor=first.next_cursor))
    # The refusal names the repair: page one of the queue as it now stands.
    fresh = service_playbill_next(instance, request=_request(limit=2))
    assert fresh.result_digest != first.result_digest and fresh.total_items == 8


@pytest.mark.parametrize("cursor", ["garbage", "e30=", "bm90IGpzb24="])
def test_a_cursor_that_is_not_a_next_cursor_is_refused(
    queue: tuple[Any, _Queue], cursor: str
) -> None:
    instance, _rows = queue

    with pytest.raises(PlaybillNextCursorMismatch, match="not a next page cursor"):
        service_playbill_next(instance, request=_request(cursor=cursor))


def test_a_cursor_does_not_cross_request_versions(queue: tuple[Any, _Queue]) -> None:
    instance, _rows = queue
    v2 = service_playbill_next(instance, request=_request(limit=2))
    v1_request = PlaybillNextRequestV1(
        evaluation_time=EVALUATION_TIME,
        access_profile=_request().access_profile,
        limit=2,
    )
    v1 = service_playbill_next(instance, request=v1_request)
    assert v1.next_cursor is not None and v2.next_cursor is not None

    with pytest.raises(PlaybillNextCursorMismatch, match="v2 queue"):
        service_playbill_next(
            instance, request=v1_request.model_copy(update={"cursor": v2.next_cursor})
        )
    with pytest.raises(PlaybillNextCursorMismatch, match="v1 queue"):
        service_playbill_next(instance, request=_request(limit=2, cursor=v1.next_cursor))


def test_a_delta_pages_its_changed_rows_under_the_whole_queue_digest(
    queue: tuple[Any, _Queue],
) -> None:
    instance, rows = queue
    before = service_playbill_next(instance, request=_request())
    rows.names = (*rows.names[2:], "doc-a", "doc-b", "doc-c")

    delta = service_playbill_next(
        instance,
        request=_request(limit=PLAYBILL_NEXT_MAX_LIMIT, since_result_digest=before.result_digest),
    )
    pages = _pages(instance, limit=2, since_result_digest=before.result_digest)

    assert delta.delta_since == before.result_digest and delta.total_items == 5
    assert len(delta.removed_item_ids) == 2
    assert tuple(item for page in pages for item in page.items) == delta.items
    assert {page.result_digest for page in pages} == {delta.result_digest}
    assert {page.delta_since for page in pages} == {before.result_digest}
    assert {page.total_items for page in pages} == {5}
    # Each page names only the removals it carries; together they are all of them.
    for page in pages:
        assert set(page.removed_item_ids) <= {item.item_id for item in page.items}
    removed = [item_id for page in pages for item_id in page.removed_item_ids]
    assert sorted(removed) == sorted(delta.removed_item_ids)


def test_a_delta_cursor_refuses_once_its_base_is_forgotten(queue: tuple[Any, _Queue]) -> None:
    instance, rows = queue
    before = service_playbill_next(instance, request=_request())
    rows.names = (*rows.names[2:], "doc-a", "doc-b", "doc-c")
    first = service_playbill_next(
        instance, request=_request(limit=2, since_result_digest=before.result_digest)
    )
    assert first.next_cursor is not None

    with next_module._QUEUE_MEMO_LOCK:
        for key in [key for key in next_module._QUEUE_MEMO if key[1] == before.result_digest]:
            next_module._QUEUE_MEMO.pop(key)
    # Without its base the delta would silently become the whole queue; paging
    # that at the delta's offset would skip rows, so the cursor refuses instead.
    with pytest.raises(PlaybillNextCursorMismatch, match="delta base"):
        service_playbill_next(instance, request=_request(limit=2, cursor=first.next_cursor))
