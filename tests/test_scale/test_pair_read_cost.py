"""Pair reads cost the page, never the unrelated pairs nor an end's historical shells.

Two worlds (``ReachWorld``, accepted raw) differ only in ``unrelated``: 100 vs
1,000 exposures and occurrences of other components, each holding a live fact,
plus as many historical shells (exposures whose fact was retired) of the paged
end ``c0``. Both hold the same output: ``c0`` and ``d0`` each with ``OUTPUT``
justified exposures and one occurrence with a scope. Measured, each warm:

- ``end_card``: ``get d0`` (its pairs per kind and part, bounded by the card);
- ``pairs_page``: ``get c0 detail=pairs`` (one page holding every live pair of
  ``c0``), whose shells have no pair index rows and so cost nothing;
- ``pair_card``: ``get`` of one exposure (its ends from ``pair_ends``).

Rule: for every read and counter ``LARGE <= SMALL * RATIO + SLACK`` (the scale
guard's), with the same answer at both sizes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import GetRequest, GetResult
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._query_reach_world import (
    COMPONENT,
    EXPOSURE,
    INVENTORY,
    JUSTIFICATION,
    OCCURRENCE,
    SCOPE,
    VULN,
    ReachWorld,
)
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK

OUTPUT = 10
SIZES = (100, 1_000)
COUNTED = ("sql_statements", "sql_vm_steps", "claim_parses")
_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)
READS = ("end_card", "pairs_page", "pair_card")


def _ref(path: str) -> str:
    return path.removeprefix("subjects/").removesuffix(".json")


def _world(root: Path, unrelated: int) -> tuple[ReachWorld, dict[str, str]]:
    built = ReachWorld(root)
    inventory = built.subject(INVENTORY, "i0")
    other = built.subject(INVENTORY, "i1")
    shared = built.subject(VULN, "shared")
    ends: dict[str, str] = {}
    for name in ("c0", "d0"):
        end = ends[name] = built.subject(COMPONENT, name)
        built.claim(SCOPE, "required", built.subject(OCCURRENCE, inventory, end))
        for index in range(OUTPUT):
            pair = built.subject(EXPOSURE, end, built.subject(VULN, f"{name}-v{index}"))
            built.claim(JUSTIFICATION, "vulnerable_code_not_present", pair)
    retire = []
    for index in range(unrelated):
        component = built.subject(COMPONENT, f"u{index}")
        built.claim(SCOPE, "required", built.subject(OCCURRENCE, other, component))
        built.claim(
            JUSTIFICATION, "vulnerable_code_not_present", built.subject(EXPOSURE, component, shared)
        )
        shell = built.subject(EXPOSURE, ends["c0"], built.subject(VULN, f"w{index}"))
        retire.append(built.claim(JUSTIFICATION, "vulnerable_code_not_present", shell))
    built.accept()
    for claim in retire:
        built.retire(claim)
    built.accept()
    ends["pair"] = built.subject(EXPOSURE, ends["d0"], built.subject(VULN, "d0-v3"))
    return built, ends


def _read(built: ReachWorld, ends: dict[str, str], read: str) -> GetResult:
    request = {
        "end_card": GetRequest(ref=_ref(ends["d0"])),
        "pairs_page": GetRequest(ref=_ref(ends["c0"]), detail="pairs"),
        "pair_card": GetRequest(ref=_ref(ends["pair"])),
    }[read]
    return service_playbill_get(built.instance, request=request, access=_ACCESS)


def _answer(result: GetResult, read: str) -> Any:
    if read == "pairs_page":
        assert result.pairs is not None and not result.truncated
        return [(group.kind, group.part, len(group.pairs)) for group in result.pairs.groups]
    card = result.card
    if read == "end_card":
        return [(group.kind, group.part, len(group.pairs), group.more) for group in card.pairs]  # type: ignore[union-attr]
    return [(end.part, end.subject.split("/")[0]) for end in card.ends]  # type: ignore[union-attr]


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> Iterator[list[dict[str, Any]]]:
    root = tmp_path_factory.mktemp("pair-read-counts")
    out: list[dict[str, Any]] = []
    with trace_sqlite(root):
        for size in SIZES:
            built, ends = _world(root / f"u{size}", size)
            counts: dict[str, Any] = {}
            for read in READS:
                _read(built, ends, read)
                with counting(root) as window:
                    result = _read(built, ends, read)
                assert not window.unexplained, window.unexplained
                counts[read] = {
                    "answer": _answer(result, read),
                    **{counter: window.counts[counter] for counter in COUNTED},
                }
            out.append(counts)
    yield out


@pytest.mark.parametrize("read", READS)
def test_pair_reads_are_flat_in_unrelated_pairs_and_shells(
    measured: list[dict[str, Any]], read: str
) -> None:
    small, large = (sized[read] for sized in measured)
    assert small["answer"] == large["answer"], (small, large)
    for counter in COUNTED:
        assert large[counter] <= small[counter] * RATIO + SLACK[counter], (read, counter, measured)


def test_the_measured_answers_hold_the_output(measured: list[dict[str, Any]]) -> None:
    # The counts, for a run with -s.
    print({read: [sized[read] for sized in measured] for read in READS})
    small = measured[0]
    assert small["end_card"]["answer"] == [
        (EXPOSURE, "component", 5, True),
        (OCCURRENCE, "component", 1, False),
    ]
    # Every live pair of c0 on one page; none of its shells.
    assert small["pairs_page"]["answer"] == [
        (EXPOSURE, "component", OUTPUT),
        (OCCURRENCE, "component", 1),
    ]
    assert small["pair_card"]["answer"] == [("component", COMPONENT), ("vulnerability", VULN)]
