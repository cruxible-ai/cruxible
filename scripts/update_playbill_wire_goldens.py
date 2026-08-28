"""Rebuild the nine Playbill fixtures authorized by the PC-DEL2 wire cut."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactAuthority, ArtifactIdentity
from cruxible_client.contracts.canonical import ChangeSetDigest, typed_digest
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    claim_artifact_digest,
    claim_statement_digest,
    render_claim,
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
from cruxible_core.playbill.settlement import (
    ChangeSetRecordV3,
    change_set_digest,
    parse_change_set_record,
    render_change_set,
)
from cruxible_core.service.playbill_claims import DirectClaimAuthoringV1

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
    resolution.pop("authority_rule_digest", None)
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
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
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
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
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


def _update_capture_claim_golden() -> None:
    path = GOLDENS / "capture-claim-v1.json"
    fixture = _read(path)
    raw_wire = fixture.get("claim_wire")
    if not isinstance(raw_wire, str):
        raise TypeError("capture Claim golden has no Claim wire")
    raw = json.loads(raw_wire)
    claim_type = _direct_claim_type()
    type_digest = claim_type_digest(claim_type).tagged
    raw["statement"]["claim_type_digest"] = type_digest
    for pin in raw["pins"]:
        if pin["role"] == "claim-type":
            pin["artifact_digest"] = type_digest
    claim = ClaimArtifact.model_validate(raw)
    fixture["claim_wire"] = render_claim(claim).decode("utf-8")
    fixture["statement_digest"] = claim_statement_digest(claim.statement).tagged
    fixture["artifact_digest"] = claim_artifact_digest(claim).tagged
    _write(path, fixture)


def _update_query_golden() -> None:
    path = GOLDENS / "query-definition-v1.json"
    fixture = _read(path)
    raw = fixture.get("query_definition")
    if not isinstance(raw, dict):
        raise TypeError("QueryDefinition golden has no query_definition object")
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


def _update_changeset_golden() -> None:
    path = GOLDENS / "changeset-v3.json"
    fixture = _read(path)
    raw = fixture.get("record")
    if not isinstance(raw, dict):
        raise TypeError("ChangeSet golden has no record object")
    raw["approval_requirements"] = []
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


def _update_seed_bundle() -> None:
    type_path = SEED / "claim-types/project.work_item.status.json"
    type_payload = _remove_retired_claim_type_fields(_read(type_path))
    claim_type = ClaimType.model_validate(type_payload)
    type_digest = claim_type_digest(claim_type).tagged
    _write(type_path, claim_type.model_dump(mode="json"))

    for name in ("wi-101-status.json", "wi-102-status.json", "wi-103-status.json"):
        path = SEED / "claims" / name
        payload = _read(path)
        statement = payload.get("statement")
        if not isinstance(statement, dict):
            raise TypeError("seed Claim has no statement")
        statement["claim_type_digest"] = type_digest
        nested = payload.get("claim_type_artifact")
        if isinstance(nested, dict):
            nested_type = ClaimType.model_validate(_remove_retired_claim_type_fields(nested))
            if nested_type != claim_type:
                raise AssertionError("seed Claim carries a different ClaimType")
            payload["claim_type_artifact"] = nested_type.model_dump(mode="json")
        DirectClaimAuthoringV1.model_validate(payload)
        _write(path, payload)

    query_path = SEED / "query-definitions/project.work_items.json"
    query_payload = _read(query_path)
    pins = query_payload.get("pins")
    if not isinstance(pins, list):
        raise TypeError("seed QueryDefinition has no pins")
    for pin in pins:
        if pin["target"]["name"] == claim_type.identity.name:
            pin["artifact_digest"] = type_digest
    QueryDefinitionV1.model_validate(query_payload)
    _write(query_path, query_payload)


def main() -> None:
    _update_claim_type_golden()
    _update_capture_claim_golden()
    _update_query_golden()
    _update_changeset_golden()
    _update_seed_bundle()


if __name__ == "__main__":
    main()
