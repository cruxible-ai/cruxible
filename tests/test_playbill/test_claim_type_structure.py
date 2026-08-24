"""PC-A1 policy-free ClaimType structural validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.claim_type_structure import (
    ClaimTypeStructure,
    check_claim_type_structure,
)
from cruxible_client.contracts.laws import PLAYBILL_ACCEPTANCE_LAWS


def _literal() -> dict[str, object]:
    return {
        "predicate": "project.work_item.status",
        "allowed_subject_kinds": ["project.work_item"],
        "object_kind": "literal",
        "literal_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "enum": ["blocked", "done", "ready"],
            "type": "string",
        },
        "allowed_object_subject_kinds": [],
        "cardinality": "one",
        "permitted_roles": ["normative", "observation"],
        "referent_sensitivity": "identity",
    }


def test_claim_type_structure_validates_exact_literal_schema_without_digesting_artifact() -> None:
    structure = ClaimTypeStructure.model_validate(_literal())
    assert structure.literal_schema_bytes() == (
        b'{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        b'"enum":["blocked","done","ready"],"type":"string"}'
    )
    assert not hasattr(structure, "artifact_format")
    assert not hasattr(structure, "artifact_digest")


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"literal_schema": None}, "literal_schema"),
        ({"object_kind": "subject"}, "cannot carry literal_schema"),
        (
            {
                "object_kind": "subject",
                "literal_schema": None,
                "allowed_object_subject_kinds": [],
            },
            "allowed object Subject kinds",
        ),
        ({"allowed_subject_kinds": []}, "allowed subject kind"),
        ({"permitted_roles": []}, "roles"),
        ({"referent_sensitivity": "bytes"}, "referent_sensitivity"),
    ],
)
def test_claim_type_structure_refuses_mismatched_kinds_schemas_and_roles(
    updates: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ClaimTypeStructure.model_validate({**_literal(), **updates})


def test_claim_type_structural_diagnostics_are_explicitly_local_only() -> None:
    valid = check_claim_type_structure(_literal())
    assert valid.status == "valid"
    assert valid.coverage == "local_only"
    assert valid.structure is not None

    invalid = check_claim_type_structure({**_literal(), "literal_schema": None})
    assert invalid.status == "invalid"
    assert invalid.coverage == "local_only"
    assert [item.code for item in invalid.diagnostics] == ["playbill.claim_type.structure_invalid"]
    assert all(item.subject is None for item in invalid.diagnostics)


def test_claim_type_artifact_format_activates_only_with_pc_a2_policy_wire() -> None:
    law = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v1")
    assert law.artifact_kind == "claim-type"
    assert law.coordinate.identifier == "playbill.claim-type.v1"
