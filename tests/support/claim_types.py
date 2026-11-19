"""Build ClaimTypes without coupling fixtures to their stored value representation.

``value_type`` is a literal JSON schema, a tuple of allowed Subject kinds, or
the retired ``"exact_content"`` sentinel, which refuses with a Documents repair.
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
    from cruxible_client.contracts.shapes import coerce_field_type

    # Older fixture helpers express the object category separately. Normalize it
    # here only: product input accepts the native value declaration exclusively.
    category = overrides.pop("object_kind", None)
    schema_override = overrides.pop("literal_schema", None)
    ranges = overrides.pop("allowed_object_subject_kinds", None)
    if category == "subject" or ranges:
        value_type = tuple(ranges or ("semantic.subject",))
    elif schema_override is not None:
        value_type = schema_override

    if isinstance(value_type, Mapping):
        schema = dict(value_type)
        if isinstance(schema.get("const"), str):
            native = {"kind": "enum", "members": (schema["const"],)}
        elif "kind" in schema:
            native = schema
        elif isinstance(schema.get("enum"), list) and all(
            isinstance(member, str) for member in schema["enum"]
        ):
            native = {"kind": "enum", "members": schema["enum"]}
        else:
            kind = {"integer": "int", "boolean": "bool", "number": "number"}.get(
                schema.get("type"), schema.get("type", "string")
            )
            if kind == "string" and schema.get("format") in {"date", "date-time"}:
                kind = "date" if schema["format"] == "date" else "datetime"
            names = {
                "minLength": "min_len",
                "maxLength": "max_len",
                "minimum": "min",
                "maximum": "max",
                "pattern": "pattern",
            }
            native = {
                "kind": kind,
                **{target: schema[source] for source, target in names.items() if source in schema},
            }
            if kind == "string":
                native.setdefault("max_len", 256)
    elif isinstance(value_type, tuple):
        native = {"kind": "subject", "subject_kinds": value_type}
    elif value_type == "exact_content":
        raise ValueError(
            "cruxible.claim.exact_content_retired: prose lives in Documents; "
            "cite a Document section"
        )
    else:
        native = value_type
    from cruxible_client.contracts.shapes import StringType

    selected = coerce_field_type(overrides.pop("value", native))
    if isinstance(selected, StringType) and selected.max_len is None:
        selected = selected.model_copy(update={"max_len": 256})
    return {"value": selected, **overrides}


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
    return ClaimTypeInputRecord.model_validate(
        {
            "predicate": predicate,
            "cardinality": "one",
            "permitted_roles": ("normative", "observation"),
            **fields,
        }
    )
