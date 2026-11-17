"""Stale proposals by change-set overlap (readmit-by-change-set-overlap-1024).

A proposal is a change set against a base. It is stale only when what its change
set touches or is judged under changed in (base, head]; otherwise it is open,
and approval lands the same change set at the head. Nothing is recorded on the
proposal for the check: each key is one seek on the history index.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.errors import (
    ApprovalRequirementUnmet,
    ProposalAlreadyAccepted,
    ProposalStale,
    ProposalWithdrawnError,
)
from cruxible_client.contracts.recompilation import ProposalRecompiled
from cruxible_client.contracts.subjects import (
    parse_subject,
    render_subject,
    subject_digest,
    subject_path,
)
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.proposals.proposals import (
    _proposal_result,
    service_list_playbill_proposals,
    service_playbill_proposal_status,
    stale_unreadmitted_proposals,
)
from cruxible_core.service.proposals.review import service_prepare_playbill_approval
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._support import client_material
from tests.core_support._write_support import KIND, OWNER, _claim_type, seed_write_surface
from tests.test_ledger.test_activation import _sign
from tests.test_service.test_write_verbs import _set, _write

TITLE = claim_type_path(f"{KIND}.title")


def _world(tmp_path: Path) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    instance, _owner = seed_write_surface(tmp_path, self_approval=False)
    return instance, client_material(tmp_path, instance)


def _approve(instance: PlaybillInstance, reviewer: GeneratedKeyMaterial, proposal_id: str):  # type: ignore[no-untyped-def]
    proposal = _proposal_result(instance, proposal_id)
    assert proposal.candidate is not None
    return service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=_sign(
            reviewer,
            proposal.candidate.candidate_digest,
            proposal.candidate.candidate.parent_semantic_root,
        ).attestation,
        authenticated_submitter="reviewer",
        activate_when_complete=True,
        may_activate=True,
    )


def _pending(instance: PlaybillInstance, change: dict[str, object], **options: object) -> str:
    outcome = _write(instance, change, **options)
    assert outcome.proposal is not None and outcome.status != "accepted", outcome
    return outcome.proposal.proposal_id


def _accept_write(
    instance: PlaybillInstance, reviewer: GeneratedKeyMaterial, change: dict[str, object]
) -> None:
    receipt = _approve(instance, reviewer, _pending(instance, change))
    assert receipt.activation is not None and receipt.activation.status == "accepted"


def _accept_members(
    instance: PlaybillInstance,
    reviewer: GeneratedKeyMaterial,
    members: Mapping[str, bytes],
    name: str,
) -> None:
    base = instance.accepted_coordinate()
    proposed = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**instance.tree_at(base.git_oid), **members},
        timestamp="2026-09-29T12:30:00.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = _approve(instance, reviewer, proposed.admission.proposal_id)
    assert receipt.activation is not None and receipt.activation.status == "accepted"


def _revised(instance: PlaybillInstance, path: str) -> dict[str, bytes]:
    """The artifact at ``path`` again, as its own successor: the path changes, nothing else."""

    content = instance.tree_at(instance.accepted_coordinate().git_oid)[path]
    if path.startswith("subjects/"):
        shell = parse_subject(content, path=path)
        successor = shell.model_copy(
            update={"lifecycle": ArtifactLifecycle(predecessor_digest=subject_digest(shell).tagged)}
        )
        return {path: render_subject(successor)}
    claim_type = parse_claim_type(content, path=path)
    successor_type = claim_type.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(claim_type).tagged)
        }
    )
    return {path: render_claim_type(successor_type)}


def _entry(instance: PlaybillInstance, proposal_id: str):  # type: ignore[no-untyped-def]
    return service_playbill_proposal_status(instance, proposal_id=proposal_id)


@pytest.mark.parametrize("move", ["other_subject", "same_subject_other_field", "unused_definition"])
def test_a_pending_proposal_survives_unrelated_writes_and_approval_lands_it(
    tmp_path: Path, move: str
) -> None:
    instance, reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "survives"))
    if move == "other_subject":
        _accept_write(instance, reviewer, _set(f"{KIND}/wi-2", "title", "elsewhere"))
    elif move == "same_subject_other_field":
        _accept_write(instance, reviewer, _set(f"{KIND}/wi-1", "status", "ready"))
    else:
        notes = _claim_type("notes", value_type={"type": "string"})
        _accept_members(
            instance,
            reviewer,
            {claim_type_path(notes.predicate): render_claim_type(notes)},
            "unused-definition",
        )

    head = instance.accepted_coordinate()
    source = _proposal_result(instance, source_id)
    assert source.candidate is not None
    assert source.candidate.candidate.parent_semantic_root != head.semantic_root
    entry = _entry(instance, source_id)
    assert (entry.status, entry.stale_paths) == ("open", ())
    assert source_id in {
        item.proposal_id
        for item in service_list_playbill_proposals(instance, status="open").entries
    }
    assert stale_unreadmitted_proposals(instance, entry_coordinate(instance)) == ()

    # The approval lands the same change set at the head under its author; an
    # approval signs one candidate, so the landed one is the one to sign.
    landed = _approve(instance, reviewer, source_id)
    assert isinstance(landed, ProposalRecompiled)
    assert (landed.status, landed.members_identical, landed.changed_members) == (
        "pending_review",
        True,
        (),
    )
    assert landed.proposal_id is not None
    successor = _proposal_result(instance, landed.proposal_id)
    link = successor.admission.readmits
    # Proposed at its base, landed at the head: the readmission link records both.
    assert link is not None and link.source_proposal_id == source_id
    assert link.coordinate.git_oid == head.git_oid
    assert successor.evaluation.evaluated_base_oid == head.git_oid
    withdrawal = instance.proposal_evidence().read_withdrawal(source_id)
    assert withdrawal is not None and withdrawal.superseded_by == landed.proposal_id
    assert _entry(instance, source_id).status == "settled"
    receipt = _approve(instance, reviewer, landed.proposal_id)
    assert receipt.activation is not None and receipt.activation.status == "accepted"


def entry_coordinate(instance: PlaybillInstance):  # type: ignore[no-untyped-def]
    from cruxible_core.service.authoring.documents import AcceptedCoordinate

    return AcceptedCoordinate.from_internal(instance.accepted_coordinate())


@pytest.mark.parametrize("move", ["slot", "subject", "claim_type"])
def test_a_change_to_what_it_touches_makes_it_stale_and_names_the_path(
    tmp_path: Path, move: str
) -> None:
    instance, reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "overtaken"))
    if move == "slot":
        _accept_write(instance, reviewer, _set(f"{KIND}/wi-1", "title", "rival"))
        (expected,) = [
            path
            for path in instance.tree_at(instance.accepted_coordinate().git_oid)
            if path.startswith("claims/")
            and path not in _proposal_paths(instance, source_id)
            and b'"rival"' in instance.tree_at(instance.accepted_coordinate().git_oid)[path]
        ]
    elif move == "subject":
        expected = subject_path(KIND, "wi-1")
        _accept_members(instance, reviewer, _revised(instance, expected), "revise-subject")
    else:
        expected = TITLE
        _accept_members(instance, reviewer, _revised(instance, expected), "revise-claim-type")

    entry = _entry(instance, source_id)
    assert entry.status == "awaiting_readmit"
    assert expected in entry.stale_paths
    (stale,) = stale_unreadmitted_proposals(instance, entry_coordinate(instance))
    assert stale.proposal_id == source_id and expected in stale.stale_paths
    # No approval settles it as it stands: the refusal names what changed.
    with pytest.raises(ProposalStale) as refused:
        _approve(instance, reviewer, source_id)
    assert refused.value.reason == "changed_since_base"
    assert expected in str(refused.value)
    assert instance.proposal_evidence().read_withdrawal(source_id) is None


def test_two_writes_defining_one_identical_contract_do_not_conflict(tmp_path: Path) -> None:
    """psw F16: two pending writes each cite the same uncatalogued file, so each
    carries the file's CaptureContract, byte for byte the same. When the first
    landed, the second read as stale on that path, left the open list and the
    reviewer's approval row, and waited for a readmit nobody was told about.

    The head holding exactly what a change set proposes for a path is no
    conflict: the second stays open, the reviewer keeps the approval row, and
    approving lands it at the head without the contract it no longer adds.
    """

    from datetime import UTC, datetime

    from cruxible_client.authoring.doc_writes import write_workspace
    from cruxible_client.contracts.write import WriteRequest
    from cruxible_core.coverage.contracts import CoverageAccessProfile
    from cruxible_core.service.authoring.write_verbs import service_playbill_write
    from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
    from cruxible_core.service.proposals.proposals import open_proposal_count
    from tests.core_support._write_support import caller

    instance, reviewer = _world(tmp_path)
    workspace = tmp_path / "cited"
    workspace.mkdir()
    (workspace / "notes.md").write_text("# Notes\n\nfirst anchor\n\nsecond anchor\n", "utf-8")

    def cite(subject: str, anchor: str) -> str:
        request = WriteRequest.model_validate(
            {
                "because": "The writer checked it.",
                "changes": [
                    _set(
                        subject,
                        "title",
                        anchor,
                        evidence={"kind": "file", "file": f"notes.md#{anchor}"},
                    )
                ],
            }
        )
        outcome = write_workspace(
            request,
            workspace=workspace,
            submit=lambda observed: service_playbill_write(
                instance, request=observed, caller=caller()
            ),
        )
        assert outcome.proposal is not None, outcome
        return outcome.proposal.proposal_id

    first = cite(f"{KIND}/wi-1", "first anchor")
    second = cite(f"{KIND}/wi-2", "second anchor")
    shared = _proposal_paths(instance, first) & _proposal_paths(instance, second)
    (contract,) = shared
    assert contract.startswith("capture-contracts/")

    assert _approve(instance, reviewer, first).activation.status == "accepted"  # type: ignore[union-attr]

    entry = _entry(instance, second)
    assert (entry.status, entry.stale_paths) == ("open", ())
    assert open_proposal_count(instance) == 1
    queue = service_playbill_next(
        instance,
        request=NextRequest(
            evaluation_time=datetime.now(UTC),
            access_profile=CoverageAccessProfile(
                profile_id="test", permitted_access_classes=("instance", "public")
            ),
        ),
        caller_principal_id="reviewer",
    )
    (row,) = [item for item in queue.items if item.subject_identity == second]
    assert row.reason == "proposal_awaiting_approval"
    assert row.repair is not None and row.repair.operation == "cruxible.proposal.approve"

    landed = _approve(instance, reviewer, second)
    assert isinstance(landed, ProposalRecompiled) and landed.proposal_id is not None
    # The contract is already live, so the landed change set no longer adds it;
    # that is a different member roll, and the reviewer is shown it.
    assert landed.status == "pending_review" and not landed.members_identical
    assert [(item.path, item.change) for item in landed.changed_members] == [(contract, "removed")]
    assert _approve(instance, reviewer, landed.proposal_id).activation.status == "accepted"  # type: ignore[union-attr]


def test_a_different_artifact_at_a_proposed_path_is_still_a_conflict(tmp_path: Path) -> None:
    """Only the exact artifact the change set proposes is no conflict: a path
    the head holds a different version of names itself, as before."""

    from cruxible_core.service.proposals.staleness import proposal_change_keys

    instance, reviewer = _world(tmp_path)
    held = _pending(instance, _set(f"{KIND}/wi-1", "title", "held"))
    proposal = _proposal_result(instance, held)
    assert proposal.candidate is not None
    keys = proposal_change_keys(
        instance, proposal_id=held, candidate_digest=proposal.candidate.candidate_digest
    )
    assert {path for path, _digest in keys.proposed} == {
        member.path
        for member in proposal.candidate.members
        if member.disposition in ("create", "replace")
    }
    _accept_write(instance, reviewer, _set(f"{KIND}/wi-1", "title", "someone else's"))
    entry = _entry(instance, held)
    assert entry.status == "awaiting_readmit" and entry.stale_paths


def _proposal_paths(instance: PlaybillInstance, proposal_id: str) -> set[str]:
    candidate = _proposal_result(instance, proposal_id).candidate
    assert candidate is not None
    return {member.path for member in candidate.members}


def test_two_writes_of_one_value_the_second_goes_stale_naming_the_first(
    tmp_path: Path,
) -> None:
    """Run 44's shape on writes: once one landed, the other's slot changed under it.

    A Line's or Procedure's import re-decides at the head and closes as answered
    (``test_a_line_proposal_wholly_live_at_the_head_closes_as_answered``).
    """

    instance, reviewer = _world(tmp_path)
    first = _pending(instance, _set(f"{KIND}/wi-3", "title", "one value"))
    second = _pending(
        instance, _set(f"{KIND}/wi-3", "title", "one value"), because="A second import."
    )
    assert second != first
    receipt = _approve(instance, reviewer, first)
    assert receipt.activation is not None and receipt.activation.status == "accepted"

    entry = _entry(instance, second)
    assert entry.status == "awaiting_readmit"
    assert _proposal_paths(instance, first) <= set(entry.stale_paths)


def test_the_overlap_check_does_keyed_lookups_that_do_not_grow_with_the_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One seek per key: unrelated generations and Subjects add no lookups."""

    from cruxible_core.indexes.history.history_index import HistoryReader

    instance, reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "watched"))
    real = HistoryReader.paths_changed_after
    looked_up: list[int] = []

    def counted(self, paths, sequence):  # type: ignore[no-untyped-def]
        wanted = tuple(paths)
        looked_up.append(len(wanted))
        return real(self, wanted, sequence)

    monkeypatch.setattr(HistoryReader, "paths_changed_after", counted)
    measured = []
    written = 0
    for amount in (1, 6):
        while written < amount:
            # Another Subject each time: the instance and its history grow.
            _accept_write(instance, reviewer, _set(f"{KIND}/grown-{written}", "title", "more"))
            written += 1
        looked_up.clear()
        assert _entry(instance, source_id).status == "open"
        measured.append(sum(looked_up))
    assert measured[0] == measured[1] > 0


def test_activating_an_unapproved_moved_proposal_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """gapa-04: activation used to land an unapproved proposal whose base moved at the
    head, withdrawing it; it refuses with the unmet requirement and who may approve."""

    instance, reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "unapproved"))
    _accept_write(instance, reviewer, _set(f"{KIND}/wi-2", "title", "moves the head"))
    head = instance.accepted_coordinate()

    for _ in range(2):  # refused again, exactly the same way
        with pytest.raises(ApprovalRequirementUnmet) as refused:
            service_activate_playbill_proposal(
                instance, proposal_id=source_id, activated_by="owner"
            )
        assert refused.value.error_code == "cruxible.approval.requirement_unsatisfied"
        assert (refused.value.remaining, refused.value.eligible_signers) == (1, ("reviewer",))
        assert refused.value.repair.operation == "cruxible.proposal.approve"
        assert refused.value.repair.arguments == {
            "proposal_id": source_id,
            "signer_id": "reviewer",
        }
        assert "Nothing was changed" in str(refused.value)

    assert instance.accepted_coordinate() == head
    assert instance.proposal_evidence().read_withdrawal(source_id) is None
    assert _entry(instance, source_id).status == "open"
    # Approval still lands it at the head as before.
    landed = _approve(instance, reviewer, source_id)
    assert isinstance(landed, ProposalRecompiled) and landed.status == "pending_review"


def test_activating_an_unapproved_proposal_on_the_head_names_the_approve_repair(
    tmp_path: Path,
) -> None:
    instance, _reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "on the head"))

    with pytest.raises(ApprovalRequirementUnmet) as refused:
        service_activate_playbill_proposal(instance, proposal_id=source_id, activated_by="owner")

    assert refused.value.repair.operation == "cruxible.proposal.approve"
    assert _entry(instance, source_id).status == "open"


def test_acting_again_on_an_accepted_proposal_says_accepted_not_stale(
    tmp_path: Path,
) -> None:
    """gapa-01/02: its own acceptance moved the head past its base, so approval and
    activation called it stale and sent the caller to readmit and withdraw, which
    both refuse. They answer that it is accepted, and where."""

    instance, reviewer = _world(tmp_path)
    proposal_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "accepted once"))
    receipt = _approve(instance, reviewer, proposal_id)
    assert receipt.activation is not None and receipt.activation.accepted_coordinate is not None
    accepted_at = receipt.activation.accepted_coordinate.git_oid
    approvals_before = instance.proposal_evidence().read_approvals(
        _proposal_result(instance, proposal_id).candidate.candidate_digest  # type: ignore[union-attr]
    )

    refusals = []
    for act in (
        lambda: service_activate_playbill_proposal(
            instance, proposal_id=proposal_id, activated_by="reviewer"
        ),
        lambda: _approve(instance, reviewer, proposal_id),
        lambda: service_prepare_playbill_approval(
            instance,
            proposal_id=proposal_id,
            signer_id="reviewer",
            access=BodyAccessContext(principal_id="reviewer", can_read_body=True),
        ),
    ):
        with pytest.raises(ProposalAlreadyAccepted) as refused:
            act()
        refusals.append(refused.value)

    for refusal in refusals:
        assert refusal.error_code == "cruxible.proposal.already_accepted"
        assert refusal.git_oid == accepted_at
        assert f"already accepted at {accepted_at}" in str(refusal)
        assert refusal.repair.operation == "cruxible.get"
    # The repeated approval stored nothing.
    assert (
        instance.proposal_evidence().read_approvals(
            _proposal_result(instance, proposal_id).candidate.candidate_digest  # type: ignore[union-attr]
        )
        == approvals_before
    )


def test_an_approval_that_lands_a_moved_proposal_names_its_source(tmp_path: Path) -> None:
    """gapa-03: approval lands a moved proposal at the head under a new id. The
    receipt for the landed proposal names the one reviewed, and the old id refuses
    with the replacement to activate, not "author the change again"."""

    instance, reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "held"))
    _accept_write(instance, reviewer, _set(f"{KIND}/wi-2", "title", "moves the head"))
    landed = _approve(instance, reviewer, source_id)
    assert isinstance(landed, ProposalRecompiled) and landed.proposal_id is not None
    successor = _proposal_result(instance, landed.proposal_id)
    assert successor.candidate is not None

    held = service_submit_playbill_approval(
        instance,
        proposal_id=landed.proposal_id,
        attestation=_sign(
            reviewer,
            successor.candidate.candidate_digest,
            successor.candidate.candidate.parent_semantic_root,
        ).attestation,
        authenticated_submitter="reviewer",
        activate_when_complete=False,
        may_activate=True,
    )
    assert held.not_activated == "held"  # type: ignore[union-attr]
    assert held.readmits == source_id  # type: ignore[union-attr]

    with pytest.raises(ProposalWithdrawnError) as refused:
        service_activate_playbill_proposal(instance, proposal_id=source_id, activated_by="owner")
    assert refused.value.superseded_by == landed.proposal_id
    assert f"act on {landed.proposal_id} instead: activate it" in str(refused.value)
    assert "author the change again" not in str(refused.value)
    assert refused.value.repair.operation == "cruxible.proposal.activate"
    assert refused.value.repair.arguments == {"proposal_id": landed.proposal_id}

    activation = service_activate_playbill_proposal(
        instance, proposal_id=landed.proposal_id, activated_by="reviewer"
    )
    assert activation.status == "accepted"  # type: ignore[union-attr]
    assert activation.readmits == source_id  # type: ignore[union-attr]


def _card(instance: PlaybillInstance, proposal_id: str):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get

    return service_playbill_get(
        instance,
        request=GetRequest(ref=proposal_id),
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    )


def test_a_landed_proposal_closes_as_superseded_under_who_landed_it(tmp_path: Path) -> None:
    """psw F5: the reviewer's approval landed it; the source read `withdrawn, actor: owner`
    with no successor. It closes as superseded, naming the successor and the
    reviewer, in the record, the list entry and the card."""

    from cruxible_client.contracts.get_text import render_get

    instance, reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "held"))
    _accept_write(instance, reviewer, _set(f"{KIND}/wi-2", "title", "moves the head"))
    landed = _approve(instance, reviewer, source_id)
    assert isinstance(landed, ProposalRecompiled) and landed.members_identical

    record = instance.proposal_evidence().read_withdrawal(source_id)
    assert record is not None
    assert record.model_dump(mode="json", exclude={"reason", "withdrawn_at"}) == {
        "tag": "playbill-proposal-withdrawal-v2",
        "proposal_id": source_id,
        "actor_id": "reviewer",
        "kind": "superseded",
        "superseded_by": landed.proposal_id,
    }
    entry = _entry(instance, source_id)
    assert (entry.status, entry.terminal_reason) == ("settled", "superseded")
    assert (entry.superseded_by, entry.closed_by) == (landed.proposal_id, "reviewer")
    text = render_get(_card(instance, source_id))
    assert text is not None
    assert (
        f"reason: superseded\nsuperseded_by: {landed.proposal_id}\nclosed_by: reviewer\n"
        "actor: owner\n"
    ) in text
    assert "withdrawn" not in text
    # The successor is an ordinary open proposal, and names nothing of the kind.
    assert landed.proposal_id is not None
    assert _entry(instance, landed.proposal_id).superseded_by is None
    with pytest.raises(ProposalWithdrawnError, match=f"superseded by {landed.proposal_id}"):
        _approve(instance, reviewer, source_id)


def test_a_superseded_record_from_before_the_kind_stays_readable(tmp_path: Path) -> None:
    """A recompiled source was closed as `withdrawn` under its author with
    `superseded_by` set. Those v1 records parse byte for byte and read as
    superseded; who closed them was never recorded, so it is not shown."""

    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.proposal_models import ProposalWithdrawalRecord

    instance, _reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "old"))
    successor_id = _pending(instance, _set(f"{KIND}/wi-2", "title", "new"))
    stored = {
        "tag": "playbill-proposal-withdrawal-v1",
        "proposal_id": source_id,
        "actor_id": "owner",
        "reason": f"Recompiled at the head as {successor_id}.",
        "withdrawn_at": "2026-11-17T03:32:02.568547Z",
        "superseded_by": successor_id,
    }
    legacy = ProposalWithdrawalRecord.model_validate(stored)
    assert (legacy.kind, legacy.is_superseded) == ("withdrawn", True)
    assert canonical_bytes(legacy.model_dump(mode="json")) == canonical_bytes(stored)
    instance.proposal_evidence().write_withdrawal(legacy)

    entry = _entry(instance, source_id)
    assert (entry.terminal_reason, entry.superseded_by, entry.closed_by) == (
        "superseded",
        successor_id,
        None,
    )
    # The new kind is the one shape version 2 adds; v1 never carries it.
    for wrong in (
        {**stored, "kind": "superseded"},
        {**stored, "tag": "playbill-proposal-withdrawal-v2"},
        {**stored, "tag": "playbill-proposal-withdrawal-v2", "kind": "superseded"}
        | {"superseded_by": None},
    ):
        with pytest.raises(ValueError):
            ProposalWithdrawalRecord.model_validate(wrong)
    # An ordinary withdrawal keeps its v1 bytes.
    plain = ProposalWithdrawalRecord(
        proposal_id=source_id, actor_id="owner", reason="no", withdrawn_at="2026-11-17T00:00:00Z"
    )
    assert set(plain.model_dump(mode="json")) == {
        "tag",
        "proposal_id",
        "actor_id",
        "reason",
        "withdrawn_at",
    }


def test_an_approval_bound_to_the_reviewed_candidate_refuses_the_landed_one(
    tmp_path: Path,
) -> None:
    """coldmcp-10: bound to the reviewed digest, the approval lands the moved
    proposal and refuses, naming it and that its members are identical; nothing
    is stored for either candidate. A digest that is not the proposal's
    candidate refuses before anything lands."""

    from cruxible_client.contracts.errors import ReviewedCandidateChanged

    instance, reviewer = _world(tmp_path)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "held"))
    source = _proposal_result(instance, source_id)
    assert source.candidate is not None
    reviewed = source.candidate.candidate_digest
    signed = _sign(reviewer, reviewed, source.candidate.candidate.parent_semantic_root)
    other = "sha256:" + "9" * 64
    access = BodyAccessContext(principal_id="reviewer", can_read_body=True)

    def submit(digest: str):  # type: ignore[no-untyped-def]
        return service_submit_playbill_approval(
            instance,
            proposal_id=source_id,
            attestation=signed.attestation,
            authenticated_submitter="reviewer",
            activate_when_complete=True,
            may_activate=True,
            reviewed_candidate_digest=digest,
        )

    for refuse in (
        lambda: submit(other),
        lambda: service_prepare_playbill_approval(
            instance,
            proposal_id=source_id,
            signer_id="reviewer",
            access=access,
            reviewed_candidate_digest=other,
        ),
    ):
        with pytest.raises(ReviewedCandidateChanged) as wrong:
            refuse()
        assert f"proposal {source_id} is candidate {reviewed}, not the reviewed {other}" in str(
            wrong.value
        )
        assert wrong.value.repair.arguments == {"proposal_id": source_id}
    assert not instance.proposal_evidence().read_approvals(reviewed)

    _accept_write(instance, reviewer, _set(f"{KIND}/wi-2", "title", "moves the head"))
    # The challenge is still for the reviewed candidate; submitting it meets the moved head.
    challenge = service_prepare_playbill_approval(
        instance,
        proposal_id=source_id,
        signer_id="reviewer",
        access=access,
        reviewed_candidate_digest=reviewed,
    )
    assert challenge.statement.payload_digest == reviewed
    with pytest.raises(ReviewedCandidateChanged) as refused:
        submit(reviewed)

    successor_id = refused.value.current_proposal_id
    successor = _proposal_result(instance, successor_id)
    assert successor.candidate is not None and successor_id != source_id
    assert refused.value.error_code == "cruxible.approval.reviewed_candidate_changed"
    assert refused.value.members_identical is True
    assert str(refused.value) == (
        f"cruxible.approval.reviewed_candidate_changed: proposal {source_id} was proposed "
        f"again at the head as {successor_id} (candidate "
        f"{successor.candidate.candidate_digest}), not the reviewed {reviewed}; members "
        f"identical: true; nothing was signed. Review {successor_id} and approve it"
    )
    assert refused.value.repair.operation == "cruxible.proposal.review"
    assert refused.value.repair.arguments == {"proposal_id": successor_id}
    evidence = instance.proposal_evidence()
    assert not evidence.read_approvals(reviewed)
    assert not evidence.read_approvals(successor.candidate.candidate_digest)
    assert _entry(instance, successor_id).status == "open"
    assert _entry(instance, source_id).superseded_by == successor_id
    # Reviewed again, the successor is approved under its own digest.
    receipt = service_submit_playbill_approval(
        instance,
        proposal_id=successor_id,
        attestation=_sign(
            reviewer,
            successor.candidate.candidate_digest,
            successor.candidate.candidate.parent_semantic_root,
        ).attestation,
        authenticated_submitter="reviewer",
        activate_when_complete=True,
        may_activate=True,
        reviewed_candidate_digest=successor.candidate.candidate_digest,
    )
    assert receipt.activation is not None and receipt.activation.status == "accepted"  # type: ignore[union-attr]


@pytest.mark.parametrize("bound", [False, True])
def test_a_reviewer_registered_after_the_base_approves_by_landing_it_first(
    tmp_path: Path, bound: bool
) -> None:
    """F3 / coldmcp-01: a principal registered after a proposal was admitted is absent
    at its base, where its signature is checked, so `approve` refused "principal is
    absent". A caller who may submit approvals now lands the proposal at the head
    first (as approving a moved proposal does) and signs the landed candidate, whose
    parent registry holds the signer; one who may not is told why and who readmits.
    ``bound`` to the candidate it reviewed, the challenge lands it and refuses,
    naming the landed proposal to review."""

    from cruxible_client.contracts.errors import ApprovalSignerAfterBase
    from cruxible_core.governance.keys import generate_client_principal_key
    from cruxible_core.service.authoring.documents import (
        service_propose_playbill_principal_change,
    )

    instance, owner = seed_write_surface(tmp_path, self_approval=False)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "before late joined"))
    late = generate_client_principal_key(
        tmp_path / "late-keys",
        principal_id="late",
        kind="ordinary",
        forbidden_roots=(instance.root,),
    )
    registration = service_propose_playbill_principal_change(
        instance,
        principal=late.principal,
        actor_id="owner",
        proposal_name="register-late",
        timestamp="2026-09-29T12:40:00.000000Z",
    ).proposal
    assert registration.candidate is not None
    for material in (owner, reviewer := client_material(tmp_path, instance)):
        signed = _sign(
            material,
            registration.candidate.candidate_digest,
            registration.candidate.candidate.parent_semantic_root,
        )
        last = service_submit_playbill_approval(
            instance,
            proposal_id=registration.admission.proposal_id,
            attestation=signed.attestation,
            authenticated_submitter=material.principal.principal_id,
            activate_when_complete=True,
            may_activate=True,
        )
    assert last.activation is not None and last.activation.status == "accepted"  # type: ignore[union-attr]
    access = BodyAccessContext(principal_id="late", can_read_body=True)

    with pytest.raises(ApprovalSignerAfterBase) as refused:
        service_prepare_playbill_approval(
            instance, proposal_id=source_id, signer_id="late", access=access
        )
    assert refused.value.error_code == "cruxible.approval.signer_after_base"
    assert "otherwise its author 'owner' readmits it" in str(refused.value)
    assert refused.value.repair.arguments == {"proposal_id": source_id}
    assert instance.proposal_evidence().read_withdrawal(source_id) is None

    landed_by: list[str] = []

    def land() -> str:
        landed_by.append("checked")
        return "late"

    if bound:
        from cruxible_client.contracts.errors import ReviewedCandidateChanged

        reviewed = _proposal_result(instance, source_id).candidate.candidate_digest  # type: ignore[union-attr]
        with pytest.raises(ReviewedCandidateChanged) as changed:
            service_prepare_playbill_approval(
                instance,
                proposal_id=source_id,
                signer_id="late",
                access=access,
                land=land,
                reviewed_candidate_digest=reviewed,
            )
        closed = instance.proposal_evidence().read_withdrawal(source_id)
        assert closed is not None and (closed.kind, closed.actor_id) == ("superseded", "late")
        assert changed.value.current_proposal_id == closed.superseded_by
        assert changed.value.members_identical is True
        assert "members identical: true; nothing was signed" in str(changed.value)
        return
    challenge = service_prepare_playbill_approval(
        instance,
        proposal_id=source_id,
        signer_id="late",
        access=access,
        land=land,
    )
    assert landed_by == ["checked"]
    assert challenge.proposal_id != source_id
    assert challenge.signer_principal.principal_id == "late"
    head = instance.accepted_coordinate()
    assert challenge.statement.signing_semantic_root == head.semantic_root
    withdrawal = instance.proposal_evidence().read_withdrawal(source_id)
    assert withdrawal is not None and withdrawal.superseded_by == challenge.proposal_id
    # The caller whose challenge landed it closed the source, not its author.
    assert (withdrawal.kind, withdrawal.actor_id) == ("superseded", "late")

    receipt = service_submit_playbill_approval(
        instance,
        proposal_id=challenge.proposal_id,
        attestation=_sign(
            late, challenge.statement.payload_digest, challenge.statement.signing_semantic_root
        ).attestation,
        authenticated_submitter="late",
        activate_when_complete=True,
        may_activate=True,
    )
    assert receipt.activation is not None and receipt.activation.status == "accepted"  # type: ignore[union-attr]
    assert receipt.readmits == source_id  # type: ignore[union-attr]
    assert reviewer.principal.principal_id == "reviewer"


def test_an_ineligible_late_signer_is_refused_before_anything_lands(tmp_path: Path) -> None:
    """Round 3: a recovery principal registered after an ordinary proposal's base
    cannot approve it; the challenge landed the proposal (closing the source and
    losing its signatures) before refusing. It now refuses first and changes nothing."""

    from cruxible_client.contracts.errors import ApprovalIntegrityError
    from cruxible_core.governance.keys import generate_client_principal_key
    from cruxible_core.service.authoring.documents import (
        service_propose_playbill_principal_change,
    )

    instance, owner = seed_write_surface(tmp_path, self_approval=False)
    reviewer = client_material(tmp_path, instance)
    source_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "before recovery joined"))
    rescue = generate_client_principal_key(
        tmp_path / "rescue-keys",
        principal_id="rescue",
        kind="recovery",
        forbidden_roots=(instance.root,),
    )
    registration = service_propose_playbill_principal_change(
        instance,
        principal=rescue.principal,
        actor_id="owner",
        proposal_name="register-rescue",
        timestamp="2026-09-29T12:50:00.000000Z",
    ).proposal
    assert registration.candidate is not None
    for material in (owner, reviewer):
        last = service_submit_playbill_approval(
            instance,
            proposal_id=registration.admission.proposal_id,
            attestation=_sign(
                material,
                registration.candidate.candidate_digest,
                registration.candidate.candidate.parent_semantic_root,
            ).attestation,
            authenticated_submitter=material.principal.principal_id,
            activate_when_complete=True,
            may_activate=True,
        )
    assert last.activation is not None and last.activation.status == "accepted"  # type: ignore[union-attr]
    admissions = instance.proposal_evidence().list_admissions()
    head = instance.accepted_coordinate()
    landed: list[str] = []

    with pytest.raises(ApprovalIntegrityError, match="recovery principal cannot approve"):
        service_prepare_playbill_approval(
            instance,
            proposal_id=source_id,
            signer_id="rescue",
            access=BodyAccessContext(principal_id="rescue", can_read_body=True),
            land=lambda: landed.append("landed") or "rescue",
        )

    assert landed == []
    assert instance.proposal_evidence().read_withdrawal(source_id) is None
    assert instance.proposal_evidence().list_admissions() == admissions
    assert instance.accepted_coordinate() == head
    assert _entry(instance, source_id).status == "open"


def test_withdrawing_or_rejecting_an_accepted_proposal_says_accepted(tmp_path: Path) -> None:
    """Withdraw and reject answered "only an open or stale proposal may be withdrawn;
    this one is accepted" with no code or repair; typed as approve and activate are."""

    from cruxible_core.service.proposals.proposals import (
        service_reject_playbill_proposal,
        service_withdraw_playbill_proposal,
    )

    instance, reviewer = _world(tmp_path)
    proposal_id = _pending(instance, _set(f"{KIND}/wi-1", "title", "accepted once"))
    receipt = _approve(instance, reviewer, proposal_id)
    assert receipt.activation is not None and receipt.activation.accepted_coordinate is not None
    accepted_at = receipt.activation.accepted_coordinate.git_oid

    for verb, act in (
        (
            "withdraw",
            lambda: service_withdraw_playbill_proposal(
                instance,
                proposal_id=proposal_id,
                actor_id="owner",
                reason="no",
                withdrawn_at="2026-11-16T12:00:00.000000Z",
            ),
        ),
        (
            "reject",
            lambda: service_reject_playbill_proposal(
                instance,
                proposal_id=proposal_id,
                actor_id="reviewer",
                reason="no",
                rejected_at="2026-11-16T12:00:00.000000Z",
            ),
        ),
    ):
        with pytest.raises(ProposalAlreadyAccepted) as refused:
            act()
        assert refused.value.git_oid == accepted_at
        assert f"nothing left to {verb}" in str(refused.value)
        assert refused.value.repair.operation == "cruxible.get"
    assert instance.proposal_evidence().read_withdrawal(proposal_id) is None
