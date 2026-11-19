"""Core-declared native views. Claim values retain their ClaimType's exact type."""

from __future__ import annotations

import hashlib

from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
from cruxible_client.contracts.shapes import (
    BoolType,
    Contract,
    ContractField,
    ListType,
    StringType,
    SubjectType,
)


def claim_view_contract(shape: ClaimTypeStructure) -> Contract:
    text = ContractField(type=StringType())
    return Contract(
        name="cruxible.claim-view." + hashlib.sha256(shape.predicate.encode("utf-8")).hexdigest(),
        version=1,
        fields={
            "value": ContractField(type=shape.value, optional=True),
            "claim_id": text,
            "revision": text,
            "subject": ContractField(type=SubjectType(subject_kinds=shape.allowed_subject_kinds)),
            "predicate": text,
            "qualifier": ContractField(type=StringType(), optional=True),
            "role": text,
            "object_kind": text,
            "lifecycle_state": text,
            "captures": ContractField(
                type=ListType(item=StringType(pattern=r"sha256:[0-9a-f]{64}"), max_items=1000000)
            ),
            "verdict": text,
            "currency": text,
            "identity": text,
            "artifact_digest": text,
            "statement_digest": text,
            "path": text,
        },
    )


def query_view_fields() -> dict[str, ContractField | None]:
    # Rows are supplied by the Query's governed row Contract. Metadata is a
    # separately typed API model; it is not an untyped Contract field.
    return {
        "rows": None,
        "coordinate": None,
        "receipt": None,
        "result": None,
        "definition_path": ContractField(type=StringType()),
        "definition_digest": ContractField(type=StringType()),
        **{
            name: ContractField(type=BoolType())
            for name in ("completed", "truncated", "has_conflicts")
        },
    }
