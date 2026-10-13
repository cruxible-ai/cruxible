"""Accepted candidates are read from their change-set records once their files go."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import WriteRequest
from cruxible_core.proposals.proposal_evidence import SettledCandidate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.proposals.proposals import (
    service_list_playbill_proposals,
    service_playbill_proposal_status,
)
from cruxible_core.service.proposals.review import service_review_playbill_proposal
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface
from tests.test_floor.test_floor_index import _set

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
ACCESS = BodyAccessContext(principal_id="owner", can_read_body=True)


def _write(instance: PlaybillInstance, *changes: dict[str, Any], **options: Any) -> Any:
    return service_playbill_write(
        instance,
        request=WriteRequest.model_validate(
            {"because": "The writer checked it.", "changes": list(changes), **options}
        ),
        caller=caller(),
    )


def _views(instance: PlaybillInstance, proposal_id: str) -> tuple[Any, ...]:
    return (
        service_inspect_playbill_proposal(instance, proposal_id=proposal_id),
        service_review_playbill_proposal(instance, proposal_id=proposal_id, access=ACCESS),
        service_playbill_proposal_status(instance, proposal_id=proposal_id),
        service_list_playbill_proposals(instance),
    )


def _candidate_files(instance: PlaybillInstance) -> set[str]:
    return {path.name for path in instance.proposal_evidence().candidates.iterdir()}


def test_inspect_review_status_and_the_index_answer_from_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    # Hold retirement back to read the views with the files still there.
    monkeypatch.setattr(PlaybillInstance, "retire_settled_candidates", lambda *_a, **_k: 0)
    accepted = _write(instance, _set(WI1, "status", "ready"))
    assert accepted.status == "accepted"
    proposal_id = accepted.proposal.proposal_id
    left_open = _write(instance, _set(WI2, "title", "Two"), accept="never")
    assert left_open.status != "accepted"
    before = _views(instance, proposal_id)
    files = _candidate_files(instance)
    monkeypatch.undo()

    assert instance.retire_settled_candidates() >= 1
    after_files = _candidate_files(instance)
    digest = before[0].proposal.candidate.candidate_digest.removeprefix("sha256:")
    assert f"{digest}.json" in files and f"{digest}.json" not in after_files
    assert f"{digest}.settled" in after_files
    # The open proposal's candidate is never touched.
    assert {name for name in files if name.endswith(".json")} - after_files == {f"{digest}.json"}
    # A fresh process reads the same answers from the record.
    instance._settled_candidates.clear()
    assert _views(instance, proposal_id) == before
    # Retiring again finds nothing left to remove.
    assert instance.retire_settled_candidates() == 0


def test_activation_retires_its_own_candidate(tmp_path: Path) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    accepted = _write(instance, _set(WI1, "status", "blocked"))
    inspection = service_inspect_playbill_proposal(
        instance, proposal_id=accepted.proposal.proposal_id
    )
    digest = inspection.proposal.candidate.candidate_digest.removeprefix("sha256:")
    files = _candidate_files(instance)
    assert f"{digest}.json" not in files and f"{digest}.settled" in files


def test_approvals_the_record_does_not_hold_are_kept(tmp_path: Path) -> None:
    from cruxible_core.proposals.proposal_evidence import SettledCandidateLocator

    instance, _owner = seed_write_surface(tmp_path)
    evidence = instance.proposal_evidence()
    accepted = _write(instance, _set(WI1, "status", "done"))
    candidate = service_inspect_playbill_proposal(
        instance, proposal_id=accepted.proposal.proposal_id
    ).proposal.candidate
    name = candidate.candidate_digest.removeprefix("sha256:")
    # An approval file the record never settled (submitted after acceptance).
    stray = evidence.approvals / name
    stray.mkdir(mode=0o700)
    (stray / "late-signer.json").write_bytes(b"{}\n")
    locator = evidence.read_settled_locator(candidate.candidate_digest)
    assert isinstance(locator, SettledCandidateLocator)
    removed = evidence.retire_settled_candidate(
        locator, SettledCandidate(candidate=candidate, approvals=())
    )
    assert removed == (False, False)
    assert (stray / "late-signer.json").read_bytes() == b"{}\n"
