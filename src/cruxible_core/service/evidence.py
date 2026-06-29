"""Service helpers for resolving agent-supplied evidence references."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from cruxible_core.errors import DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import (
    EvidenceRef,
    merge_evidence_ref_objects,
    normalize_evidence_ref,
)
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.service.source_artifacts import resolve_source_evidence_refs
from cruxible_core.source_artifacts.types import SourceEvidenceInput


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
    source_evidence: Sequence[SourceEvidenceInput | Mapping[str, Any]] = (),
    actor_context: GovernedActorContext | None = None,
) -> list[EvidenceRef]:
    """Resolve explicit and source-backed evidence into canonical refs."""
    try:
        explicit_refs = [normalize_evidence_ref(ref) for ref in evidence_refs]
    except ValidationError as exc:
        raise DataValidationError(
            "Invalid evidence_ref",
            errors=_validation_errors(exc),
        ) from exc
    try:
        source_refs = resolve_source_evidence_refs(
            instance,
            source_evidence,
            actor_context=actor_context,
        )
    except ValidationError as exc:
        raise DataValidationError(
            "Invalid source_evidence",
            errors=_validation_errors(exc),
        ) from exc
    return merge_evidence_ref_objects(
        explicit_refs,
        source_refs,
    )
