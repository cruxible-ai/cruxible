"""Proposal, policy and curation lists answer bounded pages with cursors."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client import contracts
from cruxible_core.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.service.claims.policies import list_playbill_policies_in_force
from cruxible_core.service.discovery.curation import (
    PlaybillCurationListRequestV1,
    _curation_page,
    service_list_playbill_curation,
)
from cruxible_core.service.list_pages import (
    PlaybillListCursorMismatch,
    PlaybillListCursorStale,
    encode_list_cursor,
    list_snapshot,
)
from cruxible_core.service.proposals.proposals import (
    service_list_playbill_proposals,
    service_withdraw_playbill_proposal,
)
from tests.core_support._claim_authoring_support import service_propose_playbill_claim
from tests.core_support._knowledge_loop_support import TIMESTAMP, activate, authoring, seed_claims

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _propose(instance, name: str, subject_id: str):  # type: ignore[no-untyped-def]
    return service_propose_playbill_claim(
        instance,
        authoring=authoring(subject_id, "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name=name,
        timestamp=TIMESTAMP,
    )


def test_proposal_pages_walk_the_whole_list_at_one_pinned_coordinate(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    _propose(instance, "page-a", "wi-50")
    later = _propose(instance, "page-b", "wi-51")
    whole = service_list_playbill_proposals(instance)
    assert len(whole.entries) >= 3
    assert whole.truncated is False and whole.next_cursor is None

    first = service_list_playbill_proposals(instance, limit=1)
    assert first.truncated is True and first.next_cursor is not None
    # Accepted state moves between pages; the cursor keeps reading its own coordinate.
    activate(instance, owner, later)
    walked = list(first.entries)
    cursor = first.next_cursor
    while cursor is not None:
        page = service_list_playbill_proposals(instance, limit=1, cursor=cursor)
        assert page.coordinate == first.coordinate
        walked.extend(page.entries)
        cursor = page.next_cursor
    assert walked == list(whole.entries)


def test_a_proposal_cursor_is_bound_to_its_selection(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    _propose(instance, "page-a", "wi-50")
    first = service_list_playbill_proposals(instance, limit=1)
    assert first.next_cursor is not None

    with pytest.raises(PlaybillListCursorMismatch, match="different selection"):
        service_list_playbill_proposals(instance, status="open", limit=1, cursor=first.next_cursor)
    with pytest.raises(PlaybillListCursorMismatch, match="not a list cursor"):
        service_list_playbill_proposals(instance, limit=1, cursor="not-a-cursor")


def test_withdrawing_an_unseen_proposal_between_pages_makes_the_cursor_stale(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    _propose(instance, "open-a", "wi-50")
    _propose(instance, "open-b", "wi-51")
    first = service_list_playbill_proposals(instance, status="open", limit=1)
    assert first.next_cursor is not None
    seen = {entry.proposal_id for entry in first.entries}
    unseen = next(
        entry.proposal_id
        for entry in service_list_playbill_proposals(instance, status="open").entries
        if entry.proposal_id not in seen
    )

    service_withdraw_playbill_proposal(
        instance,
        proposal_id=unseen,
        actor_id="owner",
        reason="superseded",
        withdrawn_at=TIMESTAMP,
    )

    with pytest.raises(PlaybillListCursorStale, match="listing changed") as caught:
        service_list_playbill_proposals(instance, status="open", limit=1, cursor=first.next_cursor)
    assert caught.value.error_code == "playbill.list.cursor_stale"


def test_policy_pages_walk_the_whole_inventory(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    whole = list_playbill_policies_in_force(instance)
    assert len(whole.policies) >= 3
    assert whole.truncated is False

    first = list_playbill_policies_in_force(instance, limit=2)
    walked = list(first.policies)
    cursor = first.next_cursor
    assert first.truncated is True and cursor is not None
    while cursor is not None:
        page = list_playbill_policies_in_force(instance, limit=2, cursor=cursor)
        walked.extend(page.policies)
        cursor = page.next_cursor
    assert walked == list(whole.policies)

    other = contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )
    with pytest.raises(PlaybillListCursorMismatch, match="different coordinate"):
        list_playbill_policies_in_force(instance, at=other, cursor=first.next_cursor)
    with pytest.raises(PlaybillListCursorMismatch, match="policies-in-force"):
        service_list_playbill_proposals(instance, cursor=first.next_cursor)


def test_curation_pages_continue_after_the_last_items_place_in_order() -> None:
    items = tuple(
        SimpleNamespace(pattern_kind=kind, subject=SimpleNamespace(qualified=subject), item_id=item)
        for kind, subject, item in (
            ("a.kind", "Subject:x/1", "sha256:1"),
            ("a.kind", "Subject:x/2", "sha256:2"),
            ("b.kind", "Subject:x/1", "sha256:3"),
        )
    )

    first, more = _curation_page(items, after=None, limit=2)  # type: ignore[arg-type]
    assert [item.item_id for item in first] == ["sha256:1", "sha256:2"] and more
    # The boundary item may have been resolved since; the page still continues after it.
    rest, more = _curation_page(
        items[2:],  # type: ignore[arg-type]
        after=("a.kind", "Subject:x/2", "sha256:2"),
        limit=2,
    )
    assert [item.item_id for item in rest] == ["sha256:3"] and not more


def test_a_curation_cursor_refuses_once_accepted_state_moves(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    profile = CoverageAccessProfileV1(profile_id="paging-test")
    actor = GovernedActorContext(
        actor_type="human_user",
        actor_id="curator",
        org_id="org-test",
        operation_id="op-list",
        timestamp=NOW,
    )
    first = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(evaluation_time=NOW, access_profile=profile, limit=1),
        actor_context=actor,
    )
    assert first.truncated is (first.next_cursor is not None)
    cursor = encode_list_cursor(
        list_name="curation",
        coordinate=first.coordinate.model_dump(mode="json"),
        selection={"access_profile": profile.model_dump(mode="json")},
        snapshot=list_snapshot([]),
        last_key=("a.kind", "Subject:x/1", "sha256:1"),
    )
    continued = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW, access_profile=profile, limit=1, cursor=cursor
        ),
        actor_context=actor,
    )
    assert continued.coordinate == first.coordinate

    activate(instance, owner, _propose(instance, "moves-state", "wi-60"))
    with pytest.raises(PlaybillListCursorMismatch, match="accepted state moved"):
        service_list_playbill_curation(
            instance,
            request=PlaybillCurationListRequestV1(
                evaluation_time=NOW, access_profile=profile, limit=1, cursor=cursor
            ),
            actor_context=actor,
        )
