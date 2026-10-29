"""A Procedure proposes Claim retirements as items, reviewed like any proposal.

A retirement item names a live Claim by its ID, the retirement ``reason`` and
the author's ``rationale``. Core does not decide what a disappearance means:
the Procedure proposes and review judges it, or a settle grant whose scope
lists ``retire`` for the ClaimType settles it; any other settle grant proposes
instead (per-rule trust). The receipt binds the item to the retired member. A
key Claim retires with its Subject, never as an item; a terminal that retires
a lineage and also revises it, or takes a position on it, refuses. One terminal
may add a value to a many-valued slot and retire its sibling there.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.procedures.proposal_items import ProcedureClaimRetirementItem
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from cruxible_core.service.procedures.procedure_runs import service_get_playbill_procedure_run
from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress
from cruxible_core.service.proposals.review import service_review_playbill_proposal
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.test_procedures import test_procedure_proposal_delivery as delivery
from tests.test_procedures import test_procedure_settlement_delivery as settlement
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_proposal_fan_out import FAN_OUT, _element, _write_feed

LATER = fixtures.NOW + timedelta(minutes=5)
GONE = "The record is no longer in the vendor export."


def _retire(claim_id: str, **overrides: Any) -> dict[str, object]:
    item: dict[str, object] = {
        "tag": "playbill-procedure-claim-retirement-item-v1",
        "retires": claim_id,
        "reason": "was-wrong",
        "rationale": GONE,
    }
    item.update(overrides)
    return item


def _claim_id(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".json")


def _head_claim(instance: Any, claim_id: str) -> Any:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))


def _proposed_world(tmp_path: Path, *, claim_type: Any = None):  # type: ignore[no-untyped-def]
    """A fan-out proposal world whose first run's Claim is accepted; returns its ID too."""

    instance, owner, root, line, _procedure = delivery.proposal_world(
        tmp_path,
        templates=FAN_OUT,  # type: ignore[arg-type]
        claim_type=claim_type,
    )
    _write_feed(root, [_element(None)])
    first = delivery.run_line(instance, root, line)
    assert first.status == "succeeded", first.terminal
    (egress,) = first.terminal_egress
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    (child,) = egress.children
    return instance, owner, root, line, _claim_id(child.path)


def _refusal(state: Any) -> Any:
    return delivery._refusal(state)


def test_the_item_names_a_claim_by_id_with_a_reason_and_a_rationale() -> None:
    item = ProcedureClaimRetirementItem.model_validate(_retire("CLM-" + "a" * 32))
    assert (item.retires, item.reason, item.rationale) == ("CLM-" + "a" * 32, "was-wrong", GONE)
    with pytest.raises(ValueError):
        ProcedureClaimRetirementItem.model_validate(_retire("CLM-" + "a" * 32, rationale=" "))
    with pytest.raises(ValueError):
        ProcedureClaimRetirementItem.model_validate(_retire("not-a-claim"))


_NEEDS_B3_EXTRACT = pytest.mark.skip(
    reason="needs B3 extract: _proposed_world/_settled_world read FAN_OUT's "
    "$steps.<alias>.content.json.proposals, and workspace.file's content contract "
    "is text/bytes only pre-B3"
)


@_NEEDS_B3_EXTRACT
def test_a_retirement_item_proposes_and_review_retires_the_claim(tmp_path: Path) -> None:
    instance, owner, root, line, claim_id = _proposed_world(tmp_path)
    _write_feed(root, [_retire(claim_id)])

    state = delivery.run_line(instance, root, line, at=LATER)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered" and egress.proposal_id is not None
    (child,) = egress.children
    # The receipt binds the item to the retired member.
    assert child.path == claim_path(claim_id) and child.path in egress.target_paths
    assert _head_claim(instance, claim_id).lifecycle.state == "live"
    review = service_review_playbill_proposal(
        instance, proposal_id=egress.proposal_id, access=delivery._READER
    )
    assert [(item.path, item.reason) for item in review.item_reasons] == [(child.path, GONE)]
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    retired = _head_claim(instance, claim_id)
    assert retired.lifecycle.state == "retired"
    assert retired.retirement is not None and retired.retirement.reason == "was-wrong"


@_NEEDS_B3_EXTRACT
def test_a_mixed_terminal_retires_one_claim_and_proposes_another(tmp_path: Path) -> None:
    instance, owner, root, line, claim_id = _proposed_world(tmp_path)
    _write_feed(root, [_retire(claim_id), _element("vendor")])

    state = delivery.run_line(instance, root, line, at=LATER)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    retired_child, created_child = egress.children
    assert retired_child.path == claim_path(claim_id)
    assert created_child.path != retired_child.path
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    assert _head_claim(instance, claim_id).lifecycle.state == "retired"
    created = _head_claim(instance, _claim_id(created_child.path))
    assert created.lifecycle.state == "live" and created.statement.qualifier == "vendor"


@_NEEDS_B3_EXTRACT
def test_retiring_and_revising_one_lineage_in_one_terminal_refuses(tmp_path: Path) -> None:
    instance, _owner, root, line, claim_id = _proposed_world(tmp_path)
    revision = _element(None, revises=claim_id)
    revision["statement"] = {  # type: ignore[index]
        **revision["statement"],  # type: ignore[dict-item]
        "object": {"kind": "literal", "value": "critical"},
    }
    _write_feed(root, [_retire(claim_id), revision])

    refusal = _refusal(delivery.run_line(instance, root, line, at=LATER))

    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details["code"] == "cruxible.procedure.retirement_lineage_conflict"
    assert refusal.details["claim_id"] == claim_id
    assert len(delivery._proposal_refs(instance)) == 1  # only the first run's


def _valued(value: str) -> dict[str, object]:
    """An item for the first run's slot (no qualifier) holding another value."""

    item = _element(None)
    item["statement"] = {  # type: ignore[index]
        **item["statement"],  # type: ignore[dict-item]
        "object": {"kind": "literal", "value": value},
    }
    return item


@_NEEDS_B3_EXTRACT
def test_one_terminal_adds_a_value_and_retires_its_sibling_in_a_many_valued_slot(
    tmp_path: Path,
) -> None:
    # Retirements stage after the Claims they sit beside, so the slot law still
    # demands a disposition of the retiring sibling: it is dispositioned
    # not_tested, as the write verbs do, rather than split across two runs.
    instance, owner, root, line, claim_id = _proposed_world(
        tmp_path, claim_type=delivery._many_valued_claim_type()
    )
    _write_feed(root, [_retire(claim_id), _valued("critical")])

    state = delivery.run_line(instance, root, line, at=LATER)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered" and egress.proposal_id is not None
    retired_child, created_child = egress.children
    assert retired_child.path == claim_path(claim_id)
    created_id = _claim_id(created_child.path)
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    assert _head_claim(instance, claim_id).lifecycle.state == "retired"
    live = _head_claim(instance, created_id)
    assert live.lifecycle.state == "live"
    assert live.statement.object.model_dump(mode="json")["value"] == "critical"
    assert [claim.identity.name for claim in delivery._live_slot_claims(instance)] == [created_id]


@pytest.mark.parametrize("disposition", ["support", "contradict", "unsure", "not_tested"])
def test_a_retiring_claim_admits_only_a_not_tested_disposition(disposition: str) -> None:
    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        AuthoringExistingClaimDisposition,
        ClaimAuthoringPayload,
        ClaimDependencyDrafts,
        ExistingCaptureCitationSource,
        authoring_member_identity,
    )
    from cruxible_core.procedures.proposal_delivery import (
        ProposalDeliveryRefused,
        _refuse_retired_lineage_conflicts,
    )

    retiring = "CLM-" + "a" * 32
    member = ClaimAuthoringPayload(
        statement=AuthoringClaimStatement.model_validate(_valued("critical")["statement"]),
        rationale="Another value beside the retiring one.",
        source=ExistingCaptureCitationSource(capture_digest="sha256:" + "c" * 64),
        citation_role="evidence",
        existing_claim_dispositions=(
            AuthoringExistingClaimDisposition(claim_id=retiring, disposition=disposition),  # type: ignore[arg-type]
        ),
        dependency_drafts=ClaimDependencyDrafts(),
    )

    def check() -> None:
        _refuse_retired_lineage_conflicts(
            {"retire-item": retiring},
            claim_ids={"add-item": "CLM-" + "b" * 32},
            members=(member,),
            identity_to_item={authoring_member_identity(member): "add-item"},
        )

    if disposition == "not_tested":
        check()  # no position taken: the slot law's demand, admitted
        return
    with pytest.raises(ProposalDeliveryRefused) as refused:
        check()
    assert refused.value.details["code"] == "cruxible.procedure.retirement_lineage_conflict"
    assert (refused.value.details["item_key"], refused.value.details["retired_by"]) == (
        "add-item",
        "retire-item",
    )


@_NEEDS_B3_EXTRACT
def test_a_retirement_of_no_live_claim_refuses(tmp_path: Path) -> None:
    instance, _owner, root, line, claim_id = _proposed_world(tmp_path)
    _write_feed(root, [_retire("CLM-" + "0" * 32)])

    refusal = _refusal(delivery.run_line(instance, root, line, at=LATER))

    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details["code"] == "cruxible.procedure.retirement_target_not_live"
    assert _head_claim(instance, claim_id).lifecycle.state == "live"


@_NEEDS_B3_EXTRACT
def test_a_key_claim_retires_with_its_subject_never_as_an_item(tmp_path: Path) -> None:
    from tests.test_procedures.test_procedure_subject_items import (
        KIND,
        SLUG,
        subject_item,
        subject_world,
    )

    instance, owner, root, line = subject_world(tmp_path, FAN_OUT)  # type: ignore[arg-type]
    _write_feed(root, [subject_item()])
    first = delivery.run_line(instance, root, line)
    assert first.status == "succeeded", first.terminal
    (egress,) = first.terminal_egress
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    (key_claim,) = [
        parse_claim(tree[path], path=path)
        for path in tree
        if path.startswith("claims/")
        and parse_claim(tree[path], path=path).statement.predicate == SLUG
    ]
    assert key_claim.statement.subject.artifact_path == f"subjects/{KIND}/click.json"
    _write_feed(root, [_retire(key_claim.identity.name)])

    refusal = _refusal(delivery.run_line(instance, root, line, at=LATER))

    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details["code"] == "cruxible.procedure.key_claim_retirement"
    assert "Retire the Subject" in refusal.details["repair"]


@pytest.mark.parametrize("after_submit", [True, False], ids=["after-proposal", "before-proposal"])
@_NEEDS_B3_EXTRACT
def test_a_crashed_retirement_terminal_recovers_one_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_submit: bool
) -> None:
    instance, owner, root, line, claim_id = _proposed_world(tmp_path)
    _write_feed(root, [_retire(claim_id)])
    delivery._crash_after(monkeypatch, after_submit=after_submit)
    with pytest.raises(delivery._Crash):
        delivery.run_line(instance, root, line, at=LATER)
    monkeypatch.undo()

    recovered = service_recover_proposal_egress(instance, recorded_at=LATER + timedelta(minutes=1))

    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered"
    state = service_get_playbill_procedure_run(instance, run_id=run_id)
    (egress,) = state.terminal_egress
    assert egress.proposal_id is not None
    assert [child.path for child in egress.children] == [claim_path(claim_id)]
    assert len(delivery._proposal_refs(instance)) == 2  # the first run's and this one
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    assert _head_claim(instance, claim_id).lifecycle.state == "retired"


# --- settle -------------------------------------------------------------------


def _settled_world(tmp_path: Path, *, change_kinds: tuple[str, ...]):  # type: ignore[no-untyped-def]
    instance, root, line = settlement.settle_world(
        tmp_path, candidate_templates=FAN_OUT, change_kinds=change_kinds
    )
    _write_feed(root, [_element(None)])
    first = settlement.run_settle(instance, root, line)
    assert first.status == "succeeded", first.terminal
    egress = settlement._egress(first)
    assert egress.settle_outcome == "settled", egress
    (child,) = egress.children
    return instance, root, line, _claim_id(child.path)


@_NEEDS_B3_EXTRACT
def test_a_settle_grant_listing_retire_settles_the_retirement(tmp_path: Path) -> None:
    instance, root, line, claim_id = _settled_world(
        tmp_path, change_kinds=("create", "revise", "retire")
    )
    _write_feed(root, [_retire(claim_id)])

    state = settlement.run_settle(instance, root, line, now=LATER)

    assert state.status == "succeeded", state.terminal
    egress = settlement._egress(state)
    assert egress.settle_outcome == "settled", egress
    assert egress.accepted_git_oid == instance.accepted_coordinate().git_oid
    identity = instance.accepting_admission(egress.accepted_git_oid)
    assert identity is not None and identity.proposal_id == egress.proposal_id
    from cruxible_core.service.discovery.change_reasons import ChangeReasons

    with instance.accepted_history_reader() as history:
        reason = ChangeReasons(instance).reason(
            history.generation(history.sequence), claim_path(claim_id)
        )
    assert reason is not None and reason.proposal == identity.proposal_id
    assert [child.path for child in egress.children] == [claim_path(claim_id)]
    assert _head_claim(instance, claim_id).lifecycle.state == "retired"


@_NEEDS_B3_EXTRACT
def test_a_settle_grant_without_retire_proposes_the_retirement_instead(tmp_path: Path) -> None:
    instance, root, line, claim_id = _settled_world(tmp_path, change_kinds=("create", "revise"))
    base = instance.accepted_coordinate()
    _write_feed(root, [_retire(claim_id)])

    state = settlement.run_settle(instance, root, line, now=LATER)

    assert state.status == "succeeded", state.terminal
    egress = settlement._egress(state)
    assert (egress.settle_outcome, egress.fallback_reason) == (
        "proposed",
        "cruxible.settle.outside_mandate_scope",
    )
    assert egress.outside_scope_claim_types == (f"ClaimType:{delivery.PREDICATE}",)
    assert egress.proposal_id is not None and instance.accepted_coordinate() == base
    assert _head_claim(instance, claim_id).lifecycle.state == "live"
