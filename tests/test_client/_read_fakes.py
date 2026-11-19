"""Accepted ClaimType reads as SDK fakes serve them through ``get(detail="proof")``.

A ClaimType's proof is its accepted read: coordinate, path, predicate,
identity, artifact digest and envelope. Fakes build these and hand the dump
back as the proof a ``get`` answers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from cruxible_client import contracts as api


class ClaimTypeRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coordinate: api.AcceptedCoordinate
    path: str
    predicate: str
    identity: str
    artifact_digest: str
    envelope: dict[str, Any]


class ClaimTypeListing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coordinate: api.AcceptedCoordinate
    claim_types: list[ClaimTypeRead]
