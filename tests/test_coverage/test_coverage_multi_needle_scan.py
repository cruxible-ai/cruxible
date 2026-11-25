"""The shared multi-needle pass and the unchanged-source memo.

The working-occurrence overlay searches every source once for every retained
needle and answers unchanged sources from a memo. Neither may change what the
overlay says: with enough budget, every overlay here must equal, model for
model, the one a naive per-needle search builds; and the budget a scan is
charged must be a pure function of its inputs, never of the memo's state.
"""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Iterable, Iterator, Sequence

import pytest

from cruxible_core.coverage import indexes
from cruxible_core.coverage.contracts import (
    CoverageCommitmentScanProof,
    LogicalSourceIdentity,
    occurrence_identity_digest,
)
from cruxible_core.coverage.indexes import (
    CoverageScanBudget,
    WorkingOccurrence,
    WorkingOccurrenceOverlayV2,
    WorkingSourceCommitmentV1,
    WorkingSourceContent,
    build_working_occurrence_overlay,
    working_occurrence_overlay_digest,
)

UNBOUNDED = CoverageScanBudget(max_scanned_bytes=10**15)
ROADMAP = LogicalSourceIdentity(plane="external", identity="docs.roadmap")
FEEDBACK = LogicalSourceIdentity(plane="external", identity="docs.feedback")
NOTES = LogicalSourceIdentity(plane="external", identity="docs.notes")
ANCHOR = indexes._NEEDLE_ANCHOR_BYTES

Wanted = tuple[str, int, bytes | None]


@pytest.fixture(autouse=True)
def _cold_memo() -> Iterator[None]:
    indexes._clear_scan_memo()
    yield
    indexes._clear_scan_memo()


def _sha(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _wanted(needles: Iterable[bytes], *, retained: bool = True) -> tuple[Wanted, ...]:
    return tuple(
        sorted({(_sha(needle), len(needle), needle if retained else None) for needle in needles})
    )


def _reference_overlay(
    sources: Sequence[tuple[LogicalSourceIdentity, bytes]],
    wanted: Sequence[Wanted],
) -> WorkingOccurrenceOverlayV2:
    """A naive, unbudgeted search: one `bytes.find` walk per needle per source."""

    commitments: list[WorkingSourceCommitmentV1] = []
    found: dict[tuple[bytes, str], set[tuple[int, int]]] = {}
    proofs: list[CoverageCommitmentScanProof] = []
    starts: dict[bytes, Sequence[int]] = {}
    for source, content in sources:
        key = source.sort_key
        starts[key] = indexes._line_starts(content)
        whole = _sha(content)
        commitments.append(
            WorkingSourceCommitmentV1(source=source, content_digest=whole, byte_length=len(content))
        )
        found.setdefault((key, whole), set()).add((0, len(content)))
        for digest, length, needle in wanted:
            if length == 0:
                continue
            proofs.append(
                CoverageCommitmentScanProof(
                    source=source, commitment_digest=digest, byte_length=length
                )
            )
            if length > len(content):
                continue
            spans = found.setdefault((key, digest), set())
            if needle is not None:
                offset = content.find(needle)
                while offset >= 0:
                    spans.add((offset, offset + length))
                    offset = content.find(needle, offset + 1)
            else:
                for offset in range(len(content) - length + 1):
                    if _sha(content[offset : offset + length]) == digest:
                        spans.add((offset, offset + length))
    by_key = {source.sort_key: source for source, _ in sources}
    occurrences = [
        WorkingOccurrence(
            source=by_key[key],
            observed_commitment_digest=digest,
            byte_length=end - start,
            ordinal=ordinal,
            identity_digest=occurrence_identity_digest(
                source=by_key[key], observed_commitment_digest=digest, ordinal=ordinal
            ),
            line_overlay=indexes._overlay(starts[key], start_byte=start, end_byte=end),
        )
        for (key, digest), spans in found.items()
        for ordinal, (start, end) in enumerate(sorted(spans))
    ]
    return WorkingOccurrenceOverlayV2(
        sources=tuple(sorted(commitments, key=lambda item: item.source.sort_key)),
        occurrences=tuple(sorted(occurrences, key=lambda item: item.sort_key)),
        source_scan_proofs=tuple(sorted(set(proofs), key=lambda item: item.sort_key)),
    )


def _overlay(
    sources: Sequence[tuple[LogicalSourceIdentity, bytes]],
    wanted: Sequence[Wanted],
    budget: CoverageScanBudget = UNBOUNDED,
) -> WorkingOccurrenceOverlayV2:
    return build_working_occurrence_overlay(
        tuple(WorkingSourceContent(source=source, content=content) for source, content in sources),
        wanted=wanted,
        budget=budget,
    )


def _assert_matches_reference(
    sources: Sequence[tuple[LogicalSourceIdentity, bytes]],
    wanted: Sequence[Wanted],
) -> WorkingOccurrenceOverlayV2:
    expected = _reference_overlay(sources, wanted)
    for _ in ("cold memo", "warm memo"):
        actual = _overlay(sources, wanted)
        assert actual == expected
        assert working_occurrence_overlay_digest(actual) == working_occurrence_overlay_digest(
            expected
        )
    return expected


def _prose(rng: random.Random, size: int) -> bytes:
    words = [b"plan", b"review", b"r\xc3\xa9sum\xc3\xa9", b"na\xc3\xafve", b"\xe2\x9c\x93", b"- "]
    out = bytearray()
    while len(out) < size:
        out += rng.choice(words) + rng.choice((b" ", b"\n", b""))
    return bytes(out[:size])


# -- equivalence with the naive search -------------------------------------


def test_overlapping_prefix_related_and_repeated_needles_match_the_reference() -> None:
    content = b"aaaaabaaaabaaab" * 3 + b"\n" + b"abab" * 9
    needles = [b"a", b"aa", b"aaa", b"aab", b"ab", b"aba", b"abab", b"b", b"zz", content]
    overlay = _assert_matches_reference([(ROADMAP, content)], _wanted(needles))
    aa = [o for o in overlay.occurrences if o.observed_commitment_digest == _sha(b"aa")]
    assert len(aa) > 1  # overlapping occurrences are each an ordinal


def test_needles_sharing_a_long_anchor_are_each_verified_in_full() -> None:
    shared = b"x" * (ANCHOR + 8)
    first, second, third = shared + b"-one", shared + b"-two", shared[: ANCHOR - 3]
    content = b"\n".join((first, b"filler", second, second, shared, b"x" * (ANCHOR * 3)))
    overlay = _assert_matches_reference(
        [(ROADMAP, content)], _wanted((first, second, third, shared + b"-absent"))
    )
    assert sum(o.observed_commitment_digest == _sha(second) for o in overlay.occurrences) == 2


def test_multibyte_utf8_offsets_are_byte_offsets() -> None:
    passage = "Résumé ✓ naïve — reviewed".encode()
    content = "前置き\n".encode() + passage + b"\n" + passage[3:] + "\n終わり".encode()
    overlay = _assert_matches_reference(
        [(ROADMAP, content)], _wanted((passage, passage[3:], "✓".encode(), "é".encode()))
    )
    (occurrence,) = (
        o for o in overlay.occurrences if o.observed_commitment_digest == _sha(passage)
    )
    assert occurrence.line_overlay.start_byte == len("前置き\n".encode())
    assert occurrence.line_overlay.start_line == 2


def test_needle_and_fallback_routes_mix_across_sources_and_lengths() -> None:
    roadmap = b"alpha beta gamma\nbeta gamma delta\n"
    feedback = b"gamma"
    retained = _wanted((b"beta gamma", b"gamma", b"alpha beta gamma\n"))
    fallback = _wanted((b"delta", b"alpha"), retained=False)
    _assert_matches_reference(
        [(ROADMAP, roadmap), (FEEDBACK, feedback), (NOTES, b"")],
        tuple(sorted(retained + fallback)),
    )


def test_randomized_snapshots_match_the_reference() -> None:
    rng = random.Random(20261123)
    for _ in range(60):
        sources = [
            (source, _prose(rng, rng.randint(0, 600)))
            for source in rng.sample((ROADMAP, FEEDBACK, NOTES), rng.randint(1, 3))
        ]
        needles: set[bytes] = set()
        for _source, content in sources:
            for _ in range(rng.randint(0, 12)):
                if not content:
                    break
                start = rng.randrange(len(content))
                needle = content[start : start + rng.randint(1, 80)]
                needles.add(needle)
                if rng.random() < 0.2:
                    needles.add(needle + b"#absent")
        _assert_matches_reference(sources, _wanted(needles))


def test_edited_and_relocated_sources_match_the_reference() -> None:
    rng = random.Random(7)
    passages = [b"- " + _prose(rng, rng.randint(40, 200)) + b"\n" for _ in range(40)]
    original = b"".join(passages)
    wanted = _wanted(passage.rstrip(b"\n") for passage in passages)
    _assert_matches_reference([(ROADMAP, original)], wanted)

    relocated = b"# moved\n" + b"".join(reversed(passages[10:])) + b"".join(passages[:10])
    duplicated = original + passages[3]  # one passage now occurs twice
    edited = original.replace(passages[5], passages[5][:-5] + b"EDIT\n")
    truncated = original[: len(original) // 2]
    for content in (relocated, duplicated, edited, truncated):
        _assert_matches_reference([(ROADMAP, content)], wanted)


# -- the budget ------------------------------------------------------------


def test_one_source_is_charged_its_length_once_for_all_its_needles() -> None:
    content = b"one two three one"
    needles = (b"one", b"three", b"two")  # sorted digest order is irrelevant here
    # source once + each needle once + each verified candidate
    exact = len(content) + sum(len(n) + content.count(n) * len(n) for n in needles)

    admitted = _overlay(
        [(ROADMAP, content)], _wanted(needles), CoverageScanBudget(max_scanned_bytes=exact)
    )
    starved = _overlay(
        [(ROADMAP, content)], _wanted(needles), CoverageScanBudget(max_scanned_bytes=exact - 1)
    )

    assert admitted.truncated is False and len(admitted.source_scan_proofs) == 3
    assert starved.truncation_reason_codes == ("scan_budget_exceeded",)
    assert len(starved.source_scan_proofs) == 2


def test_a_needle_is_charged_its_own_length_once_across_sources() -> None:
    needle = b"shared passage"
    roadmap, feedback = b"a shared passage", b"shared passage, shared passage"
    exact = (
        len(feedback)
        + len(needle)
        + 2 * len(needle)  # FEEDBACK sorts first
        + len(roadmap)
        + 1 * len(needle)
    )

    admitted = _overlay(
        [(ROADMAP, roadmap), (FEEDBACK, feedback)],
        _wanted((needle,)),
        CoverageScanBudget(max_scanned_bytes=exact),
    )
    starved = _overlay(
        [(ROADMAP, roadmap), (FEEDBACK, feedback)],
        _wanted((needle,)),
        CoverageScanBudget(max_scanned_bytes=exact - 1),
    )

    assert admitted.truncated is False
    assert starved.scanned(FEEDBACK, _sha(needle), len(needle)) is True
    assert starved.scanned(ROADMAP, _sha(needle), len(needle)) is False


def test_anchor_false_positives_are_charged_as_verification_work() -> None:
    content = b"a" * 4096
    needle = b"a" * (ANCHOR + 4) + b"b"  # its anchor matches almost everywhere
    candidates = len(content) - len(needle) + 1
    exact = len(content) + len(needle) + candidates * len(needle)

    admitted = _overlay(
        [(ROADMAP, content)], _wanted((needle,)), CoverageScanBudget(max_scanned_bytes=exact)
    )
    starved = _overlay(
        [(ROADMAP, content)], _wanted((needle,)), CoverageScanBudget(max_scanned_bytes=exact - 1)
    )

    assert admitted.scanned(ROADMAP, _sha(needle), len(needle)) is True
    assert starved.truncation_reason_codes == ("scan_budget_exceeded",)


def test_budgeted_overlays_do_not_depend_on_the_memo() -> None:
    rng = random.Random(3)
    sources = [(ROADMAP, _prose(rng, 3000)), (FEEDBACK, _prose(rng, 1500))]
    needles = {content[i : i + 60] for _, content in sources for i in range(0, 1400, 97)}
    wanted = _wanted(needles)
    budgets = [CoverageScanBudget(max_scanned_bytes=size) for size in range(0, 12000, 333)]

    cold = []
    for budget in budgets:
        indexes._clear_scan_memo()
        cold.append(_overlay(sources, wanted, budget))
    _overlay(sources, wanted)  # warm the memo with every complete scan
    warm = [_overlay(sources, wanted, budget) for budget in budgets]

    assert warm == cold
    assert any(item.truncated for item in cold) and not cold[-1].truncated


def test_pathological_repetition_stops_at_the_budget() -> None:
    content = b"a" * 20_000
    needles = [b"a" * size for size in (1, 2, 3, 5, 8, ANCHOR, ANCHOR + 1, 200)]
    single = len(content) + 1 + len(content) * 1  # the source, b"a", and every hit of it

    started = time.perf_counter()
    overlay = _overlay(
        [(ROADMAP, content)], _wanted(needles), CoverageScanBudget(max_scanned_bytes=single)
    )
    elapsed = time.perf_counter() - started

    assert overlay.truncation_reason_codes == ("scan_budget_exceeded",)
    assert overlay.scanned(ROADMAP, _sha(b"a"), 1) is True
    assert overlay.scanned(ROADMAP, _sha(b"a" * 200), 200) is False
    assert elapsed < 20


# -- scale -----------------------------------------------------------------


def _citation_dense_document(size: int, citations: int) -> tuple[bytes, list[bytes]]:
    rng = random.Random(size + citations)
    words = [f"w{index}".encode() for index in range(4000)]
    lines: list[bytes] = []
    total = 0
    while total < size:
        line = b"- " + b" ".join(rng.choice(words) for _ in range(rng.randint(20, 80))) + b"\n"
        lines.append(line)
        total += len(line)
    cited = [lines[index].rstrip(b"\n") for index in rng.sample(range(len(lines)), citations)]
    return b"".join(lines), cited


@pytest.mark.parametrize("size", [500_000, 2_000_000])
def test_a_citation_dense_document_fits_the_default_budget_unchanged_and_edited(
    size: int,
) -> None:
    content, cited = _citation_dense_document(size, 250)
    wanted = _wanted(cited)
    edited = b"# a new heading\n\n" + content.replace(cited[0], cited[0][:-4] + b" (revised)")

    for snapshot in (content, edited):
        overlay = _overlay([(ROADMAP, snapshot)], wanted, CoverageScanBudget())
        assert overlay.truncated is False
        assert len(overlay.source_scan_proofs) == len(cited)
        assert overlay == _reference_overlay([(ROADMAP, snapshot)], wanted)
    # The edit relocated every passage and revised one cited passage away.
    assert not any(
        o.observed_commitment_digest == _sha(cited[0])
        for o in _overlay([(ROADMAP, edited)], wanted, CoverageScanBudget()).occurrences
    )


def test_the_charge_grows_with_source_bytes_not_bytes_times_citations() -> None:
    def threshold(content: bytes, cited: Sequence[bytes]) -> int:
        """The least budget that admits every proof, found by bisection."""

        wanted = _wanted(cited)
        low, high = 0, 1 << 40
        while low < high:
            middle = (low + high) // 2
            overlay = _overlay(
                [(ROADMAP, content)], wanted, CoverageScanBudget(max_scanned_bytes=middle)
            )
            if overlay.truncated:
                low = middle + 1
            else:
                high = middle
        return low

    small, small_cited = _citation_dense_document(100_000, 20)
    large, large_cited = _citation_dense_document(1_000_000, 200)

    for content, cited in ((small, small_cited), (large, large_cited)):
        expected = len(content) + sum(len(n) * (1 + content.count(n)) for n in cited)
        assert threshold(content, cited) == expected
    # Ten times the bytes and ten times the citations cost about ten times the
    # budget, not a hundred times as a per-citation pass over the source would.
    ratio = threshold(large, large_cited) / threshold(small, small_cited)
    assert ratio < 12
