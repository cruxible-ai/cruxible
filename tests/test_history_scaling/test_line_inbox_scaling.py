"""The Line inbox's reads, posting and next row cost the same over a small and a larger history.

History is real: earlier postings, each journaled, most of them since judged,
from the measured Line and from others. Each operation is counted the way
``test_service_scaling`` counts a service operation (journal frames, CAS reads,
accepted blobs, partitions enumerated, SQLite VM instructions), never by wall
time, once after the small history and once after one ten times larger.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.line_inbox import InboxDismissed
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.exhaust.line_inbox import InboxPosting, LineInboxStore
from cruxible_core.service.discovery.next import _line_inbox_items
from cruxible_core.service.procedures.line_inbox import (
    service_get_inbox_item,
    service_list_inbox,
)
from tests.core_support._support import initialize_local
from tests.test_exhaust.test_line_inbox_store import NOW, _actor
from tests.test_history_scaling.test_service_scaling import _counted

SMALL, LARGE = 10, 100
LINE = "Line:watch"


def _posting(ordinal: int, *, line: str = LINE, run: str | None = None) -> InboxPosting:
    # Each posting stands on its own Capture, as a Source Line's runs do.
    return InboxPosting(
        line=line,
        procedure="Procedure:watcher",
        procedure_artifact_digest="sha256:" + "d" * 64,
        run_id=run or f"RUN-{line}-{ordinal}",
        node_id="inbox",
        item_key="item-0",
        payload={"ordinal": ordinal, "line": line},
        source_capture_digests=(
            "sha256:" + hashlib.sha256(f"{line}:{ordinal}:{run}".encode()).hexdigest(),
        ),
        posted_at=NOW + timedelta(seconds=ordinal),
    )


def _grow(store: LineInboxStore, write: dict[str, object], start: int, stop: int) -> None:
    """Judged postings from the measured Line and another, plus the other's unread ones."""

    for ordinal in range(start, stop):
        for line in (LINE, "Line:other"):
            (key,) = store.post([_posting(ordinal, line=line)], **write)  # type: ignore[arg-type]
            if line == LINE or ordinal % 2 == 0:
                store.record_disposition(  # type: ignore[arg-type]
                    store.resolve(key),
                    InboxDismissed(actor_id="agent", decided_at=NOW, reason="seen"),
                    **write,
                )


def _measure(
    monkeypatch: pytest.MonkeyPatch, operations: dict[str, Callable[[], object]]
) -> dict[str, dict[str, int]]:
    measured: dict[str, dict[str, int]] = {}
    for name, operation in operations.items():
        with _counted(monkeypatch) as work:
            operation()
        measured[name] = dict(work)
    return measured


def test_inbox_operations_do_not_grow_with_the_inbox_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = initialize_local(tmp_path)
    store = LineInboxStore(instance)
    write = {
        "actor": _actor(instance),
        "now": NOW,
        "coordinate": AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    }
    profile = CoverageAccessProfile(
        profile_id="inbox-scaling", permitted_access_classes=("instance", "public")
    )
    rounds = []
    grown = 0
    for size in (SMALL, LARGE):
        # The previous round's unread items become history too.
        unread, _cursor = store.page(line=LINE, status="unread", limit=50, cursor=None)
        for stored in unread:
            store.record_disposition(  # type: ignore[arg-type]
                stored.envelope.item_id,
                InboxDismissed(actor_id="agent", decided_at=NOW, reason="seen"),
                **write,
            )
        _grow(store, write, grown, size)
        grown = size
        fresh = 10_000 + size
        # Three unread items on the measured Line, whatever came before.
        keys = [
            store.post([_posting(fresh + offset)], **write)[0]  # type: ignore[arg-type]
            for offset in range(3)
        ]
        item = store.resolve(keys[0])
        LineInboxStore(instance).unread_by_line()  # the projection is caught up
        rounds.append(
            _measure(
                monkeypatch,
                {
                    "post": lambda fresh=fresh: store.post(  # type: ignore[misc]
                        [_posting(fresh + 3)], **write
                    ),
                    # The same row on new evidence: a sighting of an existing item.
                    "recur": lambda fresh=fresh: store.post(  # type: ignore[misc]
                        [_posting(fresh, run=f"RUN-recur-{fresh}")], **write
                    ),
                    "list_unread": lambda: service_list_inbox(
                        instance, line="watch", status="unread", limit=10
                    ),
                    "list_all": lambda: service_list_inbox(instance, limit=10),
                    "get": lambda item=item: service_get_inbox_item(instance, item=item),  # type: ignore[misc]
                    "next_row": lambda: _line_inbox_items(instance, access_profile=profile),
                },
            )
        )
        (row,) = (
            row
            for row in _line_inbox_items(instance, access_profile=profile)
            if row.subject_identity == LINE
        )
        assert row.detail["unread"] == 4

    small, large = rounds
    for name in small:
        assert small[name]["vm"] > 0, name
        # Reads match exactly; SQLite may run a handful more instructions on a
        # larger file, while one extra row per past item would add hundreds.
        assert {k: v for k, v in small[name].items() if k != "vm"} == {
            k: v for k, v in large[name].items() if k != "vm"
        }, (name, small[name], large[name])
        assert abs(small[name]["vm"] - large[name]["vm"]) <= 64, (name, rounds)
