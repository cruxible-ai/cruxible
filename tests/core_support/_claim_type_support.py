"""Test-only ClaimType input factories."""

from __future__ import annotations

from cruxible_client.contracts.captures import (
    foreign_source_capture_contract,
)
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord


def claim_type_input_example() -> ClaimTypeInputRecord:
    return ClaimTypeInputRecord(
        predicate="project.work_item.replace_me",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy={"rules": []},
        admission_policy={
            "corroboration_requirements": [],
            "freeze_requirements": [],
        },
        resolution_policy={
            "cardinality": "one",
            "eligible_verdicts": ["supported"],
            "required_basis_kinds": [],
            "require_current": True,
            "selector": "only_contender",
            "conflict_result": "unresolved",
        },
    )


def defaulted_claim_type_input_example() -> ClaimTypeInputRecord:
    example = claim_type_input_example()
    source_id = "repo.replace-me"
    # Rules name contracts by identity: the foreign-source contract Flow-A binding
    # carries for this source.
    contract = foreign_source_capture_contract(source_id).identity.qualified
    return example.model_copy(
        update={
            "predicate": "project.work_item.status",
            "anticipated_source_ids": (source_id,),
            "evidence_admission_policy": {
                "rules": [
                    {
                        "rule_id": f"source-{source_id}",
                        "claim_roles": sorted(example.permitted_roles),
                        "capture_contracts": [contract],
                        "admission": "direct",
                        "subject_binding": "exact_claim_subject",
                    }
                ]
            },
        }
    )


__all__ = [
    "claim_type_input_example",
    "defaulted_claim_type_input_example",
]


def claim_type_input_from_artifact(claim_type, *, source_ids=()):
    """Translate historical test fixtures into fresh, identity-based authoring input."""
    from cruxible_client.contracts.captures import (
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACTS,
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        capture_contract_digest,
    )

    value = claim_type.model_dump(
        mode="json",
        exclude={"artifact_format", "identity", "lifecycle", "subject_scope", "slot_policy"},
    )
    for name in ("evidence_requirement", "revision_evidence", "canonical_form"):
        if value.get(name) is None:
            value.pop(name, None)
    contracts = [DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT, *COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACTS]
    contracts.extend(foreign_source_capture_contract(source) for source in source_ids)
    identities = {
        capture_contract_digest(item).tagged: item.identity.qualified for item in contracts
    }
    # The historical direct-value profile is read-only; fresh own-words fixtures
    # use the coordinator's retained-body profile instead.
    identities[capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged] = (
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.qualified
    )
    for rule in value["evidence_admission_policy"]["rules"]:
        rule.pop("evidence_kinds", None)
        rule.pop("tag", None)
        if rule.pop("allowed_reducer_digests", ()):
            raise ValueError("legacy reducer allowances need an explicit current evidence policy")
        if "capture_contract_digests" in rule:
            rule["capture_contracts"] = [
                identities[digest] for digest in rule.pop("capture_contract_digests")
            ]
    return ClaimTypeInputRecord.model_validate(value).model_dump(mode="json")
