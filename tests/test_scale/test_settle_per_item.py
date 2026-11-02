"""A fan-out settle's work per item: SHA-256s, canonical encodes and canonical bytes.

``test_scale_guard`` checks that a route does not grow with the instance. This
checks the other axis of the cost model: that the work a settle does for each
item it settles stays near its floor (one Claim built, its digests taken once,
its bytes written once, one journal record, one entry in the change set), so a
re-encode or re-hash of one item's payloads cannot creep back unnoticed.

Each case includes subject.within. One creates Claims in empty slots; the other
pre-seeds live Claims and measures eligible revisions. Each runs four batches:

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
Before each measured settle the parsed change-set receipts and every memo added
by the revise-cost slice are forgotten, including derivations attached to
retained models. Their cold work must be paid inside each measured window;
seeding or an earlier case cannot warm it. Pre-existing immutable artifact
parses retain their ordinary content-addressed ownership.

Counted on the measuring thread only:

- ``sha256``: calls of ``hashlib.sha256`` (every module binding of it);
- ``encodes``: calls of ``canonical_bytes``, ``pretty_canonical_bytes`` and
  ``dependency_proof_order`` (a digest's own encode included), and of
  ``composed_canonical_bytes``, which joins parts already encoded;
- ``encoded_bytes``: the bytes the encoders returned (a composition's are its
  parts', so they are not counted again);
- ``sql_connects``: SQLite connections opened to databases under the world's
  root (a resident index connection opened once per process is not per item);
- ``sql_statements``: statements SQLite ran on those connections (one per
  execution, so ``executemany`` counts every row, and each trigger firing
  counts once), whenever the connection was opened;
- ``fsyncs`` and ``file_opens``: whole-settle observations. ``non_acceptance_fsyncs`` excludes the
  acceptance batch and ledger Git-object flushes (reported separately);
- ``journal_fsyncs``, ``journal_file_opens``, ``journal_appends``: the complete
  terminal item group, including its reservations and CAS publication. The
  group has no fsyncs, at most two opens per new body (one file and its shard),
  and one journal append regardless of the number of items.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import sqlite3
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
from pydantic import BaseModel

from cruxible_client.contracts import candidates, canonical, claims, projection_extensions
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.exhaust.journal_index import JournalIndex
from cruxible_core.ledger import activation, recovery
from cruxible_core.procedures.execution import ProcedureExecutor
from cruxible_core.proposals import settlement
from tests.core_support import _light_capture_support
from tests.core_support._subject_kind_support import core_assigned
from tests.test_procedures import test_procedure_proposal_delivery as proposal_support
from tests.test_procedures import test_procedure_settlement_delivery as settle_support
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import (
    SUBJECT_KIND,
)
from tests.test_scale._counters import _database_path, _prefixes, _under

N = 12
#: Ceilings per settled item, a little over what this build measures. A 400-item
#: settle measured 108 SHA-256, 200 encodes and 284 KB encoded per item before the
#: encode-once batch (encodes then counted canonical_bytes alone); this test's
#: slope after it was 34.5, 122 and 84 KB (encodes now also counting the proof
#: reference sort key and compositions, which canonical_bytes used to do). The brief's
#: targets (15, 40 and 40 KB) are not met yet; lower these as later stages close
#: the gap. ``CRUXIBLE_PER_ITEM_REPORT=<path>`` writes the measurements.
#: SQL: a settle opens no connection per item (journal, history and envelope
#: indexes keep one per process; snapshot readers are pooled), and runs 43
#: statements per item here (a 400-item settle ran 155 per item, 5.3 connects,
#: before resident index connections and one-statement-per-table writes).
PER_ITEM = {
    "claim_parses": 1,
    # Stage 5 target remains 15. Round 3 removes repeated admission, address,
    # proof and explanation construction; keep the observed ceiling strict.
    "model_builds": 67,
    "projection_normalizations": 180,
    "sha256": 40,
    "encodes": 140,
    "encoded_bytes": 100 * 1024,
    "sql_connects": 0.25,
    "sql_statements": 35,
    "non_acceptance_fsyncs": 0.1,
    "journal_fsyncs": 0,
    # One payload file plus one shard handle per body; the small-N slope also
    # includes the difference in shard collisions between the two batches.
    "journal_file_opens": 2.5,
    "journal_appends": 0.1,
}
#: A small settle after a large one: at most this times one after a small one...
LARGE_PARENT_RATIO = 1.25
#: ...plus this, for ids, paths and shard directories one item longer or deeper.
FLAT_SLACK = {
    "claim_parses": 2,
    "model_builds": 24,
    "digest_validations": 24,
    "projection_normalizations": 32,
    "sha256": 16,
    "encodes": 16,
    "encoded_bytes": 8 * 1024,
    "sql_connects": 2,
    "sql_statements": 16,
    "journal_fsyncs": 0,
    "journal_file_opens": 16,
    "journal_appends": 1,
}


class _Counts:
    """Counted encodes, encoded bytes and SHA-256s of the measuring thread."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.thread: int | None = None
        self.in_item_group = False
        self.in_acceptance_flush = False

    def measuring(self) -> bool:
        return self.thread is not None and threading.get_ident() == self.thread


_COUNTS = _Counts()


@contextmanager
def _objects_counted() -> Iterator[None]:
    """Count real cold work, including Pydantic entry points, on this thread.

    Memo hits are deliberately absent. Model construction includes direct
    constructors, validation, copies and unchecked construction, as in the
    census; a shortcut through model_construct cannot hide the work.
    """
    models = {
        getattr(BaseModel, name).__code__
        for name in (
            "__init__",
            "model_validate",
            "model_validate_json",
            "model_copy",
            "model_construct",
        )
    }
    codes = {
        claims._parse_claim.__code__: "claim_parses",
        canonical._valid_sha256_hex.__wrapped__.__code__: "digest_validations",
        projection_extensions.normalize_projection_value.__code__: "projection_normalizations",
    }
    previous = sys.getprofile()

    def count(frame: Any, event: str, arg: Any) -> None:
        if event != "call" or not _COUNTS.measuring():
            return
        code = frame.f_code
        if code in models:
            _COUNTS.counts["model_builds"] += 1
            value = frame.f_locals.get("self", frame.f_locals.get("cls"))
            name = value.__name__ if isinstance(value, type) else type(value).__name__
            _COUNTS.counts["model:" + name] += 1
            caller = frame.f_back
            if caller is not None:
                site = f"{caller.f_code.co_filename}:{caller.f_lineno}"
                _COUNTS.counts[f"model_site:{name}:{site}"] += 1
        elif code in codes:
            _COUNTS.counts[codes[code]] += 1

    sys.setprofile(count)
    try:
        yield
    finally:
        sys.setprofile(previous)


def _open_event(event: str, args: tuple[object, ...]) -> None:
    if event == "open" and _COUNTS.measuring():
        _COUNTS.counts["file_opens"] += 1
        if _COUNTS.in_item_group:
            _COUNTS.counts["journal_file_opens"] += 1


sys.addaudithook(_open_event)


def _flushing(original: Callable[..., Any]) -> Callable[..., Any]:
    def counted(*args: Any, **kwargs: Any) -> Any:
        if _COUNTS.measuring():
            _COUNTS.counts["fsyncs"] += 1
            caller = sys._getframe(1)
            _COUNTS.counts[f"fsync_site:{caller.f_code.co_filename}:{caller.f_lineno}"] += 1
            if _COUNTS.in_acceptance_flush:
                _COUNTS.counts["acceptance_fsyncs"] += 1
            else:
                frame = sys._getframe(1)
                while frame is not None and not frame.f_code.co_filename.endswith("/ledger/git.py"):
                    frame = frame.f_back
                category = "git_fsyncs" if frame is not None else "non_acceptance_fsyncs"
                _COUNTS.counts[category] += 1
            if _COUNTS.in_item_group:
                _COUNTS.counts["journal_fsyncs"] += 1
        return original(*args, **kwargs)

    return counted


@contextmanager
def _journal_counted() -> Iterator[None]:
    original = ProcedureExecutor._append_events
    append = LocalJournalBackend.append_many
    flush = activation.flush_acceptance
    read = JournalIndex.read_row

    def read_row(self: Any, row: Any) -> Any:
        if _COUNTS.measuring():
            _COUNTS.counts["journal_record_reads"] += 1
        return read(self, row)

    def barrier(self: Any, *args: Any, **kwargs: Any) -> Any:
        _COUNTS.in_acceptance_flush = True
        try:
            return flush(self, *args, **kwargs)
        finally:
            _COUNTS.in_acceptance_flush = False

    def group(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not _COUNTS.measuring():
            return original(self, *args, **kwargs)
        _COUNTS.in_item_group = True
        try:
            return original(self, *args, **kwargs)
        finally:
            _COUNTS.in_item_group = False

    def append_many(self: Any, drafts: Any, **kwargs: Any) -> Any:
        if _COUNTS.measuring() and _COUNTS.in_item_group and drafts:
            _COUNTS.counts["journal_appends"] += 1
            _COUNTS.counts["journal_records"] += len(drafts)
        return append(self, drafts, **kwargs)

    activation.flush_acceptance = barrier
    ProcedureExecutor._append_events = group
    LocalJournalBackend.append_many = append_many
    JournalIndex.read_row = read_row
    try:
        yield
    finally:
        activation.flush_acceptance = flush
        ProcedureExecutor._append_events = original
        LocalJournalBackend.append_many = append
        JournalIndex.read_row = read


def _encoding(original: Callable[[object], bytes]) -> Callable[[object], bytes]:
    def counted(value: object) -> bytes:
        encoded = original(value)
        if _COUNTS.measuring():
            _COUNTS.counts["encodes"] += 1
            _COUNTS.counts["encoded_bytes"] += len(encoded)
            caller = sys._getframe(1)
            site = f"{caller.f_code.co_filename}:{caller.f_lineno}"
            _COUNTS.counts["encode_site:" + site] += 1
            _COUNTS.counts["encode_bytes_site:" + site] += len(encoded)
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


def _statement(sql: str) -> None:
    if _COUNTS.measuring():
        _COUNTS.counts["sql_statements"] += 1
        # Report executions by shape as well as the strict total, so a newly
        # measured revise path names its indexed per-Claim work explicitly.
        shape = re.sub(r"'(?:[^']|'')*'|\b\d+\b", "?", sql)
        _COUNTS.counts["sql:" + shape] += 1


@contextmanager
def _sql_traced(root: Path) -> Iterator[None]:
    """Trace every SQLite connection opened under ``root`` until the block exits.

    Connections outlive a measured window (resident index connections, pooled
    snapshot readers), so each is traced from the moment it opens; only the
    measuring thread's statements and connects inside a window count.
    """

    prefixes = _prefixes(root)
    original = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original(*args, **kwargs)
        database = _database_path(
            args[0] if args else kwargs.get("database"), uri=bool(kwargs.get("uri"))
        )
        if database is not None and _under(database, prefixes):
            if _COUNTS.measuring():
                _COUNTS.counts["sql_connects"] += 1
            connection.set_trace_callback(_statement)
        return connection

    sqlite3.connect = connect  # type: ignore[assignment]
    try:
        yield
    finally:
        sqlite3.connect = original  # type: ignore[assignment]


@contextmanager
def _counted() -> Iterator[Counter[str]]:
    """Rebind every module global naming a counted function, for one window."""

    originals: list[Any] = [
        canonical.canonical_bytes,
        canonical.pretty_canonical_bytes,
        candidates.dependency_proof_order,
        canonical.composed_canonical_bytes,
        hashlib.sha256,
        os.fsync,
    ]
    wrappers = {
        id(originals[0]): _encoding(originals[0]),
        id(originals[1]): _encoding(originals[1]),
        id(originals[2]): _encoding(originals[2]),
        id(originals[3]): _composing(originals[3]),
        id(originals[4]): _hashing(originals[4]),
        id(originals[5]): _flushing(originals[5]),
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


def _item(index: int, *, revise: bool = False) -> dict[str, object]:
    """Recorded source rows carry facts; the core Claims template owns writes."""
    return {
        "id": _subject_id(index),
        "severity": ("high", "low", "medium")[(index + int(revise)) % 3],
    }


def _settle(
    world: Any, first: int, count: int, batch: int, *, revise: bool = False
) -> Counter[str]:
    instance, root, line = world
    proposals = [_item(index, revise=revise) for index in range(first, first + count)]
    (root / fixtures.RELATIVE_PATH).write_bytes(canonical.canonical_bytes({"proposals": proposals}))
    with _counted() as counts, _journal_counted(), _objects_counted():
        state = settle_support.run_settle(
            instance, root, line, now=fixtures.NOW + timedelta(minutes=10 * (batch + 1))
        )
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "settled", egress
    assert len(egress.target_paths) == count, egress
    return Counter(counts)


def _forget_parsed_receipts() -> None:
    from cruxible_client.contracts import claim_verdicts, content_memo
    from cruxible_core.claims import closure
    from cruxible_core.compiler import projection_artifacts
    from cruxible_core.query import engine, explanation

    settlement._CHANGE_SET_PARSES.clear()
    recovery.reset_released_records()
    memos = (
        closure._PROOFS,
        candidates._PROOF_ENCODINGS,
        claim_verdicts._CONTROL_COMPONENTS,
        projection_artifacts._SOURCE_MAPPINGS,
        projection_artifacts._PIN_VALUES,
        explanation._HISTORY_VALUES,
        engine._ROW_BINDINGS,
        engine._TRUNCATIONS,
    )
    for memo in memos:
        memo.clear()
    # These derivations were also added by the revise-cost slice, attached to
    # the immutable parses retained by pre-existing process caches. Forget the
    # new derived work without discarding the parsed artifacts themselves.
    for entry in tuple(content_memo._SEALED.values()):
        for key in tuple(entry.derived):
            namespace = key[0] if isinstance(key, tuple) else key
            if namespace in {
                "core.subject_dependency_state",
                "core.claim_dependency_state",
                "query.explanation.record-members",
                "cruxible_client.contracts.candidates.dependency_proof_order",
            }:
                del entry.derived[key]
    # Prior test worlds can own cycles whose SQLite finalizers checkpoint their
    # old proposal stores. Drain those owners before counting the next world's
    # write, rather than charging unrelated cleanup to a random measured batch.
    # The current world's reachable owners and the cold memos stay untouched.
    gc.collect()
    assert all(not memo._entries for memo in memos), "measured settle must start with cold memos"


def _roomy_selection_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture contract selects at most 4 KB; a fan-out of 2N items needs more."""

    original = _light_capture_support.record_contract

    def roomy(*args: Any, **kwargs: Any) -> Any:
        contract = original(*args, **kwargs)
        budget = contract.selection_budget.model_copy(update={"max_bytes": 1 << 20})
        return contract.model_copy(update={"selection_budget": budget})

    for module in (fixtures, proposal_support, settle_support):
        if hasattr(module, "capture_contract"):
            monkeypatch.setattr(module, "capture_contract", roomy)


def _measure_work_per_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, revise: bool, count: int
) -> tuple[Counter[str], Counter[str], Counter[str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _roomy_selection_budget(monkeypatch)
    with _sql_traced(tmp_path):
        instance, root, line = settle_support.settle_world(
            tmp_path,
            claims_terminal=True,
            claims_within=tuple(
                subject_path(SUBJECT_KIND, _subject_id(i)) for i in range(5 * count)
            ),
        )
        _create_subjects(instance, settle_support.OWNERS[instance.root], 5 * count)
        world = (instance, root, line)
        _settle(world, 0, count, 0)
        if revise:
            _settle(world, count, 4 * count, 5)
        _forget_parsed_receipts()
        small = _settle(world, count, count, 1, revise=revise)
        _forget_parsed_receipts()
        large = _settle(world, 2 * count, 2 * count, 2, revise=revise)
        _forget_parsed_receipts()
        after_large = _settle(world, 4 * count, count, 3, revise=revise)

    return small, large, after_large


@pytest.mark.parametrize("revise", [False, True], ids=["create-within", "revise-within"])
def test_a_settles_work_per_item_stays_at_its_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revise: bool
) -> None:
    small, large, after_large = _measure_work_per_item(
        tmp_path, monkeypatch, revise=revise, count=N
    )

    assert small["journal_records"] == N
    assert large["journal_records"] == 2 * N
    assert small["journal_appends"] == large["journal_appends"] == 1
    slope = {name: (large[name] - small[name]) / N for name in PER_ITEM}
    observed = {
        name: (large[name] - small[name]) / N
        for name in (
            "fsyncs",
            "file_opens",
            "acceptance_fsyncs",
            "git_fsyncs",
            "non_acceptance_fsyncs",
            "digest_validations",
            "projection_normalizations",
        )
    }
    report = (
        f"per item {slope}; observed whole settle {observed}; "
        f"small {dict(small)}; large {dict(large)}; "
        f"after large {dict(after_large)}"
    )
    destination = os.environ.get("CRUXIBLE_PER_ITEM_REPORT")
    if destination:
        suffix = ".revise" if revise else ".create"
        Path(destination + suffix).write_text(report + "\n")
    violations = [
        f"{name} grew to {slope[name]:.1f} per item (ceiling {ceiling})"
        for name, ceiling in PER_ITEM.items()
        if slope[name] > ceiling
    ]
    for name, slack in FLAT_SLACK.items():
        if after_large[name] > small[name] * LARGE_PARENT_RATIO + slack:
            violations.append(
                f"{name}: after large {after_large[name]} against after small {small[name]}"
            )
    assert not violations, f"{violations}: {report}"


def test_settle_cost_per_claim_is_flat_at_12_and_48(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slopes = {}
    counters = (*PER_ITEM, "file_opens", "journal_record_reads")
    for revise in (False, True):
        case = "revise" if revise else "create"
        slopes[case] = {}
        for count in (12, 48):
            small, large, after = _measure_work_per_item(
                tmp_path / case / str(count), monkeypatch, revise=revise, count=count
            )
            slopes[case][count] = {name: (large[name] - small[name]) / count for name in counters}
            assert small["journal_records"] == count
            assert large["journal_records"] == 2 * count
            assert small["journal_appends"] == large["journal_appends"] == 1
            for name, ceiling in PER_ITEM.items():
                assert slopes[case][count][name] <= ceiling, (case, count, name, slopes)
            for name, slack in FLAT_SLACK.items():
                assert after[name] <= small[name] * LARGE_PARENT_RATIO + slack, (
                    case,
                    count,
                    name,
                    small[name],
                    after[name],
                )
    destination = os.environ.get("CRUXIBLE_PER_ITEM_REPORT")
    if destination:
        Path(destination + ".scaling").write_text(json.dumps(slopes, indent=2) + "\n")
    # Marginal costs cancel per-delivery setup, including one predecessor run
    # read. Enlarging both the run and instance fourfold must leave each
    # per-Claim slope flat: an O(run) cost per Claim fails this comparison.
    growing = {
        (case, name): (values[12][name], values[48][name])
        for case, values in slopes.items()
        for name in counters
        if values[48][name] > values[12][name] * 1.1 + (1024 if name == "encoded_bytes" else 0.1)
    }
    assert not growing, growing


def test_a_small_commit_after_a_large_one_does_not_encode_the_parent_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.runtime import instance as instance_module
    from tests.core_support._support import initialize_local
    from tests.test_authoring.test_authoring_change_set_intents import (
        _accept,
        _change_set,
        _claim,
        _coordinator,
    )
    from tests.test_authoring.test_authoring_preflight import TIMESTAMP, _seed_claim_surface

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    prepare = instance_module.prepare_generation
    measured: list[Counter[str]] = []

    def counted_prepare(*args: Any, **kwargs: Any) -> Any:
        before = Counter(_COUNTS.counts)
        result = prepare(*args, **kwargs)
        measured.append(Counter(_COUNTS.counts) - before)
        return result

    monkeypatch.setattr(instance_module, "prepare_generation", counted_prepare)

    def commit(first: int, count: int) -> Counter[str]:
        payload = _change_set(
            *(_claim(qualifier=f"parent-size-{i}") for i in range(first, first + count))
        )
        _forget_parsed_receipts()
        before = len(measured)
        with _counted() as counts, _objects_counted():
            intent = coordinator.create(
                actor=actor, payload=payload, canonical_timestamp=TIMESTAMP
            ).intent
            _accept(instance, owner, coordinator, intent.intent_id, actor)
        assert len(measured) == before + 1
        return Counter(counts)

    commit(0, 1)
    small = commit(1, 1)
    commit(2, 2 * N)
    after_large = commit(2 + 2 * N, 1)
    report = (
        f"whole small {dict(small)}; whole after large {dict(after_large)}; "
        f"preparation {[dict(c) for c in measured]}"
    )
    destination = os.environ.get("CRUXIBLE_WHOLE_WRITE_REPORT")
    if destination:
        Path(destination).write_text(report + "\n")
    violations = []
    for label, prior, after in (
        ("whole write", small, after_large),
        ("preparation", measured[1], measured[3]),
    ):
        for name in ("encodes", "encoded_bytes", "claim_parses"):
            if after[name] > prior[name] * LARGE_PARENT_RATIO + FLAT_SLACK[name]:
                violations.append((label, name, prior[name], after[name]))
    assert not violations, (violations, report)


def test_reobservation_same_value_new_bytes_visits_each_item_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-R8: one admission per changed row, one cited-envelope read per batch."""
    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore
    from cruxible_core.storage.cas import ContentAddressedBodyStore

    _roomy_selection_budget(monkeypatch)
    observations = []
    original_admit = ClaimReobservationStore._admissible
    original_read = ContentAddressedBodyStore.read
    active = [False]
    counters: Counter[str] = Counter()
    cited: set[str] = set()

    def admit(self: Any, **kwargs: Any) -> Any:
        counters["items"] += 1
        active[0] = True
        try:
            return original_admit(self, **kwargs)
        finally:
            active[0] = False

    def read(self: Any, digest: str, **kwargs: Any) -> Any:
        if active[0] and digest in cited:
            counters["cited_reads"] += 1
        return original_read(self, digest, **kwargs)

    monkeypatch.setattr(ClaimReobservationStore, "_admissible", admit)
    monkeypatch.setattr(ContentAddressedBodyStore, "read", read)
    for count in (N, 2 * N):
        directory = tmp_path / str(count)
        directory.mkdir()
        instance, root, line = settle_support.settle_world(directory, claims_terminal=True)
        _create_subjects(instance, settle_support.OWNERS[instance.root], count)
        _settle((instance, root, line), 0, count, 0)
        from cruxible_client.contracts.claims import parse_claim

        tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
        claims = [parse_claim(tree[path], path=path) for path in tree if path.startswith("claims/")]
        cited.clear()
        cited.update(capture for claim in claims for capture in claim.backing.capture_digests)
        head = instance.accepted_coordinate()
        counters.clear()
        (root / fixtures.RELATIVE_PATH).write_bytes(
            canonical.canonical_bytes(
                {
                    "proposals": [_item(index) for index in range(count)],
                    "unrelated_catalog_bytes": "changed without changing any observed field",
                }
            )
        )
        state = settle_support.run_settle(
            instance, root, line, now=fixtures.NOW + timedelta(minutes=20)
        )
        assert state.status == "succeeded", state.terminal
        (egress,) = state.terminal_egress
        assert egress.proposal_id is None and len(egress.children) == count
        assert instance.accepted_coordinate() == head
        assert counters == {"items": count, "cited_reads": 1}
        observations.append(dict(counters))
    assert observations[1]["items"] == 2 * observations[0]["items"]
    assert observations[1]["cited_reads"] == observations[0]["cited_reads"] == 1


def _measure_absence_work(tmp_path, monkeypatch, *, policy, count):
    """Absence stays propose-only even with a settle grant; measure that real door."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _roomy_selection_budget(monkeypatch)
    with _sql_traced(tmp_path):
        instance, root, line = settle_support.settle_world(
            tmp_path,
            claims_terminal=True,
            claims_absence=policy,
            change_kinds=("create", "revise", "retire"),
            claims_within=tuple(subject_path(SUBJECT_KIND, _subject_id(i)) for i in range(count)),
        )
        _create_subjects(instance, settle_support.OWNERS[instance.root], count)
        world = (instance, root, line)
        _settle(world, 0, count, 0)
        (root / fixtures.RELATIVE_PATH).write_bytes(canonical.canonical_bytes({"proposals": []}))
        _forget_parsed_receipts()
        with _counted() as counts, _journal_counted(), _objects_counted():
            state = settle_support.run_settle(
                instance, root, line, now=fixtures.NOW + timedelta(hours=2)
            )
        assert state.status == "succeeded", state.terminal
        (egress,) = state.terminal_egress
        assert egress.settle_outcome == "proposed", egress
        assert egress.outside_scope_claim_types == (proposal_support.PREDICATE,)
        assert len(egress.target_paths) == count, egress
        evaluation = instance.proposal_evidence().read_evaluation(egress.proposal_id)
        assert evaluation.candidate_digest is not None, evaluation.diagnostics
    return Counter(counts)


@pytest.mark.parametrize("policy", ("mirror", "inverse"))
def test_absence_per_item_work_stays_at_its_floor(tmp_path, monkeypatch, policy):
    slopes = {}
    measured = {}
    for n in (12, 48):
        small = _measure_absence_work(
            tmp_path / str(n) / "small", monkeypatch, policy=policy, count=n
        )
        large = _measure_absence_work(
            tmp_path / str(n) / "large", monkeypatch, policy=policy, count=2 * n
        )
        slopes[n] = {
            name: (large[name] - small[name]) / n
            for name in (*PER_ITEM, "file_opens", "journal_record_reads")
        }
        measured[n] = {"small": dict(small), "large": dict(large), "slopes": slopes[n]}
        destination = os.environ.get("CRUXIBLE_PER_ITEM_REPORT")
        if destination:
            Path(destination + f".absence-{policy}").write_text(json.dumps(measured, indent=2))
        for name, ceiling in PER_ITEM.items():
            assert slopes[n][name] <= ceiling, (policy, n, name, slopes, dict(small), dict(large))
    growing = {
        name: (slopes[12][name], slopes[48][name])
        for name in slopes[12]
        if slopes[48][name] > slopes[12][name] * 1.1 + (1024 if name == "encoded_bytes" else 0.1)
    }
    assert not growing, growing
