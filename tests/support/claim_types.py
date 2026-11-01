"""Build ClaimTypes without coupling fixtures to their stored value representation.

``value_type`` is a literal JSON schema, a tuple of allowed Subject kinds, or
``"exact_content"``. Keep that translation here when the ClaimType wire changes.
The defaults describe an ordinary string field with no evidence requirements;
fixtures declare only the vocabulary and policies their tests exercise.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeAlias

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimResolutionPolicy,
)
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord

ClaimValueType: TypeAlias = Mapping[str, object] | tuple[str, ...] | Literal["exact_content"]


def claim_type_changes(*, value_type: ClaimValueType, **overrides: Any) -> dict[str, Any]:
    """Changes for ``model_copy`` or raw authoring data, without adding fixture defaults.

    This deliberately does not validate: tests also copy artifacts to exercise
    admission refusals, just as they do with ``model_copy(update=...)``.
    """
    if isinstance(value_type, Mapping):
        fields: dict[str, Any] = {
            "object_kind": "literal",
            "literal_schema": dict(value_type),
            "allowed_object_subject_kinds": (),
        }
    elif isinstance(value_type, tuple):
        fields = {
            "object_kind": "subject",
            "literal_schema": None,
            "allowed_object_subject_kinds": value_type,
        }
    elif value_type == "exact_content":
        fields = {
            "object_kind": "exact_content",
            "literal_schema": None,
            "allowed_object_subject_kinds": (),
        }
    else:
        raise TypeError(f"Unknown test ClaimType value type: {value_type!r}")
    return {**fields, **overrides}


def claim_type(
    predicate: str = "project.work_item.status",
    *,
    value_type: ClaimValueType | None = None,
    **overrides: Any,
) -> ClaimType:
    """A validated artifact; model defaults supply optional fields.

    Identity and Subject kind follow the predicate unless explicitly overridden.
    Cardinality also selects the default resolution policy.
    """
    cardinality = overrides.get("cardinality", "one")
    fields = {
        "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
        "predicate": predicate,
        "allowed_subject_kinds": (predicate.rsplit(".", 1)[0],),
        "cardinality": cardinality,
        "permitted_roles": ("normative", "observation"),
        "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(),
        "admission_policy": ClaimAdmissionPolicy(),
        "resolution_policy": ClaimResolutionPolicy(
            cardinality=cardinality,
            eligible_verdicts=("supported",),
            selector="all" if cardinality == "many" else "only_contender",
        ),
    }
    return ClaimType.model_validate(
        {
            **fields,
            **claim_type_changes(
                value_type={"type": "string"} if value_type is None else value_type, **overrides
            ),
        }
    )


def claim_type_input(
    predicate: str = "project.work_item.status",
    *,
    value_type: ClaimValueType | None = None,
    **overrides: Any,
) -> ClaimTypeInputRecord:
    """A fresh authoring definition, preserving the input model's own defaults.

    Partial-edit and default-inference tests should construct their raw input
    directly, since omission is the behavior those tests exercise.
    """
    fields = claim_type_changes(
        value_type={"type": "string"} if value_type is None else value_type, **overrides
    )
    # Authoring inputs serialize only stated fields. Literal fixtures do not
    # state an empty Subject range merely because artifacts default to one.
    if (isinstance(value_type, Mapping) or value_type is None) and (
        "allowed_object_subject_kinds" not in overrides
    ):
        fields.pop("allowed_object_subject_kinds", None)
    return ClaimTypeInputRecord.model_validate(
        {
            "predicate": predicate,
            "cardinality": "one",
            "permitted_roles": ("normative", "observation"),
            **fields,
        }
    )
