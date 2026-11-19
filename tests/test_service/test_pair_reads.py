"""Served reads of pair Subjects (slice S5): ends as Subject refs, an end's pairs, pair kinds.

- A pair's card serves each end as a typed ref (its key part, the end's
  ``kind/id`` and the endpoint Claim), read from ``pair_ends``: a retired pair
  still names its ends. Its endpoint key Claims are not repeated as fields.
- An end's card lists the pairs naming it, per pair kind and key part, at most
  ``GET_CARD_PAIRS`` each; ``detail="pairs"`` pages them all. Only pairs
  holding a live fact are listed (the pair index); a historical shell is
  reached through ``incoming``.
- A SubjectKind card says it is a pair kind and lists its ends' kinds;
  ``orient`` shows the same per SubjectKind and in each kind's identity.

The listing is checked against a reference computed from every Claim, and
the CLI, MCP and SDK surfaces render the same service answer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.get_reads import (
    GET_CARD_PAIRS,
    GetEndPairs,
    GetKindEnd,
    GetPairEnd,
    GetPairs,
    GetRequest,
    GetResult,
    GetSubjectCard,
    GetSubjectKindCard,
)
from cruxible_client.contracts.orient import OrientKindEnd
from cruxible_core.cli.main import cli
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.list_pages import ListCursorMismatch
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._pair_reference import reference_pairs
from tests.core_support._pair_world import (
    APP_EXPOSURE,
    COMPONENT,
    EXPOSURE,
    INVENTORY,
    OCCURRENCE,
    VULN,
    accepted,
    create,
    pair_world,
    write,
)

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)
#: Exposures of the one component, each with a justification.
EXPOSED = 7
C1 = f"{COMPONENT}/pkg-pypi-requests-2.31.0"
C2 = f"{COMPONENT}/pkg-pypi-urllib3-2.0.0"
INV = f"{INVENTORY}/web"


def _vuln(index: int) -> str:
    return f"{VULN}/cve-2024-{index}"


def _exposure(component: str, index: int) -> str:
    return f"{EXPOSURE}/{component.split('/')[1]}--cve-2024-{index}"


OCC = f"{OCCURRENCE}/web--pkg-pypi-requests-2.31.0"
APP = f"{APP_EXPOSURE}/web--pkg-pypi-requests-2.31.0--cve-2024-0"
#: A pair whose only fact was retired: a historical shell of C1.
SHELL = _exposure(C1, 90)
#: A pair retired with its Claims.
GONE = _exposure(C2, 0)


def _justified(label: str) -> dict[str, Any]:
    return {
        "op": "set",
        "subject": {"created": label},
        "field": "justification",
        "value": "vulnerable_code_not_present",
    }


@pytest.fixture(scope="module")
def instance(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    built = pair_world(tmp_path_factory.mktemp("pair-reads"))
    changes: list[dict[str, Any]] = [
        create("c", COMPONENT, "pkg:pypi/requests@2.31.0"),
        create("c2", COMPONENT, "pkg:pypi/urllib3@2.0.0"),
        create("i", INVENTORY, "web"),
        create("o", OCCURRENCE, {"inventory": {"created": "i"}, "component": {"created": "c"}}),
        {"op": "set", "subject": {"created": "o"}, "field": "scope", "value": "required"},
    ]
    for index in (*range(EXPOSED), 90):
        changes += [
            create(f"v{index}", VULN, f"CVE-2024-{index}"),
            create(
                f"e{index}",
                EXPOSURE,
                {"component": {"created": "c"}, "vulnerability": {"created": f"v{index}"}},
            ),
            _justified(f"e{index}"),
        ]
    changes += [
        create(
            "a", APP_EXPOSURE, {"occurrence": {"created": "o"}, "vulnerability": {"created": "v0"}}
        ),
        {
            "op": "set",
            "subject": {"created": "a"},
            "field": "justification",
            "value": "component_not_present",
        },
        create("g", EXPOSURE, {"component": {"created": "c2"}, "vulnerability": {"created": "v0"}}),
        _justified("g"),
    ]
    outcome = accepted(write(built, *changes))
    shell_fact = next(
        item.claim
        for item in outcome.changes
        if item.op == "set" and item.subject == SHELL and item.field == "justification"
    )
    accepted(write(built, {"op": "retire", "target": shell_fact, "reason": "was-wrong"}))
    accepted(write(built, {"op": "retire", "target": GONE, "with_claims": True}))
    return built


def _get(instance: PlaybillInstance, ref: str, **fields: Any) -> GetResult:
    return service_playbill_get(instance, request=GetRequest(ref=ref, **fields), access=_ACCESS)


def _card(instance: PlaybillInstance, ref: str) -> Any:
    card = _get(instance, ref).card
    assert card is not None
    return card


def _reference_pairs(instance: PlaybillInstance, end: str) -> dict[tuple[str, str], list[str]]:
    return reference_pairs(instance, end, at=datetime.now(UTC)).listed


# -- a pair's card ------------------------------------------------------------------


def test_a_pair_card_serves_each_end_as_a_typed_ref_in_key_order(
    instance: PlaybillInstance,
) -> None:
    card = _card(instance, _exposure(C1, 3))

    assert isinstance(card, GetSubjectCard)
    assert [(end.part, end.subject) for end in card.ends] == [
        ("component", C1),
        ("vulnerability", _vuln(3)),
    ]
    assert all(isinstance(end, GetPairEnd) and end.claim for end in card.ends)
    # Each end's Claim is its endpoint key Claim, and is not repeated as a field.
    for end in card.ends:
        claim = _card(instance, str(end.claim))
        assert claim.subject == _exposure(C1, 3) and claim.value == end.subject
    assert [row.predicate for row in card.claims] == ["justification"]
    # A pair of a pair: the app exposure's end is the occurrence.
    app = _card(instance, APP)
    assert [(end.part, end.subject) for end in app.ends] == [
        ("occurrence", OCC),
        ("vulnerability", _vuln(0)),
    ]
    # A Subject that is no pair has no ends.
    assert _card(instance, C1).ends == ()
    assert "ends" not in _get(instance, C1).model_dump(mode="json")["card"]


def test_a_retired_pair_still_names_its_ends_without_claims(instance: PlaybillInstance) -> None:
    card = _card(instance, GONE)

    assert card.lifecycle == "retired"
    # The endpoint key Claims retired with the pair; its ends stay named.
    assert [(end.part, end.subject, end.claim) for end in card.ends] == [
        ("component", C2, None),
        ("vulnerability", _vuln(0), None),
    ]


# -- an end's card and its pairs ------------------------------------------------------


def test_an_end_card_lists_its_pairs_per_kind_and_part_bounded(instance: PlaybillInstance) -> None:
    card = _card(instance, C1)

    assert [(group.kind, group.part) for group in card.pairs] == [
        (EXPOSURE, "component"),
        (OCCURRENCE, "component"),
    ]
    exposures, occurrences = card.pairs
    assert isinstance(exposures, GetEndPairs)
    assert exposures.pairs == tuple(_exposure(C1, index) for index in range(GET_CARD_PAIRS))
    assert exposures.more is True
    assert occurrences.pairs == (OCC,) and occurrences.more is False
    # The shell (its only fact retired) is not a live pair; incoming still reaches it.
    assert SHELL not in exposures.pairs
    assert card.next[-1] == f'cruxible_get(ref="{C1}", detail="pairs")'
    incoming = _get(instance, C1, detail="incoming", limit=200).incoming
    assert incoming is not None and SHELL in {row.subject for row in incoming.claims}
    # A nested end: the occurrence lists its app exposures; a vuln its exposures.
    assert [(group.kind, group.part, group.pairs) for group in _card(instance, OCC).pairs] == [
        (APP_EXPOSURE, "occurrence", (APP,)),
    ]
    vuln = _card(instance, _vuln(0))
    assert [(group.kind, group.part) for group in vuln.pairs] == [
        (APP_EXPOSURE, "vulnerability"),
        (EXPOSURE, "vulnerability"),
    ]
    # The retired pair is gone from its ends' cards.
    assert vuln.pairs[1].pairs == (_exposure(C1, 0),)
    assert _card(instance, C2).pairs == ()
    # A card that cuts no group suggests no pairs read.
    assert not any("pairs" in step for step in vuln.next)


def _pages(instance: PlaybillInstance, ref: str, *, limit: int) -> list[GetResult]:
    pages = [_get(instance, ref, detail="pairs", limit=limit)]
    while pages[-1].next_cursor is not None:
        pages.append(_get(instance, ref, detail="pairs", limit=limit, cursor=pages[-1].next_cursor))
    return pages


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 8, 200])
def test_pairs_detail_pages_every_live_pair_once_matching_the_reference(
    instance: PlaybillInstance, limit: int
) -> None:
    pages = _pages(instance, C1, limit=limit)

    assert all(isinstance(page.pairs, GetPairs) for page in pages)
    assert [page.truncated for page in pages] == [True] * (len(pages) - 1) + [False]
    listed: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str, str]] = []
    for page in pages:
        assert page.pairs is not None
        assert sum(len(group.pairs) for group in page.pairs.groups) <= limit
        for group in page.pairs.groups:
            assert group.more is False
            if group.pairs:
                listed.setdefault((group.kind, group.part), []).extend(group.pairs)
            order.extend((group.kind, group.part, pair) for pair in group.pairs)
    assert listed == _reference_pairs(instance, C1)
    assert order == sorted(order) and len(order) == EXPOSED + 1


def test_pairs_detail_refuses_a_foreign_cursor_and_a_non_subject(
    instance: PlaybillInstance,
) -> None:
    first = _get(instance, C1, detail="pairs", limit=1)
    assert first.next_cursor is not None
    with pytest.raises(ListCursorMismatch):
        _get(instance, _vuln(0), detail="pairs", cursor=first.next_cursor)
    incoming = _get(instance, C1, detail="incoming", limit=1)
    with pytest.raises(ListCursorMismatch):
        _get(instance, C1, detail="pairs", cursor=incoming.next_cursor)
    with pytest.raises(Exception, match="pairs"):
        _get(instance, f"SubjectKind:{EXPOSURE}", detail="pairs")
    # A vulnerability whose one exposure is a shell names no live pair.
    empty = _get(instance, _vuln(90), detail="pairs")
    assert empty.pairs is not None and empty.pairs.groups == () and not empty.truncated


# -- pair kinds -----------------------------------------------------------------------


def test_a_subject_kind_card_says_it_is_a_pair_kind_and_lists_its_ends(
    instance: PlaybillInstance,
) -> None:
    card = _card(instance, f"SubjectKind:{APP_EXPOSURE}")

    assert isinstance(card, GetSubjectKindCard) and card.pair is True
    assert card.ends == (
        GetKindEnd(key=f"{APP_EXPOSURE}.occurrence", kind=OCCURRENCE),
        GetKindEnd(key=f"{APP_EXPOSURE}.vulnerability", kind=VULN),
    )
    plain = _card(instance, f"SubjectKind:{COMPONENT}")
    assert plain.pair is False and plain.ends == ()
    assert {"pair", "ends"}.isdisjoint(
        _get(instance, f"SubjectKind:{COMPONENT}").model_dump(mode="json")["card"]
    )
    with pytest.raises(ValueError, match="pair kind"):
        GetSubjectKindCard.model_validate({**card.model_dump(), "pair": False})


def test_orient_shows_pair_kinds_and_their_end_kinds(instance: PlaybillInstance) -> None:
    kinds = service_playbill_orient(instance, section="subject_kinds", limit=200).subject_kinds
    assert kinds is not None
    by_kind = {item.kind: item for item in kinds}
    assert by_kind[EXPOSURE].ends == (
        OrientKindEnd(key=f"{EXPOSURE}.component", kind=COMPONENT),
        OrientKindEnd(key=f"{EXPOSURE}.vulnerability", kind=VULN),
    )
    assert by_kind[COMPONENT].ends == ()
    detail = service_playbill_orient(instance, kind=EXPOSURE).kind_detail
    assert detail is not None
    assert detail.identity == (
        f"keys: {EXPOSURE}.component: subject<{COMPONENT}>, "
        f"{EXPOSURE}.vulnerability: subject<{VULN}> (unique)"
    )
    plain = service_playbill_orient(instance, kind=COMPONENT).kind_detail
    assert plain is not None and plain.identity == f"keys: {COMPONENT}.purl (unique)"


# -- the surfaces ---------------------------------------------------------------------


class _ServiceClient:
    """A transport whose get is the real service over one instance."""

    def __init__(self, instance: PlaybillInstance) -> None:
        self.instance = instance
        self.requests: list[GetRequest] = []

    def get(self, instance_id: str, *, request: GetRequest) -> Any:
        self.requests.append(request)
        return service_playbill_get(self.instance, request=request, access=_ACCESS)


def test_the_cli_prints_ends_pairs_and_the_pairs_pages(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _ServiceClient(instance)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    prefix = ["--server-url", "http://server", "--instance-id", "inst"]

    pair = CliRunner().invoke(cli, [*prefix, "get", _exposure(C1, 3)])
    assert pair.exit_code == 0, pair.output
    assert f"  component      -> {C1}  CLM-" in pair.output
    assert f"  vulnerability  -> {_vuln(3)}  CLM-" in pair.output
    assert "  justification  vulnerable_code_not_present" in pair.output

    end = CliRunner().invoke(cli, [*prefix, "get", C1])
    assert end.exit_code == 0, end.output
    shown = ", ".join(_exposure(C1, index) for index in range(GET_CARD_PAIRS))
    assert f"pairs {EXPOSURE} by component: {shown}, ..." in end.output
    assert f"pairs {OCCURRENCE} by component: {OCC}\n" in end.output
    assert f"next: cruxible get {C1} --detail pairs" in end.output

    paged = CliRunner().invoke(cli, [*prefix, "get", C1, "--detail", "pairs", "--limit", "2"])
    assert paged.exit_code == 0, paged.output
    assert f"{_exposure(C1, 0)}  {EXPOSURE}  by component" in paged.output
    assert f"next: cruxible get {C1} --detail pairs --cursor " in paged.output
    assert client.requests[-1].detail == "pairs" and client.requests[-1].limit == 2

    kind = CliRunner().invoke(cli, [*prefix, "get", f"SubjectKind:{EXPOSURE}"])
    assert kind.exit_code == 0, kind.output
    assert "\npair: true\n" in kind.output, kind.output
    assert f"  {EXPOSURE}.component  {COMPONENT}" in kind.output


def test_the_mcp_tool_reads_pairs(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.mcp import handlers
    from cruxible_core.mcp.server import create_server
    from tests.test_mcp.test_playbill_protocol_curation import _protocol_session, _run

    monkeypatch.setattr(handlers, "_get_client", lambda: _ServiceClient(instance))
    server = create_server()

    async def exercise() -> str:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool(
                "cruxible_get", {"instance_id": "inst", "ref": C1, "detail": "pairs", "limit": 3}
            )
            assert not result.isError
            return " ".join(block.text for block in result.content if hasattr(block, "text"))

    body = json.loads(_run(exercise()))
    assert body["pairs"]["groups"][0]["pairs"] == [_exposure(C1, index) for index in range(3)]
    assert body["truncated"] is True and body["next_cursor"]


def test_the_sdk_reads_every_pairs_page_and_joins_a_cut_group(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pages of three: the exposures group is cut twice.
    monkeypatch.setattr("cruxible_core.service.discovery.get.GET_HISTORY_DEFAULT_LIMIT", 3)
    playbill = Cruxible.__new__(Cruxible)
    client = _ServiceClient(instance)
    playbill._client = client  # type: ignore[assignment]
    playbill._instance_id = "inst"
    playbill._read_at = lambda coordinate=None: None  # type: ignore[method-assign,assignment]
    playbill._evaluation_time = lambda: datetime.now(UTC).isoformat()  # type: ignore[method-assign]
    playbill._observe_read = lambda *_a, **_k: None  # type: ignore[method-assign]

    whole = playbill.get(C1, detail="pairs").value

    assert isinstance(whole, GetPairs)
    assert {(group.kind, group.part): list(group.pairs) for group in whole.groups} == (
        _reference_pairs(instance, C1)
    )
    assert len(whole.groups) == 2
    assert [request.detail for request in client.requests] == ["pairs"] * 3
