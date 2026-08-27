"""PB-D accepted Document explanation-fact projection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.playbill.assembler import ProjectionAssembler
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.explanation import (
    BasisRelation,
    CoverageBinding,
    LedgerProofReference,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.serving import bind_current_projection
from cruxible_core.playbill.settlement import (
    ChangeActorBinding,
    ChangeSetRecordV3,
    prepare_generation,
)
from cruxible_core.storage.playbill_projection import bind_projection

from .test_activation import _candidate, _instance, _sign


def _accepted_document(tmp_path: Path):
    instance, _owner, reviewer = _instance(tmp_path)
    body = b"private body phrase that must not enter explanation facts"
    base, tree, candidate = _candidate(instance, body_content=body)
    approvals = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)
    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=approvals,
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(
            actor_id="owner",
            source_compilation_digest="sha256:" + "77" * 32,
        ),
        proposal_actor_id="owner",
        sequence=1,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    return reopened, bundle, projection, body


def _facts(view) -> dict[str, object]:
    return {fact.schema_id: fact.value for fact in view.facts}


def test_accepted_document_emits_composable_exact_proof_facts_without_body_leakage(
    tmp_path: Path,
) -> None:
    instance, bundle, _projection, body = _accepted_document(tmp_path)
    publication = Path(instance.inspect().storage_directories["projections"])
    coordinate = instance.accepted_coordinate()
    with bind_current_projection(publication, expected=coordinate) as handle:
        view = handle.document(
            "document:design",
            access=BodyAccessContext(principal_id="auditor", can_read_body=False),
        )
        assert view is not None
        facts = _facts(view)

    assert {
        "playbill.document.attestation_coverage",
        "playbill.document.governance",
        "playbill.document.history",
        "playbill.document.provenance",
    }.issubset(facts)
    governance = facts["playbill.document.governance"]
    assert governance["law_identifier"] == "playbill.document.v1"  # type: ignore[index]
    assert governance["required_tier"] == "graph_write"  # type: ignore[index]
    assert governance["activation_policy"] == "snapshot"  # type: ignore[index]

    coverage = facts["playbill.document.attestation_coverage"]
    binding = coverage["coverage_binding"]  # type: ignore[index]
    assert binding["coverage"] == "containing_change_set"
    assert "exact_subject" not in str(coverage)
    assert "containing_artifact" not in str(coverage)
    assert {item["kind"] for item in coverage["basis"]} == {  # type: ignore[index]
        "authority_ruled",
        "cryptographically_committed",
        "replay_verified",
    }
    attestations = coverage["attestations"]  # type: ignore[index]
    assert [item["signer_id"] for item in attestations] == ["reviewer"]
    for item in attestations:
        assert item["attestation_digest"]["$digest"].startswith("sha256:")
        assert item["key_history_ref"]["principal_path"]["$path"] == (
            f"principals/{item['signer_id']}.yaml"
        )
        assert item["key_history_ref"]["semantic_root"]["$digest"] == (
            bundle.record.candidate.parent_semantic_root
        )

    proof = binding["proof_ref"]
    assert proof["accepted_coordinate"]["git_oid"] == coordinate.git_oid
    assert proof["accepted_coordinate"]["semantic_root"]["$digest"] == (coordinate.semantic_root)
    record_path = proof["change_set_path"]["$path"]
    stored = ChangeSetRecordV3.model_validate_json(
        instance._ledger.read_tree(coordinate.git_oid)[record_path]
    )
    assert proof["changeset_digest"]["$digest"] == stored.changeset_digest
    assert proof["candidate_digest"]["$digest"] == stored.candidate_digest

    provenance = facts["playbill.document.provenance"]
    assert provenance["actor_id"] == "owner"  # type: ignore[index]
    assert provenance["source_compilation_digest"] == {  # type: ignore[index]
        "$digest": "sha256:" + "77" * 32
    }
    serialized = str(facts)
    assert body.decode() not in serialized
    assert "start_byte" not in serialized and "end_byte" not in serialized


def test_rebuild_from_head_reproduces_explanation_logical_digest_and_facts(
    tmp_path: Path,
) -> None:
    instance, _bundle, accepted_projection, _body = _accepted_document(tmp_path)
    alternate = tmp_path / "alternate-projection"
    alternate.mkdir()
    assembler = ProjectionAssembler(
        instance._ledger,
        accepted=instance.accepted_coordinate(),
        publication_directory=alternate,
        bodies=instance.body_store(),
    )
    rebuilt = assembler.assemble(
        assembler.request(output_staging_directory=alternate / ".stage-rebuild")
    )

    assert rebuilt.logical_digest == accepted_projection.logical_digest
    with (
        bind_projection(
            Path(accepted_projection.manifest_path),
            expected=instance.accepted_coordinate(),
        ) as accepted_handle,
        bind_projection(
            Path(rebuilt.manifest_path),
            expected=instance.accepted_coordinate(),
        ) as rebuilt_handle,
    ):
        access = BodyAccessContext(principal_id="auditor", can_read_body=False)
        accepted = accepted_handle.document("document:design", access=access)
        replayed = rebuilt_handle.document("document:design", access=access)
        assert accepted is not None and replayed is not None
        assert accepted.facts == replayed.facts


def test_coverage_and_basis_contracts_refuse_laundering_or_mismatched_proofs() -> None:
    proof = LedgerProofReference(
        change_set_path="changesets/cs-00000000000000000001.json",
        changeset_digest="sha256:" + "11" * 32,
        candidate_digest="sha256:" + "22" * 32,
    )
    with pytest.raises(ValueError, match="only its containing change set"):
        CoverageBinding(
            coverage="exact_subject",
            subject_path="documents/design.yaml",
            signed_payload_digest=proof.candidate_digest,
            proof_ref=proof,
        )
    with pytest.raises(ValueError, match="differs"):
        CoverageBinding(
            coverage="containing_change_set",
            subject_path="documents/design.yaml",
            signed_payload_digest="sha256:" + "33" * 32,
            proof_ref=proof,
        )
    with pytest.raises(ValueError, match="Input should be"):
        BasisRelation.model_validate(
            {
                "kind": "confidence_scored",
                "proof_ref": proof.model_dump(mode="json"),
            }
        )
