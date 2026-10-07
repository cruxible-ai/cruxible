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
