"""Artifact-format versioning for procedure definitions.

This module holds ONE stored artifact class's implementation of a pattern that
is deliberately kept mechanically extractable (`dd-artifact-format-versioning`):

1. a **format discriminator** carried on the artifact itself
   (``ProcedureDefinition.graph_format``), never inferred from content;
2. a **reader-dispatch registry** keyed by that discriminator;
3. **frozen verifiers** for retired versions, so a stored artifact stays
   verifiable after its writer is gone;
4. a **loud refusal** on an unknown format, in one absorbable function;
5. evolution by **supersession**, never an in-place rewrite of stored bytes.

The registries and the refusal live here, in their own module scope and named
for their role rather than for procedures, because a third consumer of the
pattern (the first two: the snapshot procedures artifact and ``graph_format``)
should be able to lift them without untangling procedure-specific logic. No
shared helper is built yet.

Nothing here imports ``procedure.types`` at run time: registration is pushed
FROM the type module INTO this one, which keeps the dependency one-way and lets
the digest layer sit on top of both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from cruxible_core.errors import ConfigError

if TYPE_CHECKING:  # pragma: no cover - typing only, and deliberately one-way
    from cruxible_core.procedure.types import ProcedureDefinition

DEFINITION_FORMAT_V1 = 1
"""Every definition authored before graph procedures. ``graph_format`` absent."""

DEFINITION_FORMAT_V2 = 2
"""Graph-format definitions. ``graph_format: 2`` is declared on the wire."""

GRAPH_FORMAT_DECLARED_WITHOUT_CONSTRUCT = "graph_format_declared_without_construct"
"""Warning code for R14, kept beside the warning it names.

A plain string, so the one-way dependency holds: this module still imports
nothing from ``procedure.types``, and the typed-warning layer above maps the
code without either module learning about the other.
"""

SUPPORTED_DECLARED_FORMATS: frozenset[int | None] = frozenset({None, DEFINITION_FORMAT_V2})
"""The ONLY legal wire spellings of ``graph_format``.

Exactly one spelling per format, which is why ``1`` is absent: format v1 is
spelled by ABSENCE. Admitting an explicit ``1`` would give one definition two
wire forms with two digests, and the explicit form would additionally be
refused by a 0.3 core -- on a definition that core can otherwise read
perfectly.
"""

_V2_ONLY_POSITIONS: list[tuple[type[BaseModel], str]] = []
_V2_ONLY_STEP_TYPES: list[type[BaseModel]] = []

# ``graph_format`` is DELIBERATELY NEVER REGISTERED here: it is the
# authoritative declaration, not a construct. Registering it would make the
# structural check trivially agree with the declaration whenever the
# declaration is 2, so R14 could never fire and R13 would fire on nothing.


def register_v2_definition_field(model: type[BaseModel], field: str) -> None:
    """Declare a definition field whose presence implies format v2.

    Called from the same commit that DECLARES the field, so a definition can
    never exist carrying a construct the structural check does not yet know
    about -- which would silently produce two digests for one definition.
    """
    _V2_ONLY_POSITIONS.append((model, field))


def register_v2_step_type(step_type: type[BaseModel]) -> None:
    """Declare a step schema whose presence implies format v2."""
    _V2_ONLY_STEP_TYPES.append(step_type)


def registered_v2_definition_fields() -> tuple[tuple[type[BaseModel], str], ...]:
    """Return the registered v2-only definition positions (read-only view)."""
    return tuple(_V2_ONLY_POSITIONS)


def registered_v2_step_types() -> tuple[type[BaseModel], ...]:
    """Return the registered v2-only step types (read-only view)."""
    return tuple(_V2_ONLY_STEP_TYPES)


def refuse_unknown_artifact_format(
    *,
    artifact_class: str,
    declared_version: Any,
    supported_versions: tuple[int, ...],
) -> ConfigError:
    """Build the loud refusal for an artifact whose format nothing here reads.

    Kept as a function returning the error, in one place, so that an eventual
    shared format helper can absorb it verbatim. Failing closed is the whole
    point: an unknown format read by a reader that guesses is how stored
    provenance quietly stops meaning what it said.
    """
    supported = ", ".join(str(version) for version in sorted(supported_versions))
    return ConfigError(
        f"{artifact_class} declares format version {declared_version!r}, which this "
        f"core cannot read (supported: {supported}). Upgrade cruxible-core to a "
        "version that declares support for it; this reader refuses rather than "
        "guessing at an artifact it does not understand."
    )


def _uses_v2_construct(definition: ProcedureDefinition) -> bool:
    """Report whether a definition structurally uses any registered v2 construct.

    Traverses DECLARED MODEL FIELDS and REGISTERED STEP TYPES only. It never
    descends into a ``dict[str, Any]`` leaf -- not ``params``, not ``input``,
    not a projection's ``fields`` map. A provider input of
    ``{"next": ..., "parameters": {...}}`` is a perfectly ordinary v1
    definition, and the corpus carries exactly that collision as a regression
    entry: content-sniffing would route it through v2 digest rules and break
    perpetual v1 reproduction on live data.
    """
    for model, field in _V2_ONLY_POSITIONS:
        if isinstance(definition, model) and getattr(definition, field, None) is not None:
            return True
    step_types = tuple(_V2_ONLY_STEP_TYPES)
    if not step_types:
        return False
    return any(isinstance(step, step_types) for step in definition.steps)


def coerce_present_declared_format(value: Any) -> Any:
    """Refuse a ``graph_format`` KEY that is present with anything but the integer 2.

    Called only when the key IS on the wire, which is what lets explicit null
    be told apart from absence. They are not the same wire form even though
    they dump identically: a 0.3 core refuses `"graph_format": null` outright,
    so accepting it here is a reader silently taking something the format's own
    old-reader lock rejected. Format v1 is spelled by ABSENCE -- that is the
    whole spelling, key included.

    Runs in ``mode="before"``, ahead of pydantic's coercion, because coercion
    is the fail-open: ``int | None`` is non-strict, so ``"2"`` and ``2.0``
    become ``2`` and reach the value check already looking legal. The wire has
    one spelling per format, and a JSON string is not it -- an artifact whose
    discriminator arrives as a different JSON type was not written by a core
    that agrees with this one about what the field is.

    ``bool`` is excluded on purpose despite being an ``int`` subclass: ``True``
    would otherwise read as the explicit ``1`` it is not.
    """
    if value is None:
        # The unknown-format refusal, plus the remedy the generic wording
        # cannot carry: this key should not be here at all.
        unreadable = refuse_unknown_artifact_format(
            artifact_class="ProcedureDefinition",
            declared_version=None,
            supported_versions=(DEFINITION_FORMAT_V1, DEFINITION_FORMAT_V2),
        )
        raise ConfigError(
            f"{unreadable} Format v1 is spelled by ABSENCE: omit the key entirely "
            "rather than sending an explicit null, which a 0.3 core refuses outright "
            "and which is therefore not the same wire form as omitting it."
        )
    if type(value) is not int:
        raise refuse_unknown_artifact_format(
            artifact_class="ProcedureDefinition",
            declared_version=value,
            supported_versions=(DEFINITION_FORMAT_V1, DEFINITION_FORMAT_V2),
        )
    _refuse_unreadable_declaration(value)
    return value


def _refuse_unreadable_declaration(declared: int | None) -> None:
    """Refuse any ``graph_format`` outside the legal wire spellings.

    Two distinct wrongs, so two distinct messages -- an author who wrote ``1``
    needs to be told to DELETE the key, and an operator who met a ``3`` needs
    to be told to upgrade the core. Collapsing them into one refusal would send
    each of them the other's instruction.
    """
    if declared in SUPPORTED_DECLARED_FORMATS:
        return
    if declared == DEFINITION_FORMAT_V1:
        raise ConfigError(
            "definition declares 'graph_format: 1'. Format v1 is spelled by ABSENCE: "
            "remove the key. An explicit 1 is a second wire form for one format -- it "
            "is emitted by exclude_none dumps, so it yields a different digest for an "
            "otherwise identical definition, and a 0.3 core refuses it outright on a "
            "definition it could otherwise read."
        )
    raise refuse_unknown_artifact_format(
        artifact_class="ProcedureDefinition",
        declared_version=declared,
        supported_versions=(DEFINITION_FORMAT_V1, DEFINITION_FORMAT_V2),
    )


def definition_format_version(definition: ProcedureDefinition) -> tuple[int, list[str]]:
    """Return ``(format version, warnings)``. The DISCRIMINATOR decides.

    The structural check is a cross-check with ASYMMETRIC consequences, because
    the two mismatches are not the same kind of mistake:

    * a v2 construct with no declaration would be digested under v1 rules AND
      would parse on an old core, which then mis-executes it -- **refuse**;
    * a declaration with no construct yet is well-formed and sometimes
      deliberate (pre-committing a definition to the v2 digest namespace before
      adding branches) -- **warn**.

    Before either, the DECLARED VALUE ITSELF is checked. ``graph_format`` is a
    known key, so ``extra="forbid"`` refuses an unknown KEY and can say nothing
    about an unreadable VALUE. Treating everything that is not ``2`` as v1
    would fail open on exactly the artifact the discriminator exists to catch.
    """
    _refuse_unreadable_declaration(definition.graph_format)
    declared = DEFINITION_FORMAT_V2 if definition.graph_format == 2 else DEFINITION_FORMAT_V1
    structural = DEFINITION_FORMAT_V2 if _uses_v2_construct(definition) else DEFINITION_FORMAT_V1

    if structural == DEFINITION_FORMAT_V2 and declared == DEFINITION_FORMAT_V1:
        raise ConfigError(  # R13
            "definition uses a graph construct but does not declare 'graph_format: 2'; "
            "add the declaration or remove the construct. Without it the definition "
            "would be digested under format-v1 rules and would parse on a core that "
            "cannot execute its control flow."
        )
    if structural == DEFINITION_FORMAT_V1 and declared == DEFINITION_FORMAT_V2:
        return DEFINITION_FORMAT_V2, [  # R14
            "graph_format 2 declared but no graph construct is used"
        ]
    return declared, []


__all__ = [
    "DEFINITION_FORMAT_V1",
    "GRAPH_FORMAT_DECLARED_WITHOUT_CONSTRUCT",
    "coerce_present_declared_format",
    "DEFINITION_FORMAT_V2",
    "SUPPORTED_DECLARED_FORMATS",
    "definition_format_version",
    "refuse_unknown_artifact_format",
    "register_v2_definition_field",
    "register_v2_step_type",
    "registered_v2_definition_fields",
    "registered_v2_step_types",
]
