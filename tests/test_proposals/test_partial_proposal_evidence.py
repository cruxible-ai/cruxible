"""Retained partial evidence stays visible without suppressing healthy proposals."""

from __future__ import annotations

import pytest

from cruxible_client.contracts import PlaybillProposalList
from cruxible_client.contracts.proposal_models import ProposalWithdrawalRecord
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from tests.core_support._support import initialize_local
from tests.test_proposals.test_grouped_proposal_notes import _submit


def test_withdrawal_without_other_records_has_an_indexed_source_locator(tmp_path):
    instance, _ = initialize_local(tmp_path)
    evidence = instance.proposal_evidence()
    withdrawal = ProposalWithdrawalRecord(
        proposal_id="sha256:" + "7" * 64,
        actor_id="owner",
        reason="retained partial evidence",
        withdrawn_at="2026-08-11T12:31:00.000000Z",
    )
    evidence.write_withdrawal(withdrawal)
    entry = service_list_playbill_proposals(instance).entries[0]
    assert entry.incomplete_reasons == ("missing_admission", "missing_evaluation")
    assert entry.withdrawal_present
    assert evidence.read_withdrawal(withdrawal.proposal_id) == withdrawal
    (evidence.root / ".proposal-source.json").unlink()
    assert service_list_playbill_proposals(instance).entries == (entry,)


def test_orphan_withdrawal_survives_rebuild_restart_and_later_admission(tmp_path):
    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance, "withdrawn")
    evidence = instance.proposal_evidence()
    withdrawal = ProposalWithdrawalRecord(
        proposal_id=proposal.admission.proposal_id,
        actor_id="owner",
        reason="retain this withdrawal",
        withdrawn_at="2026-08-11T12:31:00.000000Z",
    )
    evidence.write_withdrawal(withdrawal)
    evidence.index.locate(evidence, withdrawal.proposal_id)
    (evidence.proposals / f"{withdrawal.proposal_id.removeprefix('sha256:')}.json").unlink()

    entry = service_list_playbill_proposals(instance).entries[0]
    assert entry.status == "incomplete"
    assert entry.incomplete_reasons == ("missing_admission",)
    assert entry.withdrawal_present
    assert entry.actor_id is entry.target_ref is entry.admitted_at is None
    assert evidence.read_withdrawal(withdrawal.proposal_id) == withdrawal

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert service_list_playbill_proposals(reopened).entries == (entry,)
    evidence = reopened.proposal_evidence()
    evidence.write_admission(proposal.admission)
    restored = service_list_playbill_proposals(reopened).entries[0]
    assert restored.status == "settled"
    assert restored.terminal_reason == "withdrawn"
    assert evidence.read_withdrawal(withdrawal.proposal_id) == withdrawal


@pytest.mark.parametrize("missing", ("evaluation", "candidate"))
def test_incomplete_entry_does_not_hide_healthy_rows_or_invent_verdict(tmp_path, missing):
    instance, _ = initialize_local(tmp_path)
    healthy = _submit(instance, "healthy")
    partial = _submit(instance, "partial", timestamp="2026-08-11T12:30:00.000001Z")
    evidence = instance.proposal_evidence()
    row = evidence.index.locate(evidence, partial.admission.proposal_id)
    path = (
        evidence.root / row["evaluation_path"]
        if missing == "evaluation"
        else evidence.candidates
        / f"{partial.candidate.candidate_digest.removeprefix('sha256:')}.json"
    )
    path.unlink()

    result = service_list_playbill_proposals(instance)
    by_id = {entry.proposal_id: entry for entry in result.entries}
    assert by_id[healthy.admission.proposal_id].status == "open"
    entry = by_id[partial.admission.proposal_id]
    assert entry.status == "incomplete"
    assert entry.incomplete_reasons == (f"missing_{missing}",)
    assert entry.verdict == (None if missing == "evaluation" else "candidate")
    assert entry.terminal_reason is None
    assert service_list_playbill_proposals(instance, status="open").entries == (
        by_id[healthy.admission.proposal_id],
    )
    assert service_list_playbill_proposals(instance, status="settled").entries == ()
    assert service_list_playbill_proposals(instance, status="incomplete").entries == (entry,)
    assert PlaybillProposalList.model_validate(result.model_dump(mode="json")).entries


@pytest.mark.parametrize("missing", ("admission", "evaluation", "candidate"))
def test_one_proposal_status_reports_missing_evidence_as_incomplete(tmp_path, missing):
    """The by-ID status read keeps the list's incomplete-entry answer, not an error."""
    from cruxible_client.contracts.errors import ProposalNotFoundError
    from cruxible_core.service.proposals.proposals import service_playbill_proposal_status

    instance, _ = initialize_local(tmp_path)
    partial = _submit(instance, "partial")
    proposal_id = partial.admission.proposal_id
    evidence = instance.proposal_evidence()
    if missing == "admission":
        # A retained withdrawal keeps the row listed once its admission is gone.
        evidence.write_withdrawal(
            ProposalWithdrawalRecord(
                proposal_id=proposal_id,
                actor_id="owner",
                reason="retain this row",
                withdrawn_at="2026-08-11T12:31:00.000000Z",
            )
        )
        evidence.index.locate(evidence, proposal_id)
        path = evidence.proposals / f"{proposal_id.removeprefix('sha256:')}.json"
    elif missing == "evaluation":
        path = evidence.root / evidence.index.locate(evidence, proposal_id)["evaluation_path"]
    else:
        path = (
            evidence.candidates
            / f"{partial.candidate.candidate_digest.removeprefix('sha256:')}.json"
        )
    path.unlink()

    (listed,) = [
        entry
        for entry in service_list_playbill_proposals(instance).entries
        if entry.proposal_id == proposal_id
    ]
    status = service_playbill_proposal_status(instance, proposal_id=proposal_id)

    assert status == listed
    assert status.status == "incomplete"
    assert status.incomplete_reasons == (f"missing_{missing}",)
    with pytest.raises(ProposalNotFoundError):
        service_playbill_proposal_status(instance, proposal_id="sha256:" + "0" * 64)
