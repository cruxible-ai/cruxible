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


def definition_format_version(definition: ProcedureDefinition) -> tuple[int, list[str]]:
    """Return ``(format version, warnings)``. The DISCRIMINATOR decides.

    The structural check is a cross-check with ASYMMETRIC consequences, because
    the two mismatches are not the same kind of mistake:

    * a v2 construct with no declaration would be digested under v1 rules AND
      would parse on an old core, which then mis-executes it -- **refuse**;
    * a declaration with no construct yet is well-formed and sometimes
      deliberate (pre-committing a definition to the v2 digest namespace before
      adding branches) -- **warn**.
    """
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
    "DEFINITION_FORMAT_V2",
    "definition_format_version",
    "refuse_unknown_artifact_format",
    "register_v2_definition_field",
    "register_v2_step_type",
    "registered_v2_definition_fields",
    "registered_v2_step_types",
]
