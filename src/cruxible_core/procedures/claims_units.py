"""Adapt resolved template objects to the single Claim unit law."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from cruxible_core.indexes.subject_identity import KeyTuple, key_encoding

_OBJECT: TypeAdapter[ClaimObject] = TypeAdapter(ClaimObject)
# The statement's own field type: the same lax datetime parse, then UTC.
_PERIOD: TypeAdapter[datetime | None] = TypeAdapter(datetime | None)


class ClaimUnitUnresolved(CoreError):
    pass


def unit_key(obj: Any, claim_type: Any, context: Any) -> bytes:
    return canonical_bytes(_unit_value(obj, claim_type, context))


def _unit_value(obj: Any, claim_type: Any, context: Any) -> Any:
    def object_key_value(address: SemanticAddress, predicate: str) -> object:
        reference = address.subject_key_reference
        if reference is not None:
            # A Subject this run creates (V1) is named by its canonical key: the
            # key part is the value its key Claim will hold once accepted.
            _kind, key = reference
            if not isinstance(key, dict):
                return None
            return next(
                (
                    value
                    for name, value in key.items()
                    if name == predicate or predicate.endswith("." + name)
                ),
                None,
            )
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
    and no unit. ``subject`` is the Subject's resolved whole-artifact address
    (D-R37 g fix): one canonical identity across the creating run, every later
    run and an absence retirement, whatever its ``expect`` spelling. For a
    Subject the run creates, that address is the one delivery's own lowering
    assigns it (``finalize_creation_identities``).
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


@dataclass(frozen=True)
class CreationIdentity:
    """A claims item naming a Subject the run creates: its identity waits for lowering.

    Until delivery's preparation lowers the terminal, a created Subject has no
    address, so the item is first identified by its key reference: the
    ``{subject_kind, key}`` Subject wire, or a ``subject-key-v1`` object for a
    value target the run creates (V1). ``finalize_creation_identities``
    replaces each with the whole-artifact address lowering assigned the
    terminal's Subject item creating that Subject.
    """

    #: The item's position in the terminal.
    index: int
    #: The position of the Subject item creating its Subject; None when it exists.
    subject_item: int | None
    #: ``claims_item_identity_digest``'s other arguments (ClaimType, unit, ...).
    parts: Mapping[str, Any]
    #: The position of the Subject item creating its object; None when it exists.
    object_item: int | None = None
    #: The resolved Subject address of an item whose Subject exists.
    subject: object = None


def _landed_object(obj: Any, address: object) -> Any:
    """``obj`` naming its created target by the address lowering assigned it."""

    target = {"kind": "subject", "address": address}
    if isinstance(obj, Mapping) and obj.get("kind") == "none":
        return {"kind": "none", "of": target}
    return target


def _unit_reads_object(claim_type: Any) -> bool:
    """Whether a Claim's identity unit is its object's address (not its key, not the slot).

    An ``object_key`` unit is the object's key value, already final in the
    creating run; every other unit is computed from the landed object.
    """

    inverse = getattr(claim_type, "inverse", None)
    return inverse is None or inverse.key.kind != "object_key"


def finalize_creation_identities(
    *,
    node_id: str,
    creations: Sequence[CreationIdentity],
    assigned_path: Callable[[int], str | None],
) -> dict[int, str]:
    """Each created Subject's items identified by the address lowering assigned it.

    One source of truth (D-R37 g fix 3): ``assigned_path`` reads, for a Subject
    item's position, the path delivery's preparation lowered it to -- shared
    lowering's whole-terminal assignment, over the admitted base overlaid
    with the terminal's own members (``members_identity_overlay``: a sibling's
    alias or key Claim can reserve or take a readable id). It is the path the
    Subject has once accepted, so the identity equals every later run's. A
    Subject item with no lowered path keeps its key-reference identity. One
    address per created Subject, not per item naming it.

    Returns item position -> finalized identity digest.
    """

    addresses: dict[int, object | None] = {}

    def address_of(item: int) -> object | None:
        if item not in addresses:
            path = assigned_path(item)
            addresses[item] = (
                None
                if path is None
                else SemanticAddress.whole_artifact(path).model_dump(mode="json")
            )
        return addresses[item]

    finalized: dict[int, str] = {}
    for creation in creations:
        subject = (
            creation.subject if creation.subject_item is None else address_of(creation.subject_item)
        )
        if subject is None:
            continue
        parts = dict(creation.parts)
        if creation.object_item is not None:
            target = address_of(creation.object_item)
            if target is None:
                continue
            if _unit_reads_object(parts.get("claim_type")):
                parts["obj"] = _landed_object(parts.get("obj"), target)
        finalized[creation.index] = claims_item_identity_digest(
            node_id=node_id, subject=subject, **parts
        )
    return finalized


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


@dataclass(frozen=True)
class ValueTarget:
    """A Subject a Subject-valued template names by key and creates when absent (V1).

    ``key`` is canonical (each part's ClaimType form, then its R12 normalizer),
    so two spellings of one key are one target and one Subject item (V5).
    """

    kind: str
    key: KeyTuple

    @property
    def slot(self) -> tuple[str, str]:
        """The (kind, canonical key) a run creates one Subject item for."""

        return self.kind, key_encoding(self.key)

    def key_mapping(self) -> dict[str, Any]:
        return {predicate: value for predicate, value in self.key}

    def address(self) -> SemanticAddress:
        return SemanticAddress.subject_key(self.kind, self.key_mapping())


def _present_key(key: object) -> object:
    """A key template's resolved parts: a present field result is its value.

    An absent or invalid part leaves the result tagged, so the key refuses.
    """

    if not isinstance(key, dict):
        return key
    return {
        name: part["value"]
        if isinstance(part, dict) and part.get("tag") == "present" and "value" in part
        else part
        for name, part in key.items()
    }


def resolve_object(
    value: Any, claim_type: Any, context: Any, executor: Any, *, trusted: bool = False
) -> dict[str, Any] | None:
    """The same value resolution for positive writes and enumeration presence."""

    return resolve_value(value, claim_type, context, executor, trusted=trusted)[0]


def resolve_value(
    value: Any, claim_type: Any, context: Any, executor: Any, *, trusted: bool = False
) -> tuple[dict[str, Any] | None, ValueTarget | None]:
    """The resolved object, and the Subject the run creates for it, if any (V1).

    A Subject-valued template naming a missing Subject by key with
    ``create: if_absent`` resolves to that key (a ``subject-key-v1`` object,
    which shared lowering lands on the Subject item creating it). A missing
    target without ``create``, an invalid key and an ambiguous one resolve to
    nothing: an ambiguous or invalid resolution never creates.
    """

    if claim_has_line_break(value):
        return None, None
    if isinstance(claim_type.value, SubjectType):
        target: ValueTarget | None = None
        try:
            if isinstance(value, dict) and value.get("kind") in claim_type.value.subject_kinds:
                kind, key = value["kind"], _present_key(value.get("key", {}))
                address = context.subject(kind, key)
                if address is None and value.get("create") == "if_absent":
                    target = ValueTarget(kind=kind, key=context.canonical(kind, key))
            elif isinstance(value, str):
                found = [context.reference(kind, value) for kind in claim_type.value.subject_kinds]
                found = [a for a in found if a is not None]
                address = found[0] if len(found) == 1 else None
            else:
                address = None
        except (ValueError, KeyError, TypeError):
            address, target = None, None
        if address is not None:
            return {"kind": "subject", "address": address.model_dump(mode="json")}, None
        if target is not None:
            return {"kind": "subject", "address": target.address().model_dump(mode="json")}, target
        return None, None
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
        return None, None
    from cruxible_core.procedures.extract import wire_value

    literal = value if checked is None else wire_value(checked.value)
    return {
        "kind": "literal",
        "value": canonicalize_literal(literal, effective_canonical_form(claim_type)),
    }, None
