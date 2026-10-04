"""Proposal, policy and curation lists answer bounded pages with cursors."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.claims import LiteralClaimObject, parse_claim, render_claim
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.service.claims.policies import service_playbill_policies_in_force
from cruxible_core.service.discovery.curation import (
    PlaybillCurationListRequestV1,
    service_list_playbill_curation,
)
from cruxible_core.service.list_pages import (
    ListCursorMismatch,
    ListCursorStale,
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

    with pytest.raises(ListCursorMismatch, match="different selection"):
        service_list_playbill_proposals(instance, status="open", limit=1, cursor=first.next_cursor)
    with pytest.raises(ListCursorMismatch, match="not a list cursor"):
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

    with pytest.raises(ListCursorStale, match="listing changed") as caught:
        service_list_playbill_proposals(instance, status="open", limit=1, cursor=first.next_cursor)
    assert caught.value.error_code == "playbill.list.cursor_stale"


def test_policy_pages_walk_the_whole_inventory(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    whole = service_playbill_policies_in_force(instance)
    assert len(whole.policies) >= 3
    assert whole.truncated is False

    first = service_playbill_policies_in_force(instance, limit=2)
    walked = list(first.policies)
    cursor = first.next_cursor
    assert first.truncated is True and cursor is not None
    while cursor is not None:
        page = service_playbill_policies_in_force(instance, limit=2, cursor=cursor)
        walked.extend(page.policies)
        cursor = page.next_cursor
    assert walked == list(whole.policies)

    other = contracts.AcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )
    with pytest.raises(ListCursorMismatch, match="different coordinate"):
        service_playbill_policies_in_force(instance, at=other, cursor=first.next_cursor)
    with pytest.raises(ListCursorMismatch, match="policies-in-force"):
        service_list_playbill_proposals(instance, cursor=first.next_cursor)


_ACTOR = GovernedActorContext(
    actor_type="human_user",
    actor_id="curator",
    org_id="org-test",
    operation_id="op-list",
    timestamp=NOW,
)
_PROFILE = CoverageAccessProfile(profile_id="paging-test")


def _curation(instance, *, limit: int, cursor: str | None = None):  # type: ignore[no-untyped-def]
    return service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW, access_profile=_PROFILE, limit=limit, cursor=cursor
        ),
        actor_context=_ACTOR,
    )


def _curation_cursor(listing) -> str:  # type: ignore[no-untyped-def]
    """A cursor over ``listing`` (the whole queue), cut after its first item."""
    first = listing.items[0] if listing.items else None
    return encode_list_cursor(
        list_name="curation",
        coordinate=listing.coordinate.model_dump(mode="json"),
        selection={"access_profile": _PROFILE.model_dump(mode="json")},
        snapshot=list_snapshot([[item.item_id, item.status] for item in listing.items]),
        last_key=(
            ("", "", "")
            if first is None
            else (first.pattern_kind, first.subject.qualified, first.item_id)
        ),
    )


def _append_refused_proposals(instance) -> None:  # type: ignore[no-untyped-def]
    """Two refused Claim proposals: the admission-failure detector clusters them."""
    valid = _propose(instance, "invalid-claim-template", "wi-44")
    evaluated_oid = valid.proposal.proposal.evaluation.evaluated_tree_oid
    tree = instance.proposal_tree(evaluated_oid)
    claim = parse_claim(tree[valid.claim_path], path=valid.claim_path)
    tree[valid.claim_path] = render_claim(
        claim.model_copy(
            update={
                "statement": claim.statement.model_copy(
                    update={"object": LiteralClaimObject(value=1)}
                )
            }
        )
    )
    base = instance.accepted_coordinate()
    for suffix in ("one", "two"):
        refused = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref=f"refs/proposals/owner/refused-{suffix}",
                proposed_base_oid=base.git_oid,
            ),
            candidate_tree=tree,
            timestamp=TIMESTAMP,
        )
        assert refused.evaluation.verdict == "refused"


def test_a_curation_cursor_refuses_once_the_queue_changes(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    whole = _curation(instance, limit=200)
    cursor = _curation_cursor(whole)
    if whole.items:
        # An unchanged queue continues after the cursor's last item.
        continued = _curation(instance, limit=200, cursor=cursor)
        assert list(continued.items) == list(whole.items[1:])

    _append_refused_proposals(instance)
    grown = _curation(instance, limit=200)
    assert grown.coordinate == whole.coordinate
    assert len(grown.items) > len(whole.items)

    with pytest.raises(ListCursorStale, match="listing changed") as caught:
        _curation(instance, limit=1, cursor=cursor)
    assert caught.value.error_code == "playbill.list.cursor_stale"


def test_a_curation_cursor_refuses_once_accepted_state_moves(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    cursor = _curation_cursor(_curation(instance, limit=200))

    activate(instance, owner, _propose(instance, "moves-state", "wi-60"))
    with pytest.raises(ListCursorStale, match="accepted state moved"):
        _curation(instance, limit=1, cursor=cursor)
