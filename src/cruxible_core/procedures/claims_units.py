"""Adapt resolved template objects to the single Claim unit law."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

from pydantic import TypeAdapter

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
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
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.errors import CoreError

_OBJECT: TypeAdapter[ClaimObject] = TypeAdapter(ClaimObject)
# The statement's own field type: the same lax datetime parse, then UTC.
_PERIOD: TypeAdapter[datetime | None] = TypeAdapter(datetime | None)


class ClaimUnitUnresolved(CoreError):
    pass


def unit_key(obj: Any, claim_type: Any, context: Any) -> bytes:
    return canonical_bytes(_unit_value(obj, claim_type, context))


def _unit_value(obj: Any, claim_type: Any, context: Any) -> Any:
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
    return key.model_dump(mode="json")


#: Domains of the per-item run record (D-R37 g1): a stable item identity and the
#: digest of the one canonical change the item made.
CLAIMS_ITEM_IDENTITY_DOMAIN = "playbill-claims-item-identity-v1"
CLAIMS_ITEM_CHANGE_DOMAIN = "playbill-claims-item-change-v1"


def claims_item_identity_digest(
    *,
    node_id: str,
    subject: object,
    claim_type: Any | None = None,
    qualifier: object = None,
    obj: Any = None,
    context: Any = None,
) -> str:
    """One claims-terminal item's identity, stable across runs: {node, ClaimType, Subject, unit}.

    ``child_index`` and the item's value are left out, so the same Subject and
    unit keep one identity when other items come and go or its value changes.
    The unit is the slot (``qualifier`` with it) plus the D-R30 unit within it:
    the whole slot for a one-valued ClaimType, the object's unit key for a
    many-valued one. Its every read was already made, and cached, by the
    eligibility law that produced the change. A Subject item has no ClaimType
    and no unit. ``subject`` is the Subject's address, or its key reference
    while the Subject does not exist yet.
    """

    def wire(value: Any) -> Any:
        return value.model_dump(mode="json") if hasattr(value, "model_dump") else value

    unit: object = None
    if claim_type is not None:
        if claim_type.cardinality != "many":
            unit = {"kind": "slot", "value": None}
        elif claim_type.inverse is None:
            # claim_unit_key's object unit, built without a model: the object in
            # its canonical form, as the eligibility law compares it (same_value).
            unit = {"kind": "object", "value": wire(obj)}
        else:
            raw = wire(obj)
            try:
                unit = _unit_value(raw, claim_type, context)
            except ClaimUnitUnresolved:
                unit = {"kind": "object", "value": raw}
    return typed_digest(
        Sha256Value,
        CLAIMS_ITEM_IDENTITY_DOMAIN,
        {
            "node_id": node_id,
            "claim_type": None if claim_type is None else claim_type.predicate,
            "subject": wire(subject),
            "qualifier": qualifier,
            "unit": unit,
        },
    ).tagged


def claims_item_change_digest(change: Any) -> str:
    """The digest of one item's canonical change; the change body is never stored (g2)."""

    return typed_digest(
        Sha256Value, CLAIMS_ITEM_CHANGE_DOMAIN, change.model_dump(mode="json")
    ).tagged


def claim_time(value: object) -> datetime | None:
    """One resolved effective time, typed as a Claim statement types it (UTC).

    Raises ``pydantic.ValidationError`` for a value that is not a datetime.
    """

    parsed = _PERIOD.validate_python(value)
    return None if parsed is None else ensure_utc(parsed)


def unit_holders(
    held: Sequence[ClaimArtifact],
    obj: Mapping[str, Any],
    claim_type: Any,
    context: Any,
) -> tuple[ClaimArtifact, ...]:
    """The held Claims of a slot that speak about ``obj``'s unit (D-R30).

    A one-valued slot is one unit: every held Claim competes. A many-valued
    slot's unit is the object (its unit key), so only the held Claims of the
    same unit compete; another unit is a separate value. Raises
    ``ClaimUnitUnresolved`` when an inverse's object key cannot be read.
    """

    if claim_type.inverse is None:
        if claim_type.cardinality != "many":
            return tuple(held)
        # Without an inverse a unit is the object itself (claim_unit_key's object
        # unit), so canonical objects are compared directly and no unit is built.
        return tuple(c for c in held if same_value(c.statement, obj))
    # Under an inverse every unit must resolve, a one-valued slot's included,
    # so a later absence can judge what this write holds. Each unit is derived
    # once per call: the incoming one and one per holder.
    unit = unit_key(dict(obj), claim_type, context)
    units = [unit_key(c.statement.object, claim_type, context) for c in held]
    if claim_type.cardinality != "many":
        return tuple(held)
    return tuple(c for c, u in zip(held, units, strict=True) if u == unit)


def same_value(held: ClaimStatement, obj: Mapping[str, Any]) -> bool:
    """The held object equals the incoming one in canonical object form.

    Both sides come from the one canonicalizer (a literal's canonical value, a
    Subject's address), so this compares values, not their source spelling.
    """

    return held.object.model_dump(mode="json") == obj


def same_content(
    held: ClaimStatement,
    *,
    obj: Mapping[str, Any],
    role: str,
    period: tuple[datetime | None, datetime | None],
) -> bool:
    """Same value, role and typed effective period (``claim_time``), never wire text."""

    return (
        held.role == role
        and (held.effective_from, held.effective_until) == period
        and same_value(held, obj)
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
