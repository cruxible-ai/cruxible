"""Acceptance fixes an admission identity independently of future proposal inventory."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.ledger.generation_facts import AcceptingAdmission, parse_accepting_admission
from cruxible_core.proposals.proposal_message import proposal_commit_message
from cruxible_core.proposals.proposal_notes import proposal_admission_id
from cruxible_core.runtime import instance as instance_module
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery import change_reasons
from cruxible_core.service.discovery.change_reasons import ChangeReasons
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.floor.floor import service_export_playbill_floor
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface

_ACCESS = BodyAccessContext(principal_id="owner", can_read_body=True)


def _cold(instance: PlaybillInstance) -> None:
    change_reasons._ADMISSIONS.clear()
    instance_module._ACCEPTING_ADMISSIONS.clear()
    instance.floor_structure_memo.clear()
    instance.floor_current_memo.clear()


def _seed(tmp_path: Path) -> tuple[PlaybillInstance, str, Any, Any]:
    instance, _ = seed_write_surface(tmp_path)
    written = service_playbill_write(
        instance,
        request=WriteRequest.model_validate(
            {
                "because": "First accepting reason.",
                "changes": [
                    {"op": "set", "subject": f"{KIND}/wi-1", "field": "status", "value": "ready"}
                ],
            }
        ),
        caller=caller(),
    )
    assert written.status == "accepted", written
    claim = str(written.changes[0].claim)
    with instance.accepted_history_reader() as history:
        generation = history.generation(history.sequence)
    admission = ChangeReasons(instance).admission(generation)
    assert admission is not None
    return instance, claim, generation, admission


def _late_admission(
    instance: PlaybillInstance, generation: Any, admission: Any, number: int
) -> str:
    """Land another valid immutable admission evaluated at the same old base.

    Its evaluation is the same candidate, actor and instant. Only the admission
    (ref, prose commit and because) differs. Persist it directly as delayed
    evidence: ordinary fresh submission would re-evaluate at today's head.
    """

    evidence = instance.proposal_evidence()
    original_evaluation = evidence.read_evaluation(admission.proposal_id)
    with instance.accepted_history_reader() as history:
        record = history.read_generation_record(
            generation.sequence, instance.retained_record_reader()
        )
    request = ProposalAdmissionRequest(
        target_ref=f"refs/proposals/owner/late-{number}",
        proposed_base_oid=admission.proposed_base_oid,
        source_compilation_digest=admission.source_compilation_digest,
        rationale=f"Different later reason {number}.",
    )
    oid, tree_oid = instance._ledger.create_proposal_commit(
        instance.proposal_tree(admission.candidate_tree_oid),
        base_oid=admission.proposed_base_oid,
        target_ref=request.target_ref,
        actor_id=admission.actor_id,
        timestamp=admission.admitted_at,
        expected_ref_oid=None,
        message=proposal_commit_message(record.members, rationale=request.rationale),
    )
    proposal_id = proposal_admission_id(
        actor_id=admission.actor_id,
        request=request,
        candidate_commit_oid=oid,
        candidate_tree_oid=tree_oid,
        admitted_at=admission.admitted_at,
        limits=admission.limits,
    )
    later = admission.model_copy(
        update={
            "proposal_id": proposal_id,
            "target_ref": request.target_ref,
            "candidate_commit_oid": oid,
            "candidate_tree_oid": tree_oid,
            "rationale": request.rationale,
        }
    )
    with evidence.publication():
        evidence.write_evaluation(
            original_evaluation.model_copy(update={"proposal_id": proposal_id})
        )
        evidence.write_admission(later)
    assert evidence.read_evaluation(proposal_id).candidate_digest == generation.candidate_digest
    return proposal_id


def _history(instance: PlaybillInstance, claim: str) -> Any:
    history = service_playbill_get(
        instance, request=GetRequest(ref=claim, detail="history"), access=_ACCESS
    ).history
    assert history is not None
    return history


def test_later_same_candidate_admission_cannot_change_history_or_floor(tmp_path: Path) -> None:
    instance, claim, generation, admission = _seed(tmp_path)
    expected = _history(instance, claim)
    assert expected.revisions[0].reason.text == "First accepting reason."
    floor = service_export_playbill_floor(instance)
    later = _late_admission(instance, generation, admission, 1)
    assert later != admission.proposal_id
    assert _history(instance, claim) == expected
    assert service_export_playbill_floor(instance) == floor
    _cold(instance)
    assert _history(instance, claim) == expected
    assert service_export_playbill_floor(instance) == floor
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    _cold(reopened)
    assert _history(reopened, claim) == expected
    assert service_export_playbill_floor(reopened) == floor
    assert reopened.accepting_admission(generation.git_oid) == AcceptingAdmission.of(admission)
    with reopened.accepted_history_reader() as history:
        assert ChangeReasons(reopened).rationale(
            history.generation(generation.sequence), "unused"
        ) == ("First accepting reason.")
    changes = [json.loads(body) for path, body in floor.items() if path.startswith("changes/")]
    assert changes[-1]["rationale"] == ["First accepting reason."]


def test_legacy_generations_keep_unique_candidate_actor_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _claim, generation, admission = _seed(tmp_path)
    # A headerless generation remains on precisely the old resolver.
    monkeypatch.setattr(instance, "accepting_admission", lambda _oid: None)
    assert ChangeReasons(instance).admission(generation) == admission
    _late_admission(instance, generation, admission, 1)
    assert ChangeReasons(instance).admission(generation) is None


def test_reason_reads_by_identity_at_two_inventory_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _claim, generation, admission = _seed(tmp_path)
    index = type(instance.proposal_evidence().index)
    rows = index.rows
    work = []

    def counted(self: Any, evidence: Any, where: str, parameters: Any = ()) -> Any:
        result = rows(self, evidence, where, parameters)
        work.append((where, len(result)))
        return result

    for size in (1, 30):
        for number in range(1 if size == 1 else 2, size + 1):
            _late_admission(instance, generation, admission, number)
        _cold(instance)
        with monkeypatch.context() as patch:
            patch.setattr(index, "rows", counted)
            resolver = ChangeReasons(instance)
            resolver.prefetch([generation])
            assert resolver.admission(generation) == admission
        assert work == [("proposal_id=?", 1)], work
        work.clear()


def test_recorded_digest_and_actor_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _claim, generation, admission = _seed(tmp_path)
    identity = AcceptingAdmission.of(admission)
    monkeypatch.setattr(
        instance,
        "accepting_admission",
        lambda _oid: identity.model_copy(update={"admission_digest": "sha256:" + "00" * 32}),
    )
    _cold(instance)
    with pytest.raises(SettlementIntegrityError, match="digest differs"):
        ChangeReasons(instance).admission(generation)
    monkeypatch.setattr(instance, "accepting_admission", lambda _oid: identity)
    with pytest.raises(SettlementIntegrityError, match="actor differs"):
        ChangeReasons(instance).admission(replace(generation, actor_id="reviewer"))


def test_generation_header_has_a_strict_versioned_canonical_shape() -> None:
    fact = AcceptingAdmission(
        proposal_id="sha256:" + "11" * 32, admission_digest="sha256:" + "22" * 32
    )
    raw = fact.header_value().encode()
    assert parse_accepting_admission(raw) == fact
    for invalid in (raw + b" ", raw.replace(b"-v1", b"-v2"), b"{}"):
        with pytest.raises(SettlementIntegrityError, match="header is malformed"):
            parse_accepting_admission(invalid)
