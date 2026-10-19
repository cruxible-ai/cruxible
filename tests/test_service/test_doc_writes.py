"""Daemon admission of section bytes and located own words; no workspace I/O."""

from pathlib import Path

from cruxible_client.contracts.claims import parse_claim, claim_path
from cruxible_client.contracts.write import WriteRequest, DocEditObservation, SectionEvidence, SetChange
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._write_support import KIND, caller, seed_write_surface
from tests.test_client.test_doc_write_contracts import _edit, _observation


def test_located_own_words_adds_copy_and_retries_without_generation(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    request = WriteRequest(because="Recorded in prose", changes=(SetChange(
        subject=f"{KIND}/wi-1", field="status", value="ready",
        evidence=SectionEvidence(file="notes.md", section=("Reason",), own_words=True, observation=_observation()),
    ),), docs=(DocEditObservation.model_validate(_edit()),))
    first = service_playbill_write(instance, request=request, caller=caller())
    assert first.status == "accepted", first
    change = first.changes[0]
    assert change.verdict == "supported"
    path = claim_path(change.claim)
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[path], path=path)
    assert {(c.role, c.origin) for c in claim.backing.citations} == {("copy", "self_source"), ("copy", "independent")}
    retried = service_playbill_write(instance, request=request, caller=caller())
    assert retried.status == "accepted", retried
    assert retried.coordinate == first.coordinate


def test_captured_only_refuses_authored_section_and_docs_only_has_no_generation(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    before = instance.accepted_coordinate()
    request = WriteRequest(because="Reason", changes=(SetChange(
        subject=f"{KIND}/wi-1", field="measured", value=7,
        evidence=SectionEvidence(file="notes.md", section=("Reason",), own_words=True, observation=_observation()),
    ),))
    result = service_playbill_write(instance, request=request, caller=caller())
    assert result.refusal.code == "cruxible.write.evidence_required", result
    docs = service_playbill_write(instance, request=WriteRequest(
        because="Prose only", docs=(DocEditObservation.model_validate(_edit()),),
    ), caller=caller())
    assert docs.status == "accepted" and docs.proposal is None and not docs.changes
    assert instance.accepted_coordinate() == before


def test_daemon_refuses_unobserved_doc_operations(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    result = service_playbill_write(instance, request=WriteRequest.model_validate({
        "because": "Prose", "docs": [{"op": "append", "file": "arbitrary.md", "text": "# A"}],
    }), caller=caller())
    assert result.refusal.code == "cruxible.write.doc_unobserved"
    assert result.refusal.repair
