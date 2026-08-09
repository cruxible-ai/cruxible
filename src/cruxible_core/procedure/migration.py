"""Pure helpers for the governed procedure-format convergence sweep.

Migration never edits a stored definition.  A lift is a newly validated v2
definition whose normalized payload differs from its v1 predecessor only by
the ``graph_format`` discriminator; lifecycle changes remain service-layer
operations over the ordinary proposal and review verbs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from cruxible_core.errors import ConfigError
from cruxible_core.procedure.graph_format import (
    DEFINITION_FORMAT_V1,
    DEFINITION_FORMAT_V2,
    definition_format_version,
)
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureRecord,
    compute_procedure_definition_digest,
)

PendingLiftDisposition = Literal["none", "matching", "refused"]


@dataclass(frozen=True)
class PendingLiftScan:
    """Read-only result of checking one name's pending proposal queue."""

    disposition: PendingLiftDisposition
    pending: ProcedureRecord | None = None
    conflicts: tuple[ProcedureRecord, ...] = ()
    reason: str | None = None


def _definition_payload(definition: ProcedureDefinition) -> dict[str, Any]:
    return definition.model_dump(mode="json", by_alias=True, exclude_none=True)


def assert_pure_v1_to_v2_lift(
    predecessor: ProcedureDefinition,
    successor: ProcedureDefinition,
) -> None:
    """Refuse any purported format lift that changes authored content."""
    name = predecessor.name
    predecessor_version, _ = definition_format_version(predecessor)
    successor_version, _ = definition_format_version(successor)
    if predecessor_version != DEFINITION_FORMAT_V1:
        raise ConfigError(
            f"Procedure migration refused for '{name}': predecessor is graph format "
            f"{predecessor_version}, not frozen format v1"
        )
    if successor_version != DEFINITION_FORMAT_V2:
        raise ConfigError(
            f"Procedure migration refused for '{name}': successor is graph format "
            f"{successor_version}, not graph format v2"
        )

    predecessor_payload = _definition_payload(predecessor)
    successor_payload = _definition_payload(successor)
    if predecessor_payload["steps"] != successor_payload["steps"]:
        raise ConfigError(f"Procedure migration refused for '{name}': lift would change a step")

    successor_without_format = dict(successor_payload)
    successor_without_format.pop("graph_format", None)
    if successor_without_format != predecessor_payload:
        raise ConfigError(
            f"Procedure migration refused for '{name}': lift would change definition "
            "content other than graph_format"
        )


def lift_v1_procedure_definition(definition: ProcedureDefinition) -> ProcedureDefinition:
    """Construct one validated ``graph_format: 2`` successor, changing nothing else."""
    payload = _definition_payload(definition)
    lifted = ProcedureDefinition.model_validate({**payload, "graph_format": 2})
    assert_pure_v1_to_v2_lift(definition, lifted)
    return lifted


def scan_pending_procedure_lift(
    predecessor: ProcedureRecord,
    lifted: ProcedureDefinition,
    pending_rows: Sequence[ProcedureRecord],
) -> PendingLiftScan:
    """Classify pending rows by the frozen pre-acceptance dedupe key.

    A matching row is carried forward.  Any other pending proposal under the
    same name wins over a match and refuses the bulk operation: the sweep must
    never race an unrelated human-authored revision.
    """
    name = predecessor.definition.name
    expected_digest = compute_procedure_definition_digest(lifted)
    relevant = tuple(
        row for row in pending_rows if row.status == "pending" and row.definition.name == name
    )
    matching = tuple(
        row
        for row in relevant
        if row.supersedes_procedure_id == predecessor.procedure_id
        and row.definition_digest == expected_digest
    )
    conflicts = tuple(row for row in relevant if row not in matching)
    if conflicts:
        named = ", ".join(row.procedure_id for row in conflicts)
        return PendingLiftScan(
            disposition="refused",
            conflicts=conflicts,
            reason=(
                f"Procedure migration refused for '{name}': non-matching pending "
                f"proposal(s) {named} already exist"
            ),
        )
    if len(matching) > 1:
        named = ", ".join(row.procedure_id for row in matching)
        return PendingLiftScan(
            disposition="refused",
            conflicts=matching,
            reason=(
                f"Procedure migration refused for '{name}': multiple matching pending "
                f"lifts already exist ({named})"
            ),
        )
    if matching:
        return PendingLiftScan(disposition="matching", pending=matching[0])
    return PendingLiftScan(disposition="none")


__all__ = [
    "PendingLiftDisposition",
    "PendingLiftScan",
    "assert_pure_v1_to_v2_lift",
    "lift_v1_procedure_definition",
    "scan_pending_procedure_lift",
]
