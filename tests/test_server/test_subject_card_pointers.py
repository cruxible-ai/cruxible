"""A Subject's card points at what else concerns it, on every surface (F24, F25).

A note about a Subject was reachable only through ``backlinks``, which its card
never named; a held proposal (``accept="never"``) left the card showing the old
value with no mention, and its ``next`` row named only a digest. The card now
names the backlinks read when there are any, lists the open proposals that would
write its fields, and the proposal's ``next`` row names the Subject and fields.
The own-words evidence row also says how much text it holds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.files import add_catalog_entry
from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.test_server.test_sdk_reference_examples import _connect, _example

SUBJECT = "shop.part/bp-2210"


@pytest.fixture
def shop(playbill_http: tuple[TestClient, str, Path], tmp_path: Path) -> tuple[Cruxible, str]:
    """The SDK reference's vocabulary, with one part whose status is in stock."""

    client, instance_id, _key = playbill_http
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    notes = workspace / "notes.md"
    notes.write_text("# Notes\n", encoding="utf-8")
    add_catalog_entry(workspace, name="notes", path=notes, fields={})
    cx = _connect(client, instance_id, workspace)
    namespace: dict[str, Any] = {"cx": cx}
    exec(compile(_example("### Defining a vocabulary"), "README.md", "exec"), namespace)  # noqa: S102
    assert namespace["outcome"].status == "accepted"
    return cx, instance_id


def _card(instance_id: str, surface: str) -> Any:
    return playbill_api.playbill_get(
        instance_id, request=GetRequest(ref=SUBJECT, surface=surface)
    ).card


def test_the_card_names_backlinks_only_once_something_links_to_it(
    shop: tuple[Cruxible, str],
) -> None:
    cx, instance_id = shop

    assert not any("backlinks" in step for step in cx.get(SUBJECT).value.next)

    cx.note("notes.md", text="## Supplier\nShips from Ohio.\n", about=[SUBJECT], because="Noted.")

    assert f'cx.backlinks("{SUBJECT}")' in cx.get(SUBJECT).value.next
    assert f"cruxible backlinks {SUBJECT}" in _card(instance_id, "cli").next
    assert f'cruxible_get(ref="{SUBJECT}", detail="backlinks")' in _card(instance_id, "mcp").next


def test_a_held_proposal_shows_on_the_card_and_its_next_row_names_the_field(
    shop: tuple[Cruxible, str],
) -> None:
    from cruxible_core.service.discovery.next import service_playbill_next
    from tests.test_integration.test_next_closed_loop import _request

    cx, instance_id = shop
    held = cx.set(SUBJECT, "status", "discontinued", because="Supplier stopped.", accept="never")
    assert held.proposal is not None
    proposal_id = held.proposal.proposal_id

    card = cx.get(SUBJECT).value
    assert [(item.proposal, item.fields) for item in card.pending] == [(proposal_id, ("status",))]
    assert f'cx.get("{proposal_id}")' in card.next
    # The accepted value stays the accepted one until the proposal is accepted.
    assert next(row.value for row in card.claims if row.predicate == "status") == "in_stock"
    # A pinned read names accepted state only, even pinned to the head's own OID.
    head = get_playbill_manager().get(instance_id).accepted_coordinate().git_oid
    pinned = playbill_api.playbill_get(
        instance_id, request=GetRequest(ref=SUBJECT, at=head, surface="cli")
    ).card
    assert pinned is not None and pinned.pending == ()
    assert not any(proposal_id in step for step in pinned.next)

    instance = get_playbill_manager().get(instance_id)
    actor = instance.proposal_evidence().read_admission(proposal_id).actor_id
    rows = [
        item
        for item in service_playbill_next(
            instance,
            request=_request(instance, evaluation_time=datetime.now(UTC)),
            caller_principal_id=actor,
        ).items
        if item.subject_identity == proposal_id
    ]
    assert rows, "the held proposal has no next row for its author"
    assert all(item.detail["touches"] == [f"{SUBJECT} shop.part.status"] for item in rows)

    from cruxible_core.cli.commands.playbill import _next_claim_about

    assert _next_claim_about(rows[0]) == f"{SUBJECT} shop.part.status"


def test_the_cli_card_lists_the_pending_proposal(
    shop: tuple[Cruxible, str], capsys: pytest.CaptureFixture[str]
) -> None:
    from cruxible_core.cli.commands import playbill as cli_playbill

    cx, instance_id = shop
    held = cx.set(SUBJECT, "status", "discontinued", because="Supplier stopped.", accept="never")
    assert held.proposal is not None
    result = playbill_api.playbill_get(instance_id, request=GetRequest(ref=SUBJECT, surface="cli"))
    capsys.readouterr()

    cli_playbill._emit_get_text(result)

    text = capsys.readouterr().out
    assert f"pending {held.proposal.proposal_id}: status (by " in text
    assert f"next: cruxible get {held.proposal.proposal_id}" in text


def test_the_own_words_row_says_how_many_bytes_it_holds(shop: tuple[Cruxible, str]) -> None:
    cx, instance_id = shop
    claim = next(row.claim for row in cx.get(SUBJECT).value.claims if row.predicate == "status")

    evidence = playbill_api.playbill_get(
        instance_id, request=GetRequest(ref=claim, detail="evidence", surface="cli")
    ).evidence
    assert evidence is not None
    (own_words,) = [item for item in evidence.captures if item.origin == "self_source"]
    assert own_words.byte_length is not None and own_words.byte_length > 0


def _pending(instance_id: str) -> list[tuple[str, tuple[str, ...]]]:
    card = _card(instance_id, "sdk")
    return [(item.proposal, item.fields) for item in card.pending]


def test_a_proposal_leaves_the_card_when_it_is_withdrawn_or_accepted(
    shop: tuple[Cruxible, str],
) -> None:
    cx, instance_id = shop
    held = cx.set(SUBJECT, "status", "discontinued", because="Supplier stopped.", accept="never")
    assert held.proposal is not None
    assert _pending(instance_id) == [(held.proposal.proposal_id, ("status",))]

    cx._client.withdraw_proposal(instance_id, held.proposal.proposal_id, reason="Not yet.")  # noqa: SLF001
    assert _pending(instance_id) == []

    again = cx.set(SUBJECT, "status", "discontinued", because="Supplier stopped.", accept="never")
    assert again.proposal is not None
    assert _pending(instance_id) == [(again.proposal.proposal_id, ("status",))]
    cx.activate(again.proposal.proposal_id)
    assert _pending(instance_id) == []


def test_a_readmission_supersedes_its_stale_source_on_the_card(
    shop: tuple[Cruxible, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.core_support._support import every_moved_base_overlaps

    cx, instance_id = shop
    held = cx.set(SUBJECT, "status", "discontinued", because="Supplier stopped.", accept="never")
    assert held.proposal is not None
    source = held.proposal.proposal_id
    # The head moves (elsewhere); the readmission machinery treats the source as stale.
    every_moved_base_overlaps(monkeypatch)
    moved = cx.set("shop.part/p1", "status", "in_stock", because="Stocked.", at=None)
    assert moved.status == "accepted", moved

    readmitted = cx.proposal(source).readmit()
    (successor,) = [proposal for proposal, _fields in _pending(instance_id)]
    assert successor != source
    assert successor in repr(readmitted)


def test_an_index_from_before_the_subject_rows_reconstructs_once(
    shop: tuple[Cruxible, str],
) -> None:
    import sqlite3

    cx, instance_id = shop
    held = cx.set(SUBJECT, "status", "discontinued", because="Supplier stopped.", accept="never")
    assert held.proposal is not None
    evidence = get_playbill_manager().get(instance_id).proposal_evidence()
    assert evidence.index is not None
    path = evidence.index.path
    get_playbill_manager().clear()
    with sqlite3.connect(path) as old:
        old.execute("DROP TABLE proposal_subjects")
        old.execute("DROP TABLE proposal_readmissions")

    assert _pending(instance_id) == [(held.proposal.proposal_id, ("status",))]


def test_a_card_read_is_flat_in_unrelated_open_proposals(
    shop: tuple[Cruxible, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding: every card walked every open proposal (1 vs 9 derivations)."""

    import cruxible_core.service.proposals.proposals as proposals
    import cruxible_core.service.proposals.staleness as staleness
    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
    from tests.test_scale._counters import COUNTERS, counting, trace_sqlite
    from tests.test_scale.test_scale_guard import RATIO, SLACK

    cx, instance_id = shop
    # The app's consumer runner opens the instance through the manager, traced
    # below, and its SQL beside a window's step-counter installs deadlocks both
    # (a CI shard hung here to its job timeout). This test measures a card read.
    get_playbill_manager().consumer_runner.close()
    candidates_read: list[str] = []
    original = ProposalEvidenceStore.read_candidate

    def read_candidate(self: ProposalEvidenceStore, digest: str) -> Any:
        candidates_read.append(digest)
        return original(self, digest)

    # The metric the finding counted: per-proposal derivations during one card read.
    derived: list[str] = []
    for module, name in (
        (proposals, "proposal_touches"),
        (proposals, "_open_rows"),
        (staleness, "proposal_change_keys"),
    ):
        real = getattr(module, name)
        monkeypatch.setattr(
            module,
            name,
            lambda *args, _real=real, _name=name, **kwargs: (
                derived.append(_name),
                _real(*args, **kwargs),
            )[1],
        )

    measured: dict[int, dict[str, int]] = {}
    written = 0
    for size in (1, 9):
        while written < size:
            held = cx.set(
                f"shop.part/other-{written}",
                "status",
                "discontinued",
                because="Held.",
                accept="never",
            )
            assert held.proposal is not None
            written += 1
        with trace_sqlite(tmp_path):
            get_playbill_manager().clear()
            assert _pending(instance_id) == []  # warm: the instance reopens traced
            monkeypatch.setattr(ProposalEvidenceStore, "read_candidate", read_candidate)
            candidates_read.clear()
            derived.clear()
            with counting(tmp_path) as window:
                assert _pending(instance_id) == []
            monkeypatch.setattr(ProposalEvidenceStore, "read_candidate", original)
            assert window.unexplained == [], window.unexplained
            assert candidates_read == []
            assert derived == [], derived
            measured[size] = {name: window.counts[name] for name in COUNTERS}
    grown = {
        name: (measured[1][name], measured[9][name])
        for name in COUNTERS
        if measured[9][name] > measured[1][name] * RATIO + SLACK[name]
    }
    assert not grown, grown
