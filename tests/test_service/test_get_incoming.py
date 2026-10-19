"""``get(detail="incoming")``: the live Claims pointing at a Subject, one keyset page at a time.

A relation is stored once, on the asserting Subject; this reads it from the
object's side through the object index (``claims_by_object_subject``), so a
page costs its own rows and never the Subject's whole incoming set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import GetRequest, GetResult, GetSubjectCard
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.get import INCOMING_PAGE_SQL, service_playbill_get
from cruxible_core.service.list_pages import ListCursorMismatch
from cruxible_core.service.read_refusals import ReadRefusalError
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)
_TARGET = f"{KIND}/wi-1"
_SOURCES = tuple(f"{KIND}/src-{n}" for n in range(7))


def _write(instance: PlaybillInstance, changes: list[dict[str, Any]]) -> None:
    request = WriteRequest.model_validate({"because": "Point at wi-1.", "changes": changes})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome


def _get(instance: PlaybillInstance, ref: str, **fields: Any) -> GetResult:
    return service_playbill_get(instance, request=GetRequest(ref=ref, **fields), access=_ACCESS)


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    instance = seed_write_surface(tmp_path)[0]
    _write(
        instance,
        [
            {"op": "add", "subject": source, "field": "governs", "value": _TARGET}
            for source in _SOURCES
        ],
    )
    return instance


def _pages(instance: PlaybillInstance, ref: str, *, limit: int) -> list[GetResult]:
    pages = [_get(instance, ref, detail="incoming", limit=limit)]
    while pages[-1].next_cursor is not None:
        pages.append(
            _get(instance, ref, detail="incoming", limit=limit, cursor=pages[-1].next_cursor)
        )
    return pages


def test_incoming_pages_every_live_pointing_claim_in_claim_order(
    instance: PlaybillInstance,
) -> None:
    pages = _pages(instance, _TARGET, limit=3)

    assert [len(page.incoming.claims) for page in pages if page.incoming] == [3, 3, 1]
    assert [page.truncated for page in pages] == [True, True, False]
    rows = [row for page in pages if page.incoming for row in page.incoming.claims]
    assert sorted(row.subject for row in rows) == sorted(_SOURCES)
    assert [row.claim for row in rows] == sorted(row.claim for row in rows)
    assert {row.predicate for row in rows} == {f"{KIND}.governs"}
    card = _get(instance, _TARGET).card
    assert isinstance(card, GetSubjectCard) and card.incoming_count == len(_SOURCES)
    # The card points at the read that lists them.
    assert card.next[-1] == f'cruxible_get(ref="{_TARGET}", detail="incoming")'
    # A Subject nothing points at has an empty page and no such suggestion.
    empty = _get(instance, _SOURCES[0], detail="incoming")
    assert empty.incoming is not None and empty.incoming.claims == () and not empty.truncated
    assert not any("incoming" in step for step in _get(instance, _SOURCES[0]).card.next)


def test_a_retired_edge_leaves_the_head_but_an_earlier_generation_still_reads_it(
    instance: PlaybillInstance,
) -> None:
    before = _get(instance, _TARGET, detail="incoming", limit=50).coordinate.generation
    _write(instance, [{"op": "retire", "target": {"subject": _SOURCES[0], "field": "governs"}}])

    now = _get(instance, _TARGET, detail="incoming", limit=50)
    assert now.incoming is not None
    assert sorted(row.subject for row in now.incoming.claims) == sorted(_SOURCES[1:])
    then = _get(instance, _TARGET, detail="incoming", limit=50, at=str(before))
    assert then.incoming is not None and len(then.incoming.claims) == len(_SOURCES)


def test_a_page_continues_at_its_own_coordinate_and_refuses_another_lists_cursor(
    instance: PlaybillInstance,
) -> None:
    first = _get(instance, _TARGET, detail="incoming", limit=2)
    assert first.next_cursor is not None
    # A write between pages never shifts the page: the cursor pins its coordinate.
    _write(
        instance,
        [{"op": "add", "subject": f"{KIND}/src-late", "field": "governs", "value": _TARGET}],
    )
    rest = _pages(instance, _TARGET, limit=50)[0]
    assert rest.incoming is not None and len(rest.incoming.claims) == len(_SOURCES) + 1
    second = _get(instance, _TARGET, detail="incoming", limit=50, cursor=first.next_cursor)
    assert second.incoming is not None
    assert len(second.incoming.claims) == len(_SOURCES) - 2
    assert second.coordinate == first.coordinate

    history = _get(instance, _TARGET, detail="history", limit=1)
    assert history.next_cursor is not None
    with pytest.raises(ListCursorMismatch):
        _get(instance, _TARGET, detail="incoming", cursor=history.next_cursor)
    with pytest.raises(ListCursorMismatch):
        _get(instance, _TARGET, detail="history", cursor=first.next_cursor)


def test_incoming_applies_to_subjects_only(instance: PlaybillInstance) -> None:
    with pytest.raises(ReadRefusalError) as caught:
        _get(instance, f"ClaimType:{KIND}.governs", detail="incoming")
    assert caught.value.error_code == "cruxible.get.detail_unsupported"


def test_a_page_seeks_the_object_index_and_reads_only_its_rows(
    instance: PlaybillInstance,
) -> None:
    """The page's statement is a bounded seek of the object index, never a scan or sort."""

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        plan = projection.typed.connection.execute(
            "EXPLAIN QUERY PLAN " + INCOMING_PAGE_SQL, (f"subjects/{_TARGET}.json", "", 3)
        ).fetchall()
    details = " ".join(str(row[-1]) for row in plan)
    assert "USING INDEX claims_by_object_subject (object_path=? AND identity>?)" in details, (
        details
    )
    assert "TEMP B-TREE" not in details, details
