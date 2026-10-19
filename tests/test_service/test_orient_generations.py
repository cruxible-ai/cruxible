"""``orient(section="generations")``: the ledger's timeline, newest first, paged by sequence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.orient import ORIENT_BECAUSE_MAX_CHARS
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.orient import _keyset_cursor, service_playbill_orient
from cruxible_core.service.list_pages import ListCursorMismatch
from tests.core_support._write_support import KIND, caller, seed_write_surface


def _write(instance: PlaybillInstance, because: str, *titles: str) -> None:
    request = WriteRequest.model_validate(
        {
            "because": because,
            "changes": [
                {"op": "set", "subject": f"{KIND}/{title}", "field": "title", "value": title}
                for title in titles
            ],
        }
    )
    assert service_playbill_write(instance, request=request, caller=caller()).status == "accepted"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    instance = seed_write_surface(tmp_path)[0]
    _write(instance, "One title.", "gen-a")
    _write(instance, "Two titles.", "gen-b", "gen-c")
    _write(instance, "x" * (ORIENT_BECAUSE_MAX_CHARS + 20), "gen-d")
    return instance


def _orient(instance: PlaybillInstance, **fields: Any) -> Any:
    return service_playbill_orient(instance, section="generations", **fields)


def test_generations_page_newest_first_down_to_genesis(instance: PlaybillInstance) -> None:
    history = instance.accepted_history()
    head = history[-1].sequence
    pages = [_orient(instance, limit=2)]
    while pages[-1].next_cursor is not None:
        pages.append(_orient(instance, limit=2, cursor=pages[-1].next_cursor))
    rows = [row for page in pages for row in page.generations]

    assert [row.sequence for row in rows] == list(range(head, -1, -1))
    assert all(page.coordinate == pages[0].coordinate for page in pages)
    assert [page.truncated for page in pages] == [True] * (len(pages) - 1) + [False]
    genesis = rows[-1]
    assert genesis.sequence == 0 and genesis.git_oid == history[0].oid
    assert genesis.proposal is None and genesis.members == 0
    for row in rows[:-1]:
        record = history[row.sequence].record
        assert record is not None
        assert row.git_oid == history[row.sequence].oid
        assert row.changeset_digest == record.changeset_digest
        assert row.members == len(record.members)
        assert row.approvals == tuple(
            dict.fromkeys(item.attestation.signer_id for item in record.approvals)
        )
        assert row.proposer == record.actor_binding.actor_id
        assert row.proposal is not None
    newest, two, one = rows[0], rows[1], rows[2]
    assert newest.because_truncated and newest.because is not None
    assert len(newest.because) == ORIENT_BECAUSE_MAX_CHARS
    assert (two.because, one.because) == ("Two titles.", "One title.")
    assert not two.because_truncated
    # The first row's proposal is the next read, then the next page.
    assert newest.proposal is not None and newest.proposal in pages[0].next[0]
    assert str(pages[0].next_cursor) in pages[0].next[1]


def test_a_page_at_an_earlier_generation_starts_there_and_a_cursor_pins_it(
    instance: PlaybillInstance,
) -> None:
    head = instance.accepted_history()[-1].sequence
    earlier = _orient(instance, at=str(head - 1), limit=1)
    assert [row.sequence for row in earlier.generations] == [head - 1]
    assert earlier.next_cursor is not None
    _write(instance, "Later.", "gen-e")
    following = _orient(instance, cursor=earlier.next_cursor, limit=1)
    assert [row.sequence for row in following.generations] == [head - 2]
    assert following.coordinate == earlier.coordinate
    # Another section's cursor never continues this one.
    foreign = _keyset_cursor(view="runs", served=earlier.coordinate, last_key=["x", "1"])
    with pytest.raises(ListCursorMismatch):
        _orient(instance, cursor=foreign)


def test_a_page_reads_only_its_own_generations(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-call counter: a page of two reads two generations' facts, at any history length."""

    from cruxible_core.indexes.history.history_index import HistoryReader

    seen: list[int] = []
    real = HistoryReader.read_generation_facts

    def counted(self: HistoryReader, sequence: int, load: Any) -> Any:
        seen.append(sequence)
        return real(self, sequence, load)

    monkeypatch.setattr(HistoryReader, "read_generation_facts", counted)
    for index in range(3):
        _write(instance, "More history.", f"gen-more-{index}")
    page = _orient(instance, limit=2)
    assert len(page.generations) == 2
    assert seen == [page.generations[0].sequence, page.generations[1].sequence]
    seen.clear()
    assert page.next_cursor is not None
    following = _orient(instance, limit=2, cursor=page.next_cursor)
    assert seen == [row.sequence for row in following.generations]
