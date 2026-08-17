"""Frozen graph-v2 ``propose_group_from`` wire model.

New authoring cannot reach this module.  The one remaining model is retained
solely so historical v1/v2 definitions and their digests remain verifiable.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

PROPOSE_GROUP_FROM_KIND = "propose_group_from"


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


__all__ = [
    "PROPOSE_GROUP_FROM_KIND",
    "ProcedureProposeGroupSpec",
]
