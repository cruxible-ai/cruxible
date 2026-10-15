"""A fan-out settle's work per item: SHA-256s, canonical encodes and canonical bytes.

``test_scale_guard`` checks that a route does not grow with the instance. This
checks the other axis of the cost model: that the work a settle does for each
item it settles stays near its floor (one Claim built, its digests taken once,
its bytes written once, one journal record, one entry in the change set), so a
re-encode or re-hash of one item's payloads cannot creep back unnoticed.

One world runs a fan-out settle Line four times, each on fresh Subjects:

1. a warm-up of ``N`` items (first parses of the accepted artifacts);
2. ``N`` items;
3. ``2N`` items;
4. ``N`` items again, right after the large one.

The per-item slope of every counter is (3) minus (2), over ``N``: everything a
settle does once, whatever its size, cancels. Each slope must stay under its
ceiling (``PER_ITEM``). A small settle right after a large one must cost what a
small one after a small one costs (``LARGE_PARENT_RATIO`` and ``FLAT_SLACK``):
the receipt of the generation it succeeds is as large as that generation's
change, and nothing a settle reads of its parent may be paid for at that size.
Before each measured settle the parsed change-set receipts are forgotten, as in
a fresh process, so a parent receipt re-read and re-parsed shows up as that cost.

Counted on the measuring thread only:

- ``sha256``: calls of ``hashlib.sha256`` (every module binding of it);
- ``encodes``: calls of ``canonical_bytes``, ``pretty_canonical_bytes`` and
  ``dependency_proof_order`` (a digest's own encode included), and of
  ``composed_canonical_bytes``, which joins parts already encoded;
- ``encoded_bytes``: the bytes the encoders returned (a composition's are its
  parts', so they are not counted again).
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import types
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts import candidates, canonical
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.ledger import recovery
from cruxible_core.proposals import settlement
from tests.core_support import _pc_c_support
from tests.core_support._subject_kind_support import core_assigned
from tests.test_procedures import test_procedure_proposal_delivery as proposal_support
from tests.test_procedures import test_procedure_settlement_delivery as settle_support
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import (
    SUBJECT_KIND,
    item_template,
)

N = 12
#: Ceilings per settled item, a little over what this build measures. A 400-item
#: settle measured 108 SHA-256, 200 encodes and 284 KB encoded per item before the
#: encode-once batch (encodes then counted canonical_bytes alone); this test's
#: slope after it was 34.5, 122 and 84 KB (encodes now also counting the proof
#: reference sort key and compositions, which canonical_bytes used to do). The brief's
#: targets (15, 40 and 40 KB) are not met yet; lower these as later stages close
#: the gap. ``CRUXIBLE_PER_ITEM_REPORT=<path>`` writes the measurements.
PER_ITEM = {"sha256": 40, "encodes": 140, "encoded_bytes": 100 * 1024}
#: A small settle after a large one: at most this times one after a small one...
LARGE_PARENT_RATIO = 1.25
#: ...plus this, for ids, paths and shard directories one item longer or deeper.
FLAT_SLACK = {"sha256": 16, "encodes": 16, "encoded_bytes": 8 * 1024}

_FAN_OUT = {"items": f"$steps.{fixtures.SOURCE_ALIAS}.content.json.proposals"}


class _Counts:
    """Counted encodes, encoded bytes and SHA-256s of the measuring thread."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.thread: int | None = None

    def measuring(self) -> bool:
        return self.thread is not None and threading.get_ident() == self.thread


_COUNTS = _Counts()


def _encoding(original: Callable[[object], bytes]) -> Callable[[object], bytes]:
    def counted(value: object) -> bytes:
        encoded = original(value)
        if _COUNTS.measuring():
            _COUNTS.counts["encodes"] += 1
            _COUNTS.counts["encoded_bytes"] += len(encoded)
        return encoded

    return counted


def _composing(original: Callable[..., bytes]) -> Callable[..., bytes]:
    def counted(*args: Any, **kwargs: Any) -> bytes:
        if _COUNTS.measuring():
            _COUNTS.counts["encodes"] += 1
        return original(*args, **kwargs)

    return counted


def _hashing(original: Callable[..., Any]) -> Callable[..., Any]:
    def counted(*args: Any, **kwargs: Any) -> Any:
        if _COUNTS.measuring():
            _COUNTS.counts["sha256"] += 1
        return original(*args, **kwargs)

    return counted


@contextmanager
def _counted() -> Iterator[Counter[str]]:
    """Rebind every module global naming a counted function, for one window."""

    originals: list[Any] = [
        canonical.canonical_bytes,
        canonical.pretty_canonical_bytes,
        candidates.dependency_proof_order,
        canonical.composed_canonical_bytes,
        hashlib.sha256,
    ]
    wrappers = {
        id(originals[0]): _encoding(originals[0]),
        id(originals[1]): _encoding(originals[1]),
        id(originals[2]): _encoding(originals[2]),
        id(originals[3]): _composing(originals[3]),
        id(originals[4]): _hashing(originals[4]),
    }
    by_wrapper = {id(wrapper): original for original, wrapper in zip(originals, wrappers.values())}
    sites: list[tuple[dict[str, Any], str, Any]] = []
    for module in list(sys.modules.values()):
        if not isinstance(module, types.ModuleType):
            continue
        for key, value in list(module.__dict__.items()):
            if any(value is original for original in originals):
                sites.append((module.__dict__, key, value))
                module.__dict__[key] = wrappers[id(value)]
    _COUNTS.counts = Counter()
    _COUNTS.thread = threading.get_ident()
    try:
        yield _COUNTS.counts
    finally:
        _COUNTS.thread = None
        for namespace, key, original in sites:
            namespace[key] = original
        # A module first imported inside the window bound a wrapper by name.
        for module in list(sys.modules.values()):
            if not isinstance(module, types.ModuleType):
                continue
            for key, value in list(module.__dict__.items()):
                original = by_wrapper.get(id(value))
                if original is not None:
                    module.__dict__[key] = original


def _subject_id(index: int) -> str:
    return f"adv-{index:05d}"


def _create_subjects(instance: Any, owner: Any, count: int) -> None:
    members = {
        subject_path(SUBJECT_KIND, _subject_id(index)): render_subject(
            SubjectShell(
                identity=ArtifactIdentity(
                    kind="Subject", name=f"{SUBJECT_KIND}/{_subject_id(index)}"
                ),
                subject_kind=SUBJECT_KIND,
                subject_id=_subject_id(index),
            )
        )
        for index in range(count)
    }
    fixtures._accept_more(
        instance,
        owner,
        core_assigned(instance, members, actor_id="owner"),
        name="per-item-subjects",
    )


def _item(index: int) -> dict[str, object]:
    statement = {
        **item_template()["statement"],  # type: ignore[dict-item]
        "subject": SemanticAddress.whole_artifact(
            subject_path(SUBJECT_KIND, _subject_id(index))
        ).model_dump(mode="json"),
        "object": {"kind": "literal", "value": ("high", "low", "medium")[index % 3]},
    }
    return item_template(statement=statement, rationale=f"The feed reports {index}.")


def _settle(world: Any, first: int, count: int, batch: int) -> Counter[str]:
    instance, root, line = world
    proposals = [_item(index) for index in range(first, first + count)]
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical.canonical_bytes({**fixtures.ADVISORY, "proposals": proposals})
    )
    with _counted() as counts:
        state = settle_support.run_settle(
            instance, root, line, now=fixtures.NOW + timedelta(minutes=10 * (batch + 1))
        )
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "settled", egress
    assert len(egress.target_paths) == count, egress
    return Counter(counts)


def _forget_parsed_receipts() -> None:
    settlement._CHANGE_SET_PARSES.clear()
    recovery.reset_released_records()


def _roomy_selection_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture contract selects at most 4 KB; a fan-out of 2N items needs more."""

    original = _pc_c_support.capture_contract

    def roomy(*args: Any, **kwargs: Any) -> Any:
        contract = original(*args, **kwargs)
        budget = contract.selection_budget.model_copy(update={"max_bytes": 1 << 20})
        return contract.model_copy(update={"selection_budget": budget})

    for module in (_pc_c_support, fixtures, proposal_support, settle_support):
        if hasattr(module, "capture_contract"):
            monkeypatch.setattr(module, "capture_contract", roomy)


def test_a_settles_work_per_item_stays_at_its_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roomy_selection_budget(monkeypatch)
    instance, root, line = settle_support.settle_world(tmp_path, candidate_templates=_FAN_OUT)
    _create_subjects(instance, settle_support.OWNERS[instance.root], 5 * N)
    world = (instance, root, line)
    _settle(world, 0, N, 0)
    _forget_parsed_receipts()
    small = _settle(world, N, N, 1)
    _forget_parsed_receipts()
    large = _settle(world, 2 * N, 2 * N, 2)
    _forget_parsed_receipts()
    after_large = _settle(world, 4 * N, N, 3)

    slope = {name: (large[name] - small[name]) / N for name in PER_ITEM}
    report = (
        f"per item {slope}; small {dict(small)}; large {dict(large)}; "
        f"after large {dict(after_large)}"
    )
    destination = os.environ.get("CRUXIBLE_PER_ITEM_REPORT")
    if destination:
        Path(destination).write_text(report + "\n")
    for name, ceiling in PER_ITEM.items():
        assert slope[name] <= ceiling, f"{name} grew to {slope[name]:.1f} per item: {report}"
    for name, slack in FLAT_SLACK.items():
        assert after_large[name] <= small[name] * LARGE_PARENT_RATIO + slack, (
            f"{name}: a small settle after a large one cost {after_large[name]} against "
            f"{small[name]} after a small one: {report}"
        )
