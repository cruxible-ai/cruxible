"""Typed Mandate fixtures; tests supply the exact Line and authority they exercise."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.procedure_mandates import ProcedureMandate
from tests.support.procedures import fixture_digest


def procedure_mandate(
    name: str = "fixture", *, changes: Mapping[str, Any] | None = None, **overrides: Any
) -> ProcedureMandate:
    """A finite propose grant, with explicit overrides for conditional settle grants."""

    target = ArtifactIdentity(kind="Line", name="fixture")
    return ProcedureMandate(
        **{
            "identity": ArtifactIdentity(kind="ProcedureMandate", name=name),
            "line": ArtifactPin(role="line", target=target, artifact_digest=fixture_digest(target)),
            "grants": "propose",
            "namespace": ("claims",),
            "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
            "expires_at": datetime(2027, 1, 1, tzinfo=UTC),
            **overrides,
            **dict(changes or {}),
        }
    )
