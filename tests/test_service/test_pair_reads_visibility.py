"""An end's pairs list only pairs holding a visible fact; hidden ones never take page slots.

``pair_units`` proposes every pair holding a live non-key Claim, whatever its
verdict. A card or a ``detail="pairs"`` page lists a candidate only when one
of its live positive facts passes ``claim_row_outcome`` under the listing
policy at the read's instant; the rest are counted ``hidden``. The answer is
checked against a full-fold reference (``_pair_reference``) that judges every
accepted Claim by the same gate, so a read that skipped the gate would differ.

End ``c`` (component) has exposures to ``v0``..``v9`` and ``w00``..``w24``, and
one occurrence:

- visible: ``v0`` and ``v6``..``v9``; ``v5`` holds one visible and one expired
  justification;
- hidden: ``v1`` (not yet effective: uncovered), ``v2`` (expired: stale
  currency), ``v3`` (a stale verdict), ``v4`` (contradicted), ``w00``..``w24``
  (expired: more hidden in a row than one page examines), and the occurrence
  (its scope only a none).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from cruxible_client.contracts.claims import claim_statement_digest, parse_claim
from cruxible_client.contracts.get_reads import GET_CARD_PAIRS, GetRequest, GetResult
from cruxible_core.query import backends
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.pair_reads import CARD_SCAN, page_scan
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._pair_reference import reference_pairs
from tests.core_support._query_reach_world import (
    COMPONENT,
    EXPOSURE,
    FUTURE,
    INVENTORY,
    JUSTIFICATION,
    OCCURRENCE,
    PAST,
    SCOPE,
    VULN,
    WHEN,
    ReachWorld,
)

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)
END = f"{COMPONENT}/c"
REASON = "vulnerable_code_not_present"
VISIBLE = ("v0", "v5", "v6", "v7", "v8", "v9")
HIDDEN_RUN = tuple(f"w{index:02d}" for index in range(25))


def _exposure(vuln: str) -> str:
    return f"{EXPOSURE}/c--{vuln}"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[ReachWorld, dict[str, str]]:
    built = ReachWorld(tmp_path_factory.mktemp("pair-visibility"))
    end = built.subject(COMPONENT, "c")
    pairs = {
        name: built.subject(EXPOSURE, end, built.subject(VULN, name))
        for name in (*(f"v{index}" for index in range(10)), *HIDDEN_RUN)
    }
    patched: dict[str, str] = {}
    for name, pair in pairs.items():
        if name == "v1":
            built.claim(JUSTIFICATION, REASON, pair, effective_from=FUTURE)
        elif name == "v2" or name.startswith("w"):
            built.claim(JUSTIFICATION, REASON, pair, effective_until=PAST)
        elif name in {"v3", "v4"}:
            patched[name] = built.claim(JUSTIFICATION, REASON, pair)
        elif name == "v5":
            built.claim(JUSTIFICATION, REASON, pair)
            built.claim(JUSTIFICATION, REASON, pair, effective_until=PAST)
        else:
            built.claim(JUSTIFICATION, REASON, pair)
    built.claim(SCOPE, None, built.subject(OCCURRENCE, built.subject(INVENTORY, "i0"), end))
    built.accept()
    tree = built.instance.tree_at(built.instance.accepted_coordinate().git_oid)
    verdicts = {
        claim_statement_digest(parse_claim(tree[path], path=path).statement).tagged: verdict
        for name, verdict in (("v3", "stale"), ("v4", "contradicted"))
        for path in (patched[name],)
    }
    return built, verdicts


@contextmanager
def _verdicts(monkeypatch: pytest.MonkeyPatch, verdicts: dict[str, str]) -> Iterator[None]:
    """v3 reads stale and v4 contradicted, on every path through the shared gate."""

    evaluate = backends.evaluate_claim_verdict

    def patched(**kwargs: Any) -> Any:
        found = evaluate(**kwargs)
        verdict = verdicts.get(kwargs["claim_statement_digest"])
        return found if verdict is None else found.model_copy(update={"verdict": verdict})

    with monkeypatch.context() as scope:
        scope.setattr(backends, "evaluate_claim_verdict", patched)
        yield


def _get(built: ReachWorld, **fields: Any) -> GetResult:
    return service_playbill_get(
        built.instance,
        request=GetRequest(ref=END, evaluation_time=WHEN, **fields),
        access=_ACCESS,
    )


def test_the_reference_sees_the_hidden_facts(
    world: tuple[ReachWorld, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    built, verdicts = world
    with _verdicts(monkeypatch, verdicts):
        reference = reference_pairs(built.instance, END, at=WHEN)
    assert reference.listed == {
        (EXPOSURE, "component"): [_exposure(name) for name in VISIBLE],
    }
    assert reference.hidden == {
        *(_exposure(name) for name in ("v1", "v2", "v3", "v4", *HIDDEN_RUN)),
        f"{OCCURRENCE}/i0--c",
    }


def test_a_card_lists_only_pairs_with_a_visible_fact(
    world: tuple[ReachWorld, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    built, verdicts = world
    with _verdicts(monkeypatch, verdicts):
        card = _get(built).card
    assert card is not None
    exposures, occurrences = card.pairs  # type: ignore[union-attr]
    # The card examines CARD_SCAN candidates: v0..v9, six of them visible.
    assert CARD_SCAN == 10 and GET_CARD_PAIRS == 5
    assert exposures.pairs == tuple(_exposure(name) for name in VISIBLE[:GET_CARD_PAIRS])
    assert exposures.hidden == 4
    # More candidates follow (the w run), so the card says more may follow.
    assert exposures.more is True
    # A none holds nothing: the occurrence is counted, never listed.
    assert (occurrences.kind, occurrences.pairs, occurrences.hidden, occurrences.more) == (
        OCCURRENCE,
        (),
        1,
        False,
    )


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 30])
def test_pages_list_every_visible_pair_once_and_count_every_hidden_one(
    world: tuple[ReachWorld, dict[str, str]], monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    built, verdicts = world
    pages: list[GetResult] = []
    with _verdicts(monkeypatch, verdicts):
        reference = reference_pairs(built.instance, END, at=WHEN)
        pages.append(_get(built, detail="pairs", limit=limit))
        while pages[-1].next_cursor is not None:
            pages.append(_get(built, detail="pairs", limit=limit, cursor=pages[-1].next_cursor))
    listed: dict[tuple[str, str], list[str]] = {}
    hidden = 0
    for page in pages:
        assert page.pairs is not None
        rows = sum(len(group.pairs) for group in page.pairs.groups)
        examined = rows + sum(group.hidden for group in page.pairs.groups)
        assert rows <= limit and examined <= page_scan(limit)
        for group in page.pairs.groups:
            if group.pairs:
                listed.setdefault((group.kind, group.part), []).extend(group.pairs)
            hidden += group.hidden
    assert listed == reference.listed
    assert hidden == len(reference.hidden)
    assert [page.truncated for page in pages] == [True] * (len(pages) - 1) + [False]


def test_hidden_candidates_never_take_page_slots(
    world: tuple[ReachWorld, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    built, verdicts = world
    pages = []
    with _verdicts(monkeypatch, verdicts):
        page = _get(built, detail="pairs", limit=1)
        pages.append(page)
        while page.next_cursor is not None:
            page = _get(built, detail="pairs", limit=1, cursor=page.next_cursor)
            pages.append(page)
    shown = [
        (
            [(group.kind, group.pairs, group.hidden) for group in page.pairs.groups],  # type: ignore[union-attr]
            page.truncated,
        )
        for page in pages
    ]

    def one(vuln: str, hidden: int = 0) -> list[tuple[str, tuple[str, ...], int]]:
        return [(EXPOSURE, (_exposure(vuln),), hidden)]

    assert page_scan(1) == 10
    assert shown == [
        # v1..v4 are passed over and counted; v5 waits for the next page.
        (one("v0", 4), True),
        (one("v5"), True),
        (one("v6"), True),
        (one("v7"), True),
        (one("v8"), True),
        # v9, then the page's scan runs out in the hidden run (w00..w08).
        (one("v9", 9), True),
        # Ten hidden in a row: nothing listed, and the next page continues after them.
        ([(EXPOSURE, (), 10)], True),
        ([(EXPOSURE, (), 6), (OCCURRENCE, (), 1)], False),
    ]
