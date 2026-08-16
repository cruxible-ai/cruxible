"""Service helpers for resolving agent-supplied evidence references."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from cruxible_core.errors import DataValidationError
from cruxible_core.graph.evidence import (
    EvidenceRef,
    merge_evidence_ref_objects,
    normalize_evidence_ref,
)
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.playbill.actor_context import GovernedActorContext


def _validation_errors(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error['msg']}"
        if error.get("loc")
        else str(error["msg"])
        for error in exc.errors()
    ]


def resolve_evidence_refs(
    instance: InstanceProtocol,
    *,
    evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]] = (),
    source_evidence: Sequence[Mapping[str, Any]] = (),
    citation_handles: Sequence[str] = (),
    actor_context: GovernedActorContext | None = None,
) -> list[EvidenceRef]:
    """Resolve explicit refs; legacy source locators fail closed after PC-C."""
    try:
        explicit_refs = [normalize_evidence_ref(ref) for ref in evidence_refs]
    except ValidationError as exc:
        raise DataValidationError(
            "Invalid evidence_ref",
            errors=_validation_errors(exc),
        ) from exc
    if source_evidence or citation_handles:
        raise DataValidationError(
            "Legacy source_evidence and citation handles were removed; use a Playbill Capture"
        )
    return merge_evidence_ref_objects(explicit_refs)
