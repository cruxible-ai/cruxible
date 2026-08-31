"""PB-C proposal admission, candidate identity, and deterministic rebase tests."""

from __future__ import annotations

import json
from inspect import getsource
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from cruxible_client.contracts.candidates import (
    CandidateRecord,
    SemanticCandidate,
    candidate_digest,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import (
    ProposalAdmissionError,
    ProposalEvaluationIntegrityError,
    ProposalIntegrityError,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.proposal_evidence import ProposalEvidenceStore
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalService,
    deterministic_rebase,
    evaluate_proposal_tree,
)
from cruxible_core.playbill.service.claim_types import (
    service_propose_playbill_claim_type,
    service_propose_playbill_claim_type_input,
)
from cruxible_core.playbill.service.documents import (
    _candidate_for_proposal,
    service_propose_playbill_document,
)
from cruxible_core.playbill.service.proposal_names import canonical_playbill_proposal_name
from cruxible_core.service.playbill_evidence import service_propose_claim_attestation
from tests.test_playbill._support import initialize_local

TIMESTAMP = "2026-08-11T12:30:00.000000Z"
DOCUMENT_PATH = "documents/playbill-design.yaml"


def _shell(body_digest: str, *, title: str = "Playbill design") -> DocumentShell:
    return DocumentShell(
        identity="document:playbill-design",
        document_kind="design",
        title=title,
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )


def _proposal_tree(instance: PlaybillInstance, shell: DocumentShell) -> dict[str, bytes]:
    service = instance.proposal_service()
    tree = service.transport.read_tree(instance.inspect().head_oid)
    return {**tree, DOCUMENT_PATH: render_document(shell)}


def _request(
    instance: PlaybillInstance,
    *,
    target_ref: str = "refs/proposals/owner/document",
) -> ProposalAdmissionRequest:
    return ProposalAdmissionRequest(
        target_ref=target_ref,
        proposed_base_oid=instance.inspect().head_oid,
        source_compilation_digest="sha256:" + "73" * 32,
    )


def test_store_then_propose_creates_complete_candidate_without_changing_main(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body("# Café\n".encode())
    service = instance.proposal_service()
    before = instance.inspect()

    result = service.submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=_request(instance),
        candidate_tree=_proposal_tree(instance, _shell(body.digest)),
        timestamp=TIMESTAMP,
    )

    assert result.evaluation.verdict == "candidate"
    assert result.candidate is not None
    assert result.candidate.candidate.model_dump() == {
        "tag": "playbill-candidate-v2",
        "parent_semantic_root": before.semantic_root,
        "candidate_manifest_root": result.candidate.candidate.candidate_manifest_root,
        "semantic_diff_digest": result.candidate.candidate.semantic_diff_digest,
        "scope": (DOCUMENT_PATH,),
        "timestamp": TIMESTAMP,
    }
    assert result.candidate.required_tier == "graph_write"
    assert result.candidate.approval_requirements == ()
    assert result.candidate.activation_policy == "snapshot"
    assert result.candidate.members[0].artifact_kind == "document"
    assert result.candidate.members[0].law_identifier == "playbill.document.v1"
    assert list(result.candidate.law_digests) == ["playbill.document.v1"]
    assert result.candidate.compiler_digest == before.compiler.rule_digest
    assert instance.inspect() == before
    assert service.transport.read_main() == before.head_oid
    assert service.transport.read_proposal_ref(_request(instance).target_ref) == (
        result.admission.candidate_commit_oid
    )
    assert result.admission.source_compilation_digest == "sha256:" + "73" * 32

    exhaust = Path(instance.inspect().storage_directories["exhaust"])
    admissions = list((exhaust / "proposals").glob("*.json"))
    candidates = list((exhaust / "candidates").glob("*.json"))
    assert len(admissions) == len(candidates) == 1
    persisted = json.loads(admissions[0].read_bytes())
    assert persisted["source_compilation_digest"] == "sha256:" + "73" * 32
    assert "source_path" not in persisted


def test_refusal_keeps_evidence_but_creates_no_candidate(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    missing = _shell("sha256:" + "ff" * 32)
    service = instance.proposal_service()
    before = instance.inspect()

    result = service.submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=_request(instance),
        candidate_tree=_proposal_tree(instance, missing),
        timestamp=TIMESTAMP,
    )

    assert result.evaluation.verdict == "refused"
    assert result.candidate is None
    assert [item.code for item in result.evaluation.diagnostics] == [
        "playbill.document.body_missing"
    ]
    assert instance.inspect() == before
    exhaust = Path(instance.inspect().storage_directories["exhaust"])
    assert len(list((exhaust / "proposals").glob("*.json"))) == 1
    assert len(list((exhaust / "evaluations").glob("*.json"))) == 1
    assert list((exhaust / "candidates").glob("*.json")) == []

    with pytest.raises(
        ProposalIntegrityError,
        match=(
            r"refused proposal has no approvable candidate; run "
            rf"`playbill proposal refusal {result.admission.proposal_id}` for refusal code "
            r"playbill\.document\.body_missing"
        ),
    ):
        _candidate_for_proposal(instance, result.admission.proposal_id)


def test_internal_evaluation_model_failure_uses_the_narrow_integrity_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_core.playbill import proposals

    class BrokenEvaluationRecord(BaseModel):
        missing_internal_field: str

    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"body")
    monkeypatch.setattr(proposals, "ProposalEvaluationRecord", BrokenEvaluationRecord)

    with pytest.raises(
        ProposalEvaluationIntegrityError,
        match="failed deterministic validation",
    ):
        instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=_request(instance),
            candidate_tree=_proposal_tree(instance, _shell(body.digest)),
            timestamp=TIMESTAMP,
        )


@pytest.mark.parametrize(
    "target_ref",
    (
        "refs/heads/main",
        "refs/tags/release",
        "refs/notes/review",
        "refs/meta/daemon",
    ),
)
def test_request_model_refuses_non_proposal_namespaces(target_ref: str) -> None:
    with pytest.raises(ValidationError, match="target_ref"):
        ProposalAdmissionRequest(
            target_ref=target_ref,
            proposed_base_oid="0" * 40,
        )


@pytest.mark.parametrize(
    ("family", "entrypoint"),
    (
        ("document", service_propose_playbill_document),
        ("claim attestation", service_propose_claim_attestation),
        ("claim type", service_propose_playbill_claim_type),
        ("claim type input", service_propose_playbill_claim_type_input),
    ),
)
def test_every_proposal_family_lowers_a_spaced_display_name(
    family: str,
    entrypoint: object,
) -> None:
    assert canonical_playbill_proposal_name(f"Add {family.title()}", family=family).startswith(
        "add-"
    )
    source = getsource(entrypoint)
    assert "canonical_playbill_proposal_name(proposal_name" in source
    assert 'target_ref=f"refs/proposals/{actor_id}/{proposal_name}"' not in source


def test_foreign_actor_namespace_refuses_before_any_ref_changes(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"body")
    service = instance.proposal_service()
    before = instance.inspect()
    target = "refs/proposals/other/document"

    with pytest.raises(ProposalAdmissionError, match="authenticated actor"):
        service.submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=_request(instance, target_ref=target),
            candidate_tree=_proposal_tree(instance, _shell(body.digest)),
            timestamp=TIMESTAMP,
        )

    assert service.transport.read_proposal_ref(target) is None
    assert instance.inspect() == before


def test_admission_refuses_local_locator_and_malformed_compilation_digest() -> None:
    payload = {
        "target_ref": "refs/proposals/owner/document",
        "proposed_base_oid": "0" * 40,
    }
    with pytest.raises(ValidationError, match="source_path"):
        ProposalAdmissionRequest.model_validate({**payload, "source_path": "/tmp/doc.md"})
    with pytest.raises(ValidationError, match="source_compilation_digest"):
        ProposalAdmissionRequest.model_validate({**payload, "source_compilation_digest": "latest"})
    with pytest.raises(ValidationError, match="limits"):
        ProposalAdmissionRequest.model_validate({**payload, "limits": {"max_files": 1}})
    assert ProposalAdmissionRequest.model_validate(payload).source_compilation_digest is None


def test_daemon_metadata_change_refuses_before_proposal_ref_update(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"body")
    service = instance.proposal_service()
    target = "refs/proposals/owner/protected-path"
    tree = _proposal_tree(instance, _shell(body.digest))
    tree["changesets/forged.json"] = b"{}\n"

    with pytest.raises(ProposalAdmissionError, match="daemon-controlled"):
        service.submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=_request(instance, target_ref=target),
            candidate_tree=tree,
            timestamp=TIMESTAMP,
        )

    assert service.transport.read_proposal_ref(target) is None


def test_current_coordinate_provider_cannot_contradict_verified_base(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    accepted = instance.accepted_coordinate()
    contradictory = accepted.model_copy(update={"semantic_root": "sha256:" + "99" * 32})
    service = ProposalService(
        instance.proposal_service().transport,
        accepted=accepted,
        bodies=instance.body_store(),
        evidence=ProposalEvidenceStore(Path(instance.inspect().storage_directories["exhaust"])),
        current_coordinate=lambda: contradictory,
    )
    body = instance.store_document_body(b"body")

    with pytest.raises(ProposalAdmissionError, match="contradicts"):
        service.submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=_request(instance),
            candidate_tree=_proposal_tree(instance, _shell(body.digest)),
            timestamp=TIMESTAMP,
        )

    assert service.transport.read_proposal_ref(_request(instance).target_ref) is None


def test_candidate_preimage_is_exact_oid_free_and_matches_golden() -> None:
    fixture_path = Path(__file__).parents[1] / "goldens" / "playbill" / "candidate-v1.json"
    fixture = json.loads(fixture_path.read_bytes())
    candidate = SemanticCandidate.model_validate(fixture["candidate"])

    payload = candidate.model_dump(mode="json")
    payload.pop("tag")
    assert sorted(payload) == [
        "candidate_manifest_root",
        "parent_semantic_root",
        "scope",
        "semantic_diff_digest",
        "timestamp",
    ]
    assert (
        canonical_bytes({"tag": "playbill-candidate-v1", **payload}).decode()
        == (fixture["canonical_preimage"])
    )
    assert candidate_digest(candidate).tagged == fixture["candidate_digest"]

    for _qualified_object_format in ("sha1", "sha256"):
        assert candidate_digest(SemanticCandidate.model_validate(fixture["candidate"])) == (
            candidate_digest(candidate)
        )
    with pytest.raises(ValidationError, match="base_oid"):
        SemanticCandidate.model_validate({**fixture["candidate"], "base_oid": "0" * 40})


def test_rebase_changes_candidate_identity_and_conflicts_are_typed(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"proposed")
    shell = _shell(body.digest)
    base_tree = instance.proposal_service().transport.read_tree(instance.inspect().head_oid)
    proposed_tree = {**base_tree, DOCUMENT_PATH: render_document(shell)}
    current = instance.accepted_coordinate()
    first = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree=proposed_tree,
        current=current,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
    )
    assert first.candidate is not None

    unrelated_body = instance.store_document_body(b"unrelated")
    unrelated = _shell(unrelated_body.digest).model_copy(
        update={"identity": "document:unrelated", "title": "Unrelated"}
    )
    current_tree = {
        **base_tree,
        "documents/unrelated.yaml": render_document(unrelated),
    }
    moved = AcceptedProjectionCoordinate(
        **{
            **current.model_dump(),
            "git_oid": "3" * len(current.git_oid),
            "semantic_root": "sha256:" + "44" * 32,
            "generation_root": "sha256:" + "55" * 32,
        }
    )
    rebased = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=current_tree,
        proposed_tree=proposed_tree,
        current=moved,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=True,
    )
    assert rebased.candidate is not None
    assert rebased.candidate.candidate.parent_semantic_root == moved.semantic_root
    assert rebased.candidate.candidate_digest != first.candidate.candidate_digest

    conflicting_body = instance.store_document_body(b"conflicting")
    conflicting = _shell(conflicting_body.digest, title="Conflicting")
    conflict = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree={**base_tree, DOCUMENT_PATH: render_document(conflicting)},
        proposed_tree=proposed_tree,
        current=moved,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=True,
    )
    assert conflict.candidate is None
    assert [item.code for item in conflict.diagnostics] == ["playbill.proposal.rebase_conflict"]

    rebased_tree, conflicts = deterministic_rebase(
        base_tree=base_tree,
        current_tree=current_tree,
        proposed_tree=proposed_tree,
    )
    assert not conflicts
    assert rebased_tree["documents/unrelated.yaml"] == render_document(unrelated)


def test_candidate_record_refuses_digest_or_closure_substitution() -> None:
    candidate = SemanticCandidate(
        parent_semantic_root="sha256:" + "11" * 32,
        candidate_manifest_root="sha256:" + "22" * 32,
        semantic_diff_digest="sha256:" + "33" * 32,
        scope=(DOCUMENT_PATH,),
        timestamp=TIMESTAMP,
    )
    values = {
        "candidate": candidate,
        "candidate_digest": candidate_digest(candidate).tagged,
        "required_tier": "governed_write",
        "approval_requirements": (),
        "activation_policy": "snapshot",
        "closure_paths": (DOCUMENT_PATH,),
        "members": (
            {
                "path": DOCUMENT_PATH,
                "artifact_kind": "document",
                "artifact_digest": "sha256:" + "66" * 32,
                "disposition": "replacement",
                "law_identifier": "playbill.document.v1",
            },
        ),
        "law_digests": {
            "playbill.document.v1": "sha256:" + "44" * 32,
        },
        "compiler_digest": "sha256:" + "55" * 32,
    }
    with pytest.raises(ValidationError, match="does not reproduce"):
        CandidateRecord.model_validate({**values, "candidate_digest": "sha256:" + "99" * 32})
    with pytest.raises(ValidationError, match="closure"):
        CandidateRecord.model_validate({**values, "closure_paths": ("documents/other.yaml",)})


def test_candidate_record_refuses_law_mapping_or_member_substitution() -> None:
    candidate = SemanticCandidate(
        parent_semantic_root="sha256:" + "11" * 32,
        candidate_manifest_root="sha256:" + "22" * 32,
        semantic_diff_digest="sha256:" + "33" * 32,
        scope=(DOCUMENT_PATH,),
        timestamp=TIMESTAMP,
    )
    values = {
        "candidate": candidate,
        "candidate_digest": candidate_digest(candidate).tagged,
        "required_tier": "governed_write",
        "approval_requirements": (),
        "activation_policy": "snapshot",
        "closure_paths": (DOCUMENT_PATH,),
        "members": (
            {
                "path": DOCUMENT_PATH,
                "artifact_kind": "document",
                "artifact_digest": "sha256:" + "66" * 32,
                "disposition": "replacement",
                "law_identifier": "playbill.document.v1",
            },
        ),
        "law_digests": {"playbill.document.v1": "sha256:" + "44" * 32},
        "compiler_digest": "sha256:" + "55" * 32,
    }
    with pytest.raises(ValidationError, match="mapping differ"):
        CandidateRecord.model_validate(
            {
                **values,
                "law_digests": {"playbill.other.v1": "sha256:" + "44" * 32},
            }
        )
    with pytest.raises(ValidationError, match="members must enumerate"):
        CandidateRecord.model_validate({**values, "members": ()})
