"""Procedure-only governed group-proposal bridge types and helpers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.primitives import ordered_unique

PROPOSE_GROUP_FROM_KIND = "propose_group_from"


class ProcedureCandidateSignalRow(BaseModel):
    """One tri-state signal carried by a procedure-produced candidate row."""

    signal_source: str
    signal: Literal["support", "contradict", "unsure"]
    evidence: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProcedureCandidateEdgeRow(BaseModel):
    """Strict procedure output row converted to one candidate relationship."""

    from_type: str
    from_id: str
    to_type: str
    to_id: str
    properties: dict[str, Any] | None = None
    signals: list[ProcedureCandidateSignalRow] | None = None
    evidence_rationale: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProcedureProposeGroupSpec(BaseModel):
    """Declaration for the single terminal governed-output bridge."""

    relationship_type: str
    edges_from: str
    proposal_scope: Any
    thesis_text: str | None = None
    pending_refresh_mode: Literal["replace", "retain_missing"] | None = None
    analysis_state: dict[str, Any] | None = None
    suggested_priority: str | None = None

    model_config = ConfigDict(extra="forbid")


def parse_candidate_edge_rows(rows: list[Any]) -> list[ProcedureCandidateEdgeRow]:
    """Parse candidate rows while preserving the offending row index."""
    parsed: list[ProcedureCandidateEdgeRow] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(
                f"row {index} is a {type(row).__name__}, not an object shaped as a candidate edge"
            )
        try:
            parsed.append(ProcedureCandidateEdgeRow.model_validate(row))
        except Exception as exc:
            raise ValueError(f"row {index} is not a valid candidate edge: {exc}") from exc
    return parsed


def procedure_candidate_members(
    relationship_type: str,
    rows: list[ProcedureCandidateEdgeRow],
) -> tuple[list[CandidateMember], list[str]]:
    """Convert strict rows to shared group members and their signal sources."""
    members: list[CandidateMember] = []
    sources: list[str] = []
    for row in rows:
        signals = [
            CandidateSignal(
                signal_source=signal.signal_source,
                signal=signal.signal,
                evidence=signal.evidence or "",
            )
            for signal in row.signals or []
        ]
        sources.extend(signal.signal_source for signal in signals)
        members.append(
            CandidateMember(
                relationship_type=relationship_type,
                from_type=row.from_type,
                from_id=row.from_id,
                to_type=row.to_type,
                to_id=row.to_id,
                properties=dict(row.properties or {}),
                signals=signals,
                evidence_rationale=row.evidence_rationale,
            )
        )
    return members, list(ordered_unique(sources))


__all__ = [
    "PROPOSE_GROUP_FROM_KIND",
    "ProcedureCandidateEdgeRow",
    "ProcedureCandidateSignalRow",
    "ProcedureProposeGroupSpec",
    "parse_candidate_edge_rows",
    "procedure_candidate_members",
]
