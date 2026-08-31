"""Test-only ClaimType input factories."""

from __future__ import annotations

from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1


def claim_type_input_example() -> ClaimTypeInputV1:
    return ClaimTypeInputV1(
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


def defaulted_claim_type_input_example() -> ClaimTypeInputV1:
    example = claim_type_input_example()
    source_id = "repo.replace-me"
    contract_digests = sorted(
        {
            capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged,
            capture_contract_digest(foreign_source_capture_contract(source_id)).tagged,
        }
    )
    return example.model_copy(
        update={
            "predicate": "project.work_item.status",
            "anticipated_source_ids": (source_id,),
            "evidence_admission_policy": {
                "rules": [
                    {
                        "rule_id": f"source-{source_id}",
                        "claim_roles": sorted(example.permitted_roles),
                        "capture_contract_digests": contract_digests,
                        "evidence_kinds": ["self_asserted"],
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
