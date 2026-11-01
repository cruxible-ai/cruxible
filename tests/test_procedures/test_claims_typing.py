"""The value type, key parts and authority of a mapping are fixed at acceptance."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import lower_procedure_spec
from cruxible_core.procedures.claims_typing import ClaimsTypingRefused, validate_claims_templates
from tests.core_support._subject_kind_support import keyed_subject_kind
from tests.support.procedures import procedure_definition
from tests.test_procedures.test_claims_terminal import KIND, PREDICATE, ROW, claim_type, claims_spec


def check(spec: Any, ct: Any = None) -> None:
    definition = procedure_definition(**lower_procedure_spec(spec).definition)
    definitions = {
        f"SubjectKind:{KIND}": keyed_subject_kind(KIND, (KIND + ".cve_id",)),
        f"ClaimType:{KIND}.cve_id": claim_type(KIND + ".cve_id", "string"),
        f"ClaimType:{PREDICATE}": ct or claim_type(),
        ROW.identity.qualified: ROW,
    }
    validate_claims_templates(definition, lambda identity: definitions.get(identity.qualified))


def test_claims_accepts_a_typed_extracted_field() -> None:
    check(claims_spec())


def test_claims_refuses_a_value_edge_that_is_not_assignable() -> None:
    with pytest.raises(ClaimsTypingRefused, match="not assignable"):
        check(claims_spec(), claim_type(value="bool"))


def test_claims_refuses_a_literal_outside_the_value_type() -> None:
    with pytest.raises(ClaimsTypingRefused, match="literal does not fit"):
        check(claims_spec(mappings=[{"type": PREDICATE, "value": "Maybe"}]))


def test_claims_refuses_a_role_or_replaces_override_outside_the_policy() -> None:
    with pytest.raises(ValueError, match="claims_replaces_forbidden"):
        check(
            claims_spec(mappings=[{"type": PREDICATE, "value": "open", "replaces": ["normative"]}])
        )
    with pytest.raises(ClaimsTypingRefused, match="claims_role_forbidden"):
        check(
            claims_spec(
                mappings=[{"type": PREDICATE, "value": "open", "role": "environment_binding"}]
            )
        )


def test_claims_refuses_missing_or_extra_subject_key_parts() -> None:
    data = claims_spec().model_dump(mode="python", by_alias=True)
    data["steps"][-1]["subject"]["key"] = {"wrong": "CVE-2026-0001"}
    from tests.support.procedures import procedure_spec

    with pytest.raises(ClaimsTypingRefused, match="key part"):
        check(procedure_spec(base=data))
