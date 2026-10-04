"""Process-wide memos on the read path stay correct when hammered from threads.

Every daemon route now runs in the threadpool, so reads promote, insert, trim
and clear these memos concurrently with each other and with activation. Each
memo is driven here from several threads at once, with its capacity cut so
nearly every insert evicts and a clearing thread running beside the readers.
Every call must return and agree with the single-threaded answer. No timing is
asserted; for the cheap memos a shortened interpreter switch interval makes
interleavings inside each memo access more likely.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_core.derived.memo import memo_clear
from cruxible_core.procedures import graph_digests
from cruxible_core.runtime import instance as instance_module
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import AcceptedCoordinate
from cruxible_core.service.discovery import claim_status
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from cruxible_core.service.procedures import measurements
from tests.core_support._knowledge_loop_support import seed_claims

_WORKERS = 8
_ROUNDS = 2000
_EVALUATION_TIME = datetime(2026, 8, 21, 14, tzinfo=UTC)


@pytest.fixture
def frequent_switches() -> Iterator[None]:
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def _hammer(
    work: Callable[[int, int], None],
    *,
    clear: Callable[[], None],
    rounds: int = _ROUNDS,
) -> None:
    """Run ``work(worker, round)`` on several threads beside a clearing thread."""

    errors: list[BaseException] = []
    start = threading.Barrier(_WORKERS + 1)
    done = threading.Event()

    def worker(index: int) -> None:
        try:
            start.wait()
            for round_index in range(rounds):
                work(index, round_index)
        except BaseException as exc:  # noqa: BLE001 - every failure is the finding
            errors.append(exc)

    def clearer() -> None:
        try:
            start.wait()
            while not done.is_set():
                clear()
                time.sleep(0)  # yield, so the clearer does not starve the readers
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(_WORKERS)]
    sweeper = threading.Thread(target=clearer)
    for thread in (*threads, sweeper):
        thread.start()
    for thread in threads:
        thread.join()
    done.set()
    sweeper.join()
    assert errors == []


@pytest.mark.usefixtures("frequent_switches")
def test_node_digest_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_digests, "NODE_DIGEST_MEMO_CAPACITY", 2)
    monkeypatch.setattr(
        graph_digests, "compute_node_digests", lambda definition: {"node": definition}
    )
    graph_digests.clear_node_digest_memo()

    def work(worker: int, round_index: int) -> None:
        digest = f"sha256:{(worker + round_index) % 5}"
        got = graph_digests.cached_node_digests(
            digest,  # type: ignore[arg-type]
            definition_digest=digest,
        )
        assert got == {"node": digest}

    _hammer(work, clear=graph_digests.clear_node_digest_memo)


@pytest.mark.usefixtures("frequent_switches")
def test_reading_index_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(measurements, "_READING_INDEX_MEMO_CAPACITY", 2)
    memo_clear(measurements._reading_index_memo)

    def records(partition: str) -> list[Any]:
        return [
            SimpleNamespace(
                record_digest=f"{partition}-{sequence}",
                record=SimpleNamespace(event_kind="procedure_run"),
            )
            for sequence in range(4)
        ]

    class Journal:
        def read_head(self, _stream: object, partition: str) -> Any:
            return SimpleNamespace(sequence=len(records(partition)))

        def all_records(self, _stream: object, partition: str) -> list[Any]:
            return records(partition)

    instance = SimpleNamespace(root=Path("/hammered"))

    def work(worker: int, round_index: int) -> None:
        partition = f"partition-{(worker + round_index) % 5}"
        index = measurements.reading_partition_index(
            instance,  # type: ignore[arg-type]
            journal=Journal(),  # type: ignore[arg-type]
            stream=None,  # type: ignore[arg-type]
            partition_id=partition,
        )
        assert index.digests == tuple(item.record_digest for item in records(partition))
        assert index.entries == ()

    def clear() -> None:
        measurements.memo_discard(measurements._reading_index_memo, ("/hammered", "partition-0"))
        memo_clear(measurements._reading_index_memo)

    _hammer(work, clear=clear)


def test_validated_path_memo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instance_module, "_VALIDATED_PATH_CAPACITY", 1)
    roots = [tmp_path / name for name in ("a", "b")]
    for root in roots:
        root.mkdir()
    worlds = [seed_claims(root)[0] for root in roots]
    expected = [
        PlaybillInstance._validated_paths(world.root, world.descriptor.storage) for world in worlds
    ]

    def work(worker: int, round_index: int) -> None:
        index = (worker + round_index) % 2
        world = worlds[index]
        assert (
            PlaybillInstance._validated_paths(world.root, world.descriptor.storage)
            == expected[index]
        )

    _hammer(work, clear=lambda: memo_clear(instance_module._VALIDATED_PATHS), rounds=50)


def test_claim_resolution_memos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claim_status, "_SLOT_MEMO_CAPACITY", 1)
    instance, _owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    claims = ClaimVerdictReadContext(instance, coordinate).claims()
    at = AcceptedCoordinate.from_internal(coordinate)

    def resolve() -> dict[str, Any]:
        return claim_status.claim_resolution_statuses(
            instance, claims=claims, at=at, evaluation_time=_EVALUATION_TIME
        )

    claim_status.reset_claim_resolution_memo()
    expected = resolve()
    assert expected

    def work(_worker: int, round_index: int) -> None:
        assert resolve() == expected

    def clear() -> None:
        claim_status.reset_claim_resolution_memo(slots=True)
        claim_status.reset_claim_resolution_memo(slots=False)

    # Each resolution is a full verdict fold, so fewer rounds; the default
    # switch interval keeps it cheap while threads still interleave.
    _hammer(work, clear=clear, rounds=5)
