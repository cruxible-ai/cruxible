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
from cruxible_client.contracts.errors import ProposalStale
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
from cruxible_core.service.authoring.documents import service_submit_playbill_approval
from cruxible_core.service.proposals.proposals import (
    _proposal_result,
    service_list_playbill_proposals,
    service_playbill_proposal_status,
    stale_unreadmitted_proposals,
)
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
        notes = _claim_type("notes", literal_schema={"type": "string"})
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
