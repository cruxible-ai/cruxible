"""One settle grant over the observations a Line writes (D-R50, ``scope: observations``).

The grant lowers to an ordinary ``cruxible-procedure-mandate-v4`` scope: one
exact ClaimType per Claim template the Line's Procedure declares, with the
change kinds and evidence bases those templates can produce. Every ClaimType
must be observation-only (``permitted_roles == ("observation",)``); a Procedure
that writes any other ClaimType, or invokes another Procedure (whose templates
its own definition cannot show), is refused.

Per template:

- a value write creates and revises on ``extracted`` and ``measured`` (and
  ``derived`` only when the author opts in);
- ``when_absent: mirror`` retires on ``absence``;
- ``when_absent: inverse`` writes its inverse as a create or revise on
  ``absence``, and retires a duplicate unit on ``absence``.

Pure and shared: authoring lowering pins the result, and the CLI shows it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.claim_type_structure import is_observation_only_claim_type
from cruxible_client.contracts.mandate_scope import (
    MANDATE_BASIS_ORDER,
    MANDATE_CHANGE_KIND_ORDER,
    MandateBasis,
    MandateChangeKind,
    MandateChanges,
)

#: The value bases an observation grant settles without opting in to ``derived``.
OBSERVATION_VALUE_BASES: Final[tuple[MandateBasis, ...]] = ("extracted", "measured")
#: How deep a Subject spec's ends nest before lowering stops reading them.
_MAX_SPEC_DEPTH: Final = 16
#: Where an observation grant reaches when the author names no namespace.
OBSERVATION_SETTLE_NAMESPACE: Final[tuple[str, ...]] = ("claims",)

ObservationSettleRefusalCode = Literal[
    "cruxible.authoring.procedure_mandate_observations_invoke",
    "cruxible.authoring.procedure_mandate_observations_empty",
    "cruxible.authoring.procedure_mandate_claim_type_missing",
    "cruxible.authoring.procedure_mandate_observations_not_observation",
    "cruxible.authoring.procedure_mandate_observations_creation_unavailable",
]


class _StrictObservationSettleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationSettleClaimType(_StrictObservationSettleModel):
    """One ClaimType the grant covers, by predicate, with its lowered changes."""

    claim_type: str
    changes: MandateChanges


class ObservationSettleScope(_StrictObservationSettleModel):
    """What ``scope: observations`` lowers to for one Procedure.

    ``creatable_subject_kinds`` are the Subject kinds the Procedure can create
    by key; a grant settles their creation only when ``subject_creation``
    names them.
    """

    claim_types: tuple[ObservationSettleClaimType, ...]
    creatable_subject_kinds: tuple[str, ...] = ()


class ObservationSettleRefusal(_StrictObservationSettleModel):
    """Why ``scope: observations`` cannot lower, with the repair it names."""

    code: ObservationSettleRefusalCode
    message: str
    repair_kind: str
    repair_description: str
    #: The ClaimTypes (or Subject kinds) the refusal is about.
    names: tuple[str, ...] = ()


def _sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: item.encode("utf-8")))


def _spec_part(spec: object, name: str) -> object:
    return spec.get(name) if isinstance(spec, Mapping) else getattr(spec, name, None)


def spec_created_kinds(spec: object, *, _depth: int = 0) -> set[str]:
    """The Subject kinds one ``{kind, key, create}`` spec can create.

    A spec with ``create: if_absent`` creates its own kind, and, as the runtime
    resolves a pair kind's ends, every end its ``key`` names the same way with
    ``create: if_absent``, recursively. An end inside a spec that does not
    create is never created (the pair can be neither found nor made), so
    nothing below it counts. Takes a model (``ClaimsSubject``) or a mapping.
    """

    if _depth > _MAX_SPEC_DEPTH or _spec_part(spec, "create") != "if_absent":
        return set()
    kind = _spec_part(spec, "kind")
    if not isinstance(kind, str):
        return set()
    kinds = {kind}
    key = _spec_part(spec, "key")
    if isinstance(key, Mapping):
        for part in key.values():
            if isinstance(part, Mapping):
                kinds |= spec_created_kinds(part, _depth=_depth + 1)
    return kinds


def created_subject_kinds(node: object) -> set[str]:
    """Every Subject kind a claims node can create: its row Subject and its value templates."""

    kinds = spec_created_kinds(getattr(node, "subject", None))
    for template in getattr(node, "claims", ()):
        kinds |= spec_created_kinds(template.value)
    return kinds


def observation_settle_scope(
    nodes: Iterable[object],
    claim_type: Callable[[str], object | None],
    *,
    include_derived: bool = False,
    subject_creation: Iterable[str] = (),
) -> ObservationSettleScope | ObservationSettleRefusal:
    """Lower ``scope: observations`` over a Procedure's ``nodes``.

    ``claim_type`` resolves a predicate to the accepted or staged ClaimType
    (anything with ``permitted_roles``), or ``None``. ``subject_creation`` must
    name only kinds the Procedure can create.
    """

    from cruxible_client.contracts.procedures.claims import ClaimsNode

    value_bases: tuple[MandateBasis, ...] = (
        (*OBSERVATION_VALUE_BASES, "derived") if include_derived else OBSERVATION_VALUE_BASES
    )
    needed: dict[str, dict[MandateChangeKind, set[MandateBasis]]] = {}
    creatable: set[str] = set()
    for node in nodes:
        if getattr(node, "kind", None) == "invoke":
            return ObservationSettleRefusal(
                code="cruxible.authoring.procedure_mandate_observations_invoke",
                message=(
                    "scope: observations reads the Claim templates of the Line's own "
                    f"Procedure; node {getattr(node, 'node_id', '?')!r} invokes another "
                    "Procedure, whose templates it cannot show."
                ),
                repair_kind="name_scope",
                repair_description=(
                    "Name each ClaimType the run writes in scope, with its change kinds and bases."
                ),
            )
        if not isinstance(node, ClaimsNode):
            continue
        creatable |= created_subject_kinds(node)
        for template in node.claims:
            changes = needed.setdefault(template.claim_type.target.name, {})
            for kind in ("create", "revise"):
                changes.setdefault(kind, set()).update(value_bases)
            if node.expect is not None and template.when_absent in ("mirror", "inverse"):
                changes.setdefault("retire", set()).add("absence")
            if node.expect is not None and template.when_absent == "inverse":
                changes["create"].add("absence")
                changes["revise"].add("absence")
    if not needed:
        return ObservationSettleRefusal(
            code="cruxible.authoring.procedure_mandate_observations_empty",
            message="scope: observations needs a Procedure with claims templates; it has none.",
            repair_kind="replace_line_name",
            repair_description="Grant a Line whose Procedure writes Claims.",
        )
    names = _sorted(needed)
    resolved = {name: claim_type(name) for name in names}
    missing = tuple(name for name in names if resolved[name] is None)
    if missing:
        return ObservationSettleRefusal(
            code="cruxible.authoring.procedure_mandate_claim_type_missing",
            message=(
                "scope: observations needs every ClaimType the Procedure writes accepted or "
                f"staged; missing: {', '.join(missing)}."
            ),
            repair_kind="replace_claim_type",
            repair_description="Accept or stage the named ClaimTypes in the same change.",
            names=missing,
        )
    mixed = tuple(name for name in names if not is_observation_only_claim_type(resolved[name]))
    if mixed:
        roles = "; ".join(
            f"{name} permits {', '.join(getattr(resolved[name], 'permitted_roles', ()))}"
            for name in mixed
        )
        return ObservationSettleRefusal(
            code="cruxible.authoring.procedure_mandate_observations_not_observation",
            message=(
                "scope: observations covers observation-only ClaimTypes (permitted_roles: "
                f"observation); the Procedure also writes {roles}."
            ),
            repair_kind="name_scope",
            repair_description=(
                "Name the scope per ClaimType instead (each review stays explicit), or "
                "write those ClaimTypes from another Line."
            ),
            names=mixed,
        )
    stray = _sorted(set(subject_creation) - creatable)
    if stray:
        return ObservationSettleRefusal(
            code="cruxible.authoring.procedure_mandate_observations_creation_unavailable",
            message=(
                f"subject_creation names {', '.join(stray)}, which the Procedure never "
                "creates; it creates "
                f"{', '.join(_sorted(creatable)) if creatable else 'no Subjects'}."
            ),
            repair_kind="replace_subject_creation",
            repair_description="Name only Subject kinds the Procedure creates by key.",
            names=stray,
        )
    return ObservationSettleScope(
        claim_types=tuple(
            ObservationSettleClaimType(
                claim_type=name,
                changes=MandateChanges.model_validate(
                    {
                        kind: {
                            "basis": [
                                basis
                                for basis in MANDATE_BASIS_ORDER
                                if basis in needed[name][kind]
                            ]
                        }
                        for kind in MANDATE_CHANGE_KIND_ORDER
                        if kind in needed[name]
                    }
                ),
            )
            for name in names
        ),
        creatable_subject_kinds=_sorted(creatable),
    )


__all__ = [
    "OBSERVATION_SETTLE_NAMESPACE",
    "OBSERVATION_VALUE_BASES",
    "ObservationSettleClaimType",
    "ObservationSettleRefusal",
    "ObservationSettleRefusalCode",
    "ObservationSettleScope",
    "created_subject_kinds",
    "observation_settle_scope",
    "spec_created_kinds",
]
