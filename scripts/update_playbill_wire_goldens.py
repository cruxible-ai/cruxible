"""Rebuild the surviving Playbill fixtures and the TauBench seed example."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_client.authoring.inputs import ClaimInput, ProcedureInput
from cruxible_client.contracts.approval_policy import ApprovalPolicyV1
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.candidates import (
    CandidateMemberLawEvidenceV2,
    DependencyProofReferenceV1,
    MemberLawEvaluationV2,
    SemanticCandidate,
    SemanticCandidateV2,
    candidate_digest,
    candidate_member_evidence_digest,
    member_law_evidence_digest,
)
from cruxible_client.contracts.canonical import (
    ChangeSetDigest,
    canonical_bytes,
    manifest_root,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    render_claim_type,
)
from cruxible_client.contracts.merkle import (
    DEPGRAPH_LEAF_DOMAIN,
    DEPGRAPH_ROOT_DOMAIN,
    EMPTY_DEPENDENCY_EDGE_ROOT,
    EMPTY_MERKLE_ROOT,
    MERKLE_LEAF_DOMAIN,
    MERKLE_ROOT_DOMAIN,
    ROOT_PREFIX,
    build_merkle_manifest,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    query_definition_digest,
    render_query_definition,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_digest
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.playbill.bootstrap import bootstrap_root, genesis_semantic_root, genesis_tree
from cruxible_core.playbill.closure import (
    build_dependency_edge_tree,
    dependency_edge_members,
)
from cruxible_core.playbill.settlement import (
    ChangeSetRecordV3,
    change_set_digest,
    parse_change_set_record,
    render_change_set,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDENS = ROOT / "tests/goldens/playbill"
SEED = ROOT / "benchmarks/playbill_taubench/seed-example"


def _read(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise TypeError(f"{path} is not a JSON object")
    return payload


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remove_retired_claim_type_fields(payload: dict[str, object]) -> dict[str, object]:
    admission = payload.get("admission_policy")
    resolution = payload.get("resolution_policy")
    if not isinstance(admission, dict) or not isinstance(resolution, dict):
        raise TypeError("ClaimType fixture does not carry its policy objects")
    admission.pop("actor_requirements", None)
    admission.pop("transition_requirements", None)
    admission["corroboration_requirements"] = admission.pop("evidence_requirements", [])
    resolution.pop("authority_rule_digest", None)
    payload.pop("authority", None)
    return payload


def _direct_claim_type() -> ClaimType:
    contract_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"enum": ["blocked", "done", "ready"], "type": "string"},
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="direct-self-asserted",
                    claim_roles=("normative", "observation"),
                    capture_contract_digests=(contract_digest,),
                    evidence_kinds=("self_asserted",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _query_claim_type(predicate: str, *, object_kind: str = "literal") -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=("project.work_item",),
        object_kind=object_kind,  # type: ignore[arg-type]
        literal_schema={"type": "string"} if object_kind == "literal" else None,
        allowed_object_subject_kinds=() if object_kind == "literal" else ("project.person",),
        cardinality="one" if object_kind == "literal" else "many",
        permitted_roles=("normative",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one" if object_kind == "literal" else "many",
            eligible_verdicts=("supported",),
            selector="only_contender" if object_kind == "literal" else "all",
        ),
    )


def _update_claim_type_golden() -> None:
    path = GOLDENS / "claim-type-v1.json"
    fixture = _read(path)
    raw = fixture.get("claim_type")
    if not isinstance(raw, dict):
        raise TypeError("ClaimType golden has no claim_type object")
    claim_type = ClaimType.model_validate(_remove_retired_claim_type_fields(raw))
    fixture["claim_type"] = claim_type.model_dump(mode="json")
    fixture["canonical_wire"] = render_claim_type(claim_type).decode("utf-8")
    fixture["artifact_digest"] = claim_type_digest(claim_type).tagged
    _write(path, fixture)


def _update_query_golden() -> None:
    path = GOLDENS / "query-definition-v1.json"
    fixture = _read(path)
    raw = fixture.get("query_definition")
    if not isinstance(raw, dict):
        raise TypeError("QueryDefinition golden has no query_definition object")
    raw.pop("authority", None)
    digests = {
        "project.work_item.reviewed_by": claim_type_digest(
            _query_claim_type("project.work_item.reviewed_by", object_kind="subject")
        ).tagged,
        "project.work_item.status": claim_type_digest(
            _query_claim_type("project.work_item.status")
        ).tagged,
    }
    for pin in raw["pins"]:
        pin["artifact_digest"] = digests[pin["target"]["name"]]
    definition = QueryDefinitionV1.model_validate(raw)
    fixture["query_definition"] = definition.model_dump(mode="json")
    fixture["canonical_wire"] = render_query_definition(definition).decode("utf-8")
    fixture["artifact_digest"] = query_definition_digest(definition).tagged
    _write(path, fixture)


def _update_subject_golden() -> None:
    path = GOLDENS / "subject-v1.json"
    fixture = _read(path)
    raw = fixture.get("wire")
    if not isinstance(raw, dict):
        raise TypeError("Subject golden has no wire object")
    raw.pop("authority", None)
    subject = SubjectShell.model_validate(raw)
    fixture["wire"] = subject.model_dump(mode="json")
    fixture["artifact_digest"] = subject_digest(subject).tagged
    render_subject(subject)
    _write(path, fixture)


def _update_semantic_genesis_golden() -> None:
    path = GOLDENS / "semantic-genesis-v1.json"
    fixture = _read(path)
    input_payload = fixture.get("input")
    if not isinstance(input_payload, dict):
        raise TypeError("semantic genesis golden has no input object")
    principals = (
        PrincipalRecord(principal_id="daemon", public_key="01" * 32, kind="daemon"),
        PrincipalRecord(principal_id="owner", public_key="02" * 32, kind="ordinary"),
        PrincipalRecord(principal_id="reviewer", public_key="03" * 32, kind="ordinary"),
    )
    approval_policy = ApprovalPolicyV1(mode="self_approval_allowed")
    tree = genesis_tree(principals, approval_policy=approval_policy)
    parent = bootstrap_root(
        instance_id=str(input_payload["instance_id"]),
        daemon_public_key=str(input_payload["daemon_public_key"]),
    )
    changeset, semantic = genesis_semantic_root(tree, parent=parent)
    input_payload["principals"] = [item.model_dump(mode="json") for item in principals]
    input_payload["approval_policy"] = approval_policy.model_dump(mode="json")
    fixture["expected"] = {
        "canonical_tree": {
            member_path: content.decode("utf-8") for member_path, content in tree.items()
        },
        "bootstrap_root": parent.tagged,
        "manifest_root": manifest_root(tree).tagged,
        "changeset_digest": changeset.tagged,
        "semantic_root": semantic.tagged,
    }
    _write(path, fixture)


def _update_changeset_golden() -> None:
    path = GOLDENS / "changeset-v3.json"
    fixture = _read(path)
    raw = fixture.get("record")
    if not isinstance(raw, dict):
        raise TypeError("ChangeSet golden has no record object")
    raw["approval_requirements"] = []
    candidate = SemanticCandidateV2.model_validate(raw["candidate"])
    raw["candidate_digest"] = candidate_digest(candidate).tagged
    law_evidence = tuple(MemberLawEvaluationV2.model_validate(item) for item in raw["law_evidence"])
    for member, evidence in zip(raw["members"], law_evidence, strict=True):
        member["law_evidence_digest"] = member_law_evidence_digest(evidence)
    members = tuple(CandidateMemberLawEvidenceV2.model_validate(item) for item in raw["members"])
    refs = tuple(item for member in members for item in member.dependency_proof_refs)
    raw["closure_proof"]["dependency_edge_root"] = build_dependency_edge_tree(refs).root.tagged
    raw["closure_proof"]["member_evidence_digest"] = candidate_member_evidence_digest(members)
    digest_payload = dict(raw)
    digest_payload.pop("tag")
    digest_payload.pop("changeset_digest")
    raw["changeset_digest"] = typed_digest(
        ChangeSetDigest,
        "playbill-changeset-v3",
        digest_payload,
    ).tagged
    record = ChangeSetRecordV3.model_validate(raw)
    canonical = render_change_set(record)
    reparsed = parse_change_set_record(canonical, path="changesets/golden.json")
    if reparsed != record:
        raise AssertionError("rebuilt v3 ChangeSet golden did not round-trip")
    fixture["canonical_bytes"] = canonical.decode("utf-8")
    fixture["changeset_digest"] = record.changeset_digest
    fixture["recomputed_changeset_digest"] = change_set_digest(record).tagged
    fixture["record"] = record.model_dump(mode="json")
    _write(path, fixture)


def _update_candidate_v2_golden() -> None:
    path = GOLDENS / "candidate-v2.json"
    if not path.exists():
        return
    fixture = _read(path)
    candidate = SemanticCandidateV2.model_validate(fixture["candidate"])
    payload = candidate.model_dump(mode="json")
    payload.pop("tag")
    fixture["canonical_preimage"] = canonical_bytes(
        {"tag": "playbill-candidate-v2", **payload}
    ).decode()
    fixture["candidate_digest"] = candidate_digest(candidate).tagged
    sibling = fixture["flat_rooted_v1_sibling"]
    if not isinstance(sibling, dict):
        raise TypeError("candidate-v2 golden has no flat-rooted sibling")
    sibling_candidate = SemanticCandidate.model_validate(sibling["candidate"])
    sibling["candidate_digest"] = candidate_digest(sibling_candidate).tagged
    _write(path, fixture)


def _update_merkle_manifest_golden() -> None:
    path = GOLDENS / "merkle-manifest-v1.json"
    if not path.exists():
        return
    fixture = _read(path)
    inputs = fixture["input"]
    members = inputs["members"]
    manifest = build_merkle_manifest(members)
    fixture["expected"] = {
        "empty_root": EMPTY_MERKLE_ROOT.tagged,
        "root": manifest.root.tagged,
        "root_preimage": canonical_bytes(
            {"tag": MERKLE_ROOT_DOMAIN, "node": manifest.node(ROOT_PREFIX).digest.value}
        ).decode(),
        "leaf_preimage": canonical_bytes(
            {
                "tag": MERKLE_LEAF_DOMAIN,
                "member_digest": members["documents/playbill-design.json"],
                "path": "documents/playbill-design.json",
            }
        ).decode(),
        "nodes": {prefix: node.digest.tagged for prefix, node in manifest.nodes.items()},
    }
    _write(path, fixture)


def _update_dependency_edge_golden() -> None:
    path = GOLDENS / "depgraph-v3.json"
    if not path.exists():
        return
    fixture = _read(path)
    inputs = fixture["input"]
    parent_edges = tuple(
        DependencyProofReferenceV1.model_validate(item) for item in inputs["parent_edges"]
    )
    edges = tuple(DependencyProofReferenceV1.model_validate(item) for item in inputs["edges"])
    members = dependency_edge_members(edges)
    tree = build_dependency_edge_tree(edges)
    fixture["expected"] = {
        "edge_set_preimage": canonical_bytes(
            {
                "tag": "playbill-depgraph-edges-v1",
                "edges": [
                    item.model_dump(mode="json")
                    for item in sorted(
                        (item for item in edges if item.source_path == "claims/alpha.json"),
                        key=lambda item: canonical_bytes(item.model_dump(mode="json")),
                    )
                ],
            }
        ).decode(),
        "empty_root": EMPTY_DEPENDENCY_EDGE_ROOT.tagged,
        "leaf_preimage": canonical_bytes(
            {
                "tag": DEPGRAPH_LEAF_DOMAIN,
                "member_digest": members["claims/alpha.json"],
                "path": "claims/alpha.json",
            }
        ).decode(),
        "members": members,
        "nodes": {prefix: node.digest.tagged for prefix, node in tree.nodes.items()},
        "parent_root": build_dependency_edge_tree(parent_edges).root.tagged,
        "root": tree.root.tagged,
        "root_preimage": canonical_bytes(
            {"tag": DEPGRAPH_ROOT_DOMAIN, "node": tree.node(ROOT_PREFIX).digest.value}
        ).decode(),
    }
    _write(path, fixture)


def _update_seed_bundle() -> None:
    type_path = SEED / "claim-types/project.work_item.status.json"
    type_payload = _remove_retired_claim_type_fields(_read(type_path))
    source_ids: list[str] = []
    for name in ("wi-101-status.json", "wi-102-status.json", "wi-103-status.json"):
        source = _read(SEED / "claims" / name).get("source")
        if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
            raise TypeError("seed ClaimInput has no working source_id")
        source_ids.append(source["source_id"])
    evidence_policy = type_payload.get("evidence_admission_policy")
    if not isinstance(evidence_policy, dict) or not isinstance(evidence_policy.get("rules"), list):
        raise TypeError("seed ClaimType has no evidence admission rules")
    for rule in evidence_policy["rules"]:
        if rule["rule_id"] == "direct-self-asserted":
            rule["capture_contract_digests"] = [
                capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
            ]
        elif rule["rule_id"] == "foreign-working-sources":
            rule["capture_contract_digests"] = sorted(
                capture_contract_digest(foreign_source_capture_contract(source_id)).tagged
                for source_id in source_ids
            )
    claim_type = ClaimType.model_validate(type_payload)
    type_digest = claim_type_digest(claim_type).tagged
    _write(type_path, claim_type.model_dump(mode="json"))

    for name in ("wi-101-status.json", "wi-102-status.json", "wi-103-status.json"):
        path = SEED / "claims" / name
        payload = _read(path)
        claim_input = ClaimInput.model_validate(payload)
        if claim_input.predicate != claim_type.predicate:
            raise AssertionError("seed ClaimInput names a different ClaimType predicate")
        _write(path, claim_input.model_dump(mode="json", exclude_defaults=True))

    query_path = SEED / "query-definitions/project.work_items.json"
    query_payload = _read(query_path)
    query_payload.pop("authority", None)
    pins = query_payload.get("pins")
    if not isinstance(pins, list):
        raise TypeError("seed QueryDefinition has no pins")
    for pin in pins:
        if pin["target"]["name"] == claim_type.identity.name:
            pin["artifact_digest"] = type_digest
    QueryDefinitionV1.model_validate(query_payload)
    _write(query_path, query_payload)

    for name in ("wi-101.json", "wi-102.json", "wi-103.json"):
        path = SEED / "subjects" / name
        payload = _read(path)
        payload.pop("authority", None)
        subject = SubjectShell.model_validate(payload)
        _write(path, subject.model_dump(mode="json"))

    procedure_path = SEED / "procedures/project.work_item.digest.json"
    procedure_payload = _read(procedure_path)
    procedure_payload.pop("authority", None)
    ProcedureInput.model_validate(procedure_payload)
    _write(procedure_path, procedure_payload)


def main() -> None:
    _update_claim_type_golden()
    _update_query_golden()
    _update_subject_golden()
    _update_semantic_genesis_golden()
    _update_candidate_v2_golden()
    _update_merkle_manifest_golden()
    _update_dependency_edge_golden()
    _update_changeset_golden()
    _update_seed_bundle()


if __name__ == "__main__":
    main()
