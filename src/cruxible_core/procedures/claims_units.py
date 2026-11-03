"""Adapt resolved template objects to the single Claim unit law."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from pydantic import TypeAdapter

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.canonical_forms import canonicalize_literal
from cruxible_client.contracts.claim_types import effective_canonical_form
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimObject,
    ClaimStatement,
    claim_unit_key,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.shapes import (
    FieldInvalid,
    SubjectType,
    check_value,
    claim_has_line_break,
)
from cruxible_core.errors import CoreError

if TYPE_CHECKING:
    from cruxible_client.contracts.authoring.models import AuthoringClaimStatement

_OBJECT: TypeAdapter[ClaimObject] = TypeAdapter(ClaimObject)


class ClaimUnitUnresolved(CoreError):
    pass


def unit_key(obj: Any, claim_type: Any, context: Any) -> bytes:
    def object_key_value(address: SemanticAddress, predicate: str) -> object:
        held = context.slot(address, predicate, None)
        return (
            held[0].statement.object.value
            if len(held) == 1 and held[0].statement.object.kind == "literal"
            else None
        )

    value = _OBJECT.validate_python(obj) if isinstance(obj, dict) else obj
    key = claim_unit_key(
        cast(ClaimStatement, SimpleNamespace(object=value)),
        claim_type.inverse,
        object_key_value=object_key_value,
    )
    if key is None:
        raise ClaimUnitUnresolved(
            "The inverse object key is unresolved; repair the object's key Claim."
        )
    return canonical_bytes(key.model_dump(mode="json"))


def unit_holders(
    held: Sequence[ClaimArtifact],
    incoming: AuthoringClaimStatement,
    claim_type: Any,
    context: Any,
) -> tuple[ClaimArtifact, ...]:
    """The held Claims of a slot that speak about ``incoming``'s unit (D-R30).

    A one-valued slot is one unit: every held Claim competes. A many-valued
    slot's unit is the object (its unit key), so only the held Claims of the
    same unit compete; another unit is a separate value. Raises
    ``ClaimUnitUnresolved`` when an inverse's object key cannot be read.
    """

    if claim_type.cardinality != "many" and claim_type.inverse is None:
        return tuple(held)
    # Under an inverse every unit must resolve, a one-valued slot's included,
    # so a later absence can judge what this write holds.
    unit = unit_key(incoming.object.model_dump(mode="json"), claim_type, context)
    units = [unit_key(c.statement.object, claim_type, context) for c in held]
    if claim_type.cardinality != "many":
        return tuple(held)
    return tuple(c for c, u in zip(held, units, strict=True) if u == unit)


def same_value(held: ClaimStatement, incoming: AuthoringClaimStatement) -> bool:
    """The held object equals the incoming one, compared as typed Claim objects."""

    return held.object == _OBJECT.validate_python(incoming.object.model_dump(mode="json"))


def same_content(held: ClaimStatement, incoming: AuthoringClaimStatement) -> bool:
    """Same value, role and effective period, compared typed (never as wire text)."""

    return (
        held.role == incoming.role
        and held.effective_from == incoming.effective_from
        and held.effective_until == incoming.effective_until
        and same_value(held, incoming)
    )


def resolve_object(
    value: Any, claim_type: Any, context: Any, executor: Any, *, trusted: bool = False
) -> dict[str, Any] | None:
    """The same value resolution for positive writes and enumeration presence."""
    if claim_has_line_break(value):
        return None
    if isinstance(claim_type.value, SubjectType):
        try:
            if isinstance(value, dict) and value.get("kind") in claim_type.value.subject_kinds:
                address = context.subject(value["kind"], value.get("key", {}))
            elif isinstance(value, str):
                found = [context.reference(kind, value) for kind in claim_type.value.subject_kinds]
                found = [a for a in found if a is not None]
                address = found[0] if len(found) == 1 else None
            else:
                address = None
        except (ValueError, KeyError, TypeError):
            address = None
        return (
            None
            if address is None
            else {"kind": "subject", "address": address.model_dump(mode="json")}
        )
    checked = (
        None
        if trusted
        else check_value(
            value,
            claim_type.value,
            at=claim_type.predicate,
            resolve=executor.contract_resolver,
            enum_members=executor.enum_resolver,
            keys=executor.key_rules,
        )
    )
    if isinstance(checked, FieldInvalid):
        return None
    from cruxible_core.procedures.extract import wire_value

    literal = value if checked is None else wire_value(checked.value)
    return {
        "kind": "literal",
        "value": canonicalize_literal(literal, effective_canonical_form(claim_type)),
    }
