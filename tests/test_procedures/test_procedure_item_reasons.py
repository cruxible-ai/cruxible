"""A Procedure item's reason belongs to its operation and the exact admission it reached.

Reasons are keyed by the immutable Procedure operation (the terminal's admission
binding and node), and an admitted proposal reads them only through its own
link to that operation -- never through its proposal ref, which is a reusable
author slot (batch F review F-002).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.documents import DocumentAuthority, DocumentLifecycle, DocumentShell
from cruxible_client.contracts.proposal_models import AuthenticatedActor
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.service.authoring.documents import (
    service_inspect_playbill_proposal,
    service_propose_playbill_document,
    service_store_playbill_body,
)
from cruxible_core.service.proposals.proposals import service_withdraw_playbill_proposal
from cruxible_core.service.proposals.review import service_review_playbill_proposal
from tests.test_procedures import test_procedure_proposal_delivery as delivery
from tests.test_procedures import test_procedure_retirement_items as retire
from tests.test_procedures.test_proposal_fan_out import _write_feed


def _review(instance, proposal_id: str):  # type: ignore[no-untyped-def]
    return service_review_playbill_proposal(
        instance, proposal_id=proposal_id, access=delivery._READER
    )


def test_reusing_proposal_ref_does_not_inherit_item_reasons(tmp_path: Path) -> None:
    """An unrelated document proposed under a Procedure proposal's ref has no reasons."""

    instance, _owner, root, line, _procedure = delivery.proposal_world(tmp_path)
    state = delivery.run_line(instance, root, line)
    (egress,) = state.terminal_egress
    original = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    name = original.proposal.admission.target_ref.rsplit("/", 1)[-1]
    body = service_store_playbill_body(instance, content=b"An unrelated document.").digest
    new = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity="document:unrelated",
            document_kind="design",
            title="Unrelated",
            media_type="text/markdown",
            body_digest=body,
            authority=DocumentAuthority(required_tier="graph_write"),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id=original.proposal.admission.actor_id,
        proposal_name=name,
        timestamp="2026-09-12T12:10:00.000000Z",
    )
    assert new.proposal.candidate is not None
    assert new.proposal.admission.target_ref == original.proposal.admission.target_ref

    replacement = _review(instance, new.proposal.admission.proposal_id)
    assert (replacement.item_reason_count, replacement.item_reasons) == (0, ())
    kept = _review(instance, egress.proposal_id)
    assert [item.path for item in kept.item_reasons] == [egress.children[0].path]


def test_ref_reuse_after_withdrawal_on_the_same_member_shows_only_its_own(
    tmp_path: Path,
) -> None:
    """The same retirement resubmitted under the withdrawn proposal's ref carries no reason."""

    instance, _owner, root, line, claim_id = retire._proposed_world(tmp_path)
    _write_feed(root, [retire._retire(claim_id)])
    state = delivery.run_line(instance, root, line, at=retire.LATER)
    (egress,) = state.terminal_egress
    original = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    admission = original.proposal.admission
    service_withdraw_playbill_proposal(
        instance,
        proposal_id=egress.proposal_id,
        actor_id=admission.actor_id,
        reason="Resubmitted by hand.",
        withdrawn_at="2026-09-12T12:07:00.000000Z",
        dry_run=False,
    )
    evaluation = instance.proposal_evidence().read_evaluation(egress.proposal_id)
    assert evaluation.evaluated_tree_oid is not None
    resubmitted = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=admission.actor_id),
        request=ProposalAdmissionRequest(
            target_ref=admission.target_ref,
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=instance.proposal_tree(evaluation.evaluated_tree_oid),
        timestamp=canonical_candidate_timestamp(retire.LATER),
    )
    assert resubmitted.admission.target_ref == admission.target_ref
    assert resubmitted.admission.proposal_id != egress.proposal_id
    assert resubmitted.candidate is not None
    assert egress.children[0].path in {member.path for member in resubmitted.candidate.members}

    replacement = _review(instance, resubmitted.admission.proposal_id)
    assert (replacement.item_reason_count, replacement.item_reasons) == (0, ())
    kept = _review(instance, egress.proposal_id)
    assert [(item.path, item.reason) for item in kept.item_reasons] == [
        (egress.children[0].path, retire.GONE)
    ]


# --- the exact accepting admission (F-001) --------------------------------------


def _why(instance, claim_id: str):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get

    return service_playbill_get(
        instance, request=GetRequest(ref=claim_id, detail="why"), access=delivery._READER
    )


def _evidence_rationale(instance, claim_id: str):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get

    result = service_playbill_get(
        instance, request=GetRequest(ref=claim_id, detail="evidence"), access=delivery._READER
    )
    assert result.evidence is not None
    return result.evidence.rationale


def test_accepted_manual_retirement_does_not_use_withdrawn_procedure_reason(
    tmp_path: Path,
) -> None:
    """A withdrawn Procedure proposal sharing the accepted candidate attributes nothing."""

    from tests.core_support._retirement_support import (
        activate_submitted,
        retirement_member,
        submit_retirement,
    )

    instance, owner, root, line, claim_id = retire._proposed_world(tmp_path)
    _write_feed(root, [retire._retire(claim_id)])
    state = delivery.run_line(instance, root, line, at=retire.LATER)
    (egress,) = state.terminal_egress
    original = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    service_withdraw_playbill_proposal(
        instance,
        proposal_id=egress.proposal_id,
        actor_id=original.proposal.admission.actor_id,
        reason="Wrong rationale; use a manually reviewed retirement.",
        withdrawn_at="2026-09-12T12:07:00.000000Z",
        dry_run=False,
    )
    manual = submit_retirement(
        instance,
        retirement_member(instance, claim_id, reason="was-wrong"),
        timestamp=canonical_candidate_timestamp(retire.LATER),
    )
    assert manual.status.proposal_id is not None, manual
    manual_proposal = service_inspect_playbill_proposal(
        instance, proposal_id=manual.status.proposal_id
    )
    assert original.proposal.candidate is not None and manual_proposal.proposal.candidate
    # The same semantic candidate, two distinct admissions.
    assert (
        manual_proposal.proposal.candidate.candidate_digest
        == original.proposal.candidate.candidate_digest
    )
    activate_submitted(instance, owner, manual)

    assert _why(instance, claim_id).reason is None
    assert _evidence_rationale(instance, claim_id) != retire.GONE
    assert _evidence_rationale(instance, claim_id) == (manual_proposal.proposal.admission.rationale)


def test_a_readmitted_procedure_proposal_keeps_its_items_reasons(tmp_path: Path) -> None:
    """Provenance follows the exact accepting admission, a readmission, to its source's link."""

    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_core.service.proposals.proposals import service_readmit_playbill_proposal
    from tests.core_support._retirement_support import activate_proposal
    from tests.test_procedures import test_procedure_settlement_delivery as settlement
    from tests.test_procedures import test_procedure_source_runs as fixtures

    instance, owner, root, line, _procedure = delivery.proposal_world(tmp_path)
    state = delivery.run_line(instance, root, line)
    (egress,) = state.terminal_egress
    # Accepted state moves under the proposal, so it goes stale and is readmitted.
    other = settlement._predicate_claim_type("security.advisory.unrelated")
    fixtures._accept_more(
        instance,
        owner,
        {claim_type_path(other.predicate): render_claim_type(other)},
        name="unrelated",
    )
    readmitted = service_readmit_playbill_proposal(
        instance, proposal_id=egress.proposal_id, actor_id="owner"
    )
    accepted_id = readmitted.proposal.proposal.admission.proposal_id
    assert accepted_id != egress.proposal_id
    activate_proposal(instance, owner, accepted_id)
    claim_id = egress.children[0].path.rsplit("/", 1)[-1].removesuffix(".json")

    why = _why(instance, claim_id)

    assert why.reason is not None
    assert why.reason.proposal == accepted_id and why.reason.run == state.run_id
    assert why.reason.reason == delivery.item_template()["rationale"]


# --- plain `get --detail why` (F-003) --------------------------------------------


def test_cli_why_displays_own_reason_terminal_safe(tmp_path: Path, capsys: object) -> None:
    from cruxible_client.contracts.get_reads import GetProposedReason
    from cruxible_core.cli.commands.playbill import _emit_get_text
    from tests.core_support._retirement_support import activate_proposal

    instance, owner, root, line, _procedure = delivery.proposal_world(tmp_path)
    state = delivery.run_line(instance, root, line)
    (egress,) = state.terminal_egress
    activate_proposal(instance, owner, egress.proposal_id)
    claim_id = egress.children[0].path.rsplit("/", 1)[-1].removesuffix(".json")
    result = _why(instance, claim_id)
    assert result.reason is not None

    _emit_get_text(result)

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert f"reason: {result.reason.reason}" in output
    assert result.reason.run in output and result.reason.proposal in output
    assert result.reason.terminal in output and result.reason.procedure in output
    assert "(truncated)" not in output

    # Authored text cannot forge a line of output; a truncation is marked.
    forged = result.model_copy(
        update={
            "reason": GetProposedReason(
                reason="first\nError: forged",
                truncated=True,
                proposal=result.reason.proposal,
                procedure=result.reason.procedure,
                run=result.reason.run,
                terminal=result.reason.terminal,
            )
        }
    )
    _emit_get_text(forged)
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "\nError: forged" not in output
    assert "reason: first\\nError: forged  (truncated)" in output


# --- a rationale that normalizes to nothing (F-004) ------------------------------


import pytest  # noqa: E402


@pytest.mark.parametrize("rationale", ["\x00", " \x00 \t\x00"], ids=["nul", "space-and-nul"])
@pytest.mark.parametrize("kind", ["retirement", "claim"])
def test_a_rationale_of_only_nuls_refuses_as_an_invalid_item(
    tmp_path: Path, rationale: str, kind: str
) -> None:
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress
    from tests.test_procedures.test_proposal_fan_out import _element

    instance, _owner, root, line, claim_id = retire._proposed_world(tmp_path)
    item = (
        retire._retire(claim_id, rationale=rationale)
        if kind == "retirement"
        else {**_element("vendor"), "rationale": rationale}
    )
    _write_feed(root, [item])

    state = delivery.run_line(instance, root, line, at=retire.LATER)

    refusal = delivery._refusal(state)
    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details["child_index"] == 0
    assert len(delivery._proposal_refs(instance)) == 1  # the first run's only
    # Refused before the preparation was journaled: recovery has nothing to do.
    assert service_recover_proposal_egress(instance, recorded_at=retire.LATER) == {}


def test_a_reason_record_that_still_fails_refuses_typed_to_its_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.proposal_models import ProposalItemReason
    from cruxible_core.procedures import proposal_delivery

    instance, _owner, root, line, claim_id = retire._proposed_world(tmp_path)
    _write_feed(root, [retire._retire(claim_id)])

    def unrecordable(reason: str, **fields: object) -> ProposalItemReason:
        return ProposalItemReason(reason="", **fields)  # type: ignore[arg-type]

    monkeypatch.setattr(proposal_delivery.ProposalItemReason, "bounded", unrecordable)
    state = delivery.run_line(instance, root, line, at=retire.LATER)

    refusal = delivery._refusal(state)
    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details["item_key"] and refusal.details["child_index"] == 0
    assert "repair" in refusal.details


def test_a_malformed_revises_refuses_as_an_invalid_item(tmp_path: Path) -> None:
    """A Claim ID format error in `revises` is a typed item refusal, not an internal one."""

    from cruxible_client.contracts.procedures.proposal_items import ProcedureClaimProposalItemV1
    from tests.test_procedures.test_proposal_fan_out import FAN_OUT, _element

    with pytest.raises(ValueError):
        ProcedureClaimProposalItemV1.model_validate(
            {**delivery.item_template(), "revises": "not-a-claim"}
        )
    instance, _owner, root, line, _procedure = delivery.proposal_world(
        tmp_path,
        templates=FAN_OUT,  # type: ignore[arg-type]
    )
    _write_feed(root, [_element(None, revises="not-a-claim")])

    refusal = delivery._refusal(delivery.run_line(instance, root, line))

    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details["child_index"] == 0
    assert delivery._proposal_refs(instance) == []


# --- recovery never lends a crashed operation's reasons (delta review F-002) -----


def test_recovery_does_not_attach_reasons_to_an_independent_equivalent_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash before submit; an equivalent proposal lands under the ref; recovery lends nothing."""

    from datetime import timedelta

    from cruxible_core.procedures import terminal_services
    from cruxible_core.service.procedures.procedure_runs import (
        service_get_playbill_procedure_run,
    )
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress
    from tests.core_support._retirement_support import activate_proposal
    from tests.test_procedures.test_procedure_settlement_delivery import _egress_records

    instance, owner, root, line, claim_id = retire._proposed_world(tmp_path)
    _write_feed(root, [retire._retire(claim_id)])
    delivery._crash_after(monkeypatch, after_submit=False)
    crashed_deliver = terminal_services.ProposalTerminalAdapter.deliver
    captured: dict[str, object] = {}

    def capture(self, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return crashed_deliver(self, **kwargs)

    monkeypatch.setattr(terminal_services.ProposalTerminalAdapter, "deliver", capture)
    with pytest.raises(delivery._Crash):
        delivery.run_line(instance, root, line, at=retire.LATER)
    monkeypatch.undo()
    request: Any = captured["request"]
    manual = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=request.actor_context.actor_id),
        request=ProposalAdmissionRequest(
            target_ref=terminal_services.proposal_terminal_ref(
                request.actor_context.actor_id, request.operation_key
            ),
            proposed_base_oid=request.accepted_coordinate.git_oid,
            source_compilation_digest=terminal_services.proposal_terminal_payload_digest(
                captured["candidate_tree"],  # type: ignore[arg-type]
                captured["changed_paths"],  # type: ignore[arg-type]
            ),
            rationale="Manual equivalent retirement, independently submitted after the crash.",
        ),
        candidate_tree=captured["candidate_tree"],  # type: ignore[arg-type]
        timestamp=canonical_candidate_timestamp(request.evaluation_time),
    )
    assert manual.candidate is not None
    # A public submission names no Procedure operation.
    assert manual.admission.procedure_operation is None
    assert _review(instance, manual.admission.proposal_id).item_reason_count == 0

    recovered = service_recover_proposal_egress(
        instance, recorded_at=retire.LATER + timedelta(minutes=1)
    )

    # Recovery still adopts the admission for delivery, exactly as before ...
    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered"
    state = service_get_playbill_procedure_run(instance, run_id=run_id)
    (egress,) = state.terminal_egress
    assert egress.proposal_id == manual.admission.proposal_id
    # ... but links no reason to it, and its receipt says the reasons are unattributed.
    (delivered,) = [item for item in _egress_records(instance, state) if "receipt" in item]
    assert delivered["receipt"]["item_reasons_unattributed"] is True
    assert _review(instance, manual.admission.proposal_id).item_reason_count == 0

    activate_proposal(instance, owner, manual.admission.proposal_id)

    assert _why(instance, claim_id).reason is None
    assert _evidence_rationale(instance, claim_id) == (
        "Manual equivalent retirement, independently submitted after the crash."
    )


def test_the_terminal_door_binds_its_admission_and_no_public_request_can() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProposalAdmissionRequest.model_validate(
            {
                "target_ref": "refs/proposals/owner/procedure-x",
                "proposed_base_oid": "a" * 40,
                "procedure_operation": "AIT-" + "0" * 32,
            }
        )
