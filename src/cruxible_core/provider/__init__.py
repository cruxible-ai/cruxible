"""Provider runtime surface."""

from typing import TYPE_CHECKING, Any

from cruxible_core.provider.payloads import (
    EvidenceRef,
    JsonItems,
    ParsedTabularBundle,
    evidence_ref,
    merge_evidence_refs,
)
from cruxible_core.provider.types import ExecutionTrace, ProviderContext, ResolvedArtifact

if TYPE_CHECKING:
    from cruxible_core.provider.registry import resolve_provider

__all__ = [
    "ExecutionTrace",
    "EvidenceRef",
    "JsonItems",
    "ParsedTabularBundle",
    "ProviderContext",
    "ResolvedArtifact",
    "evidence_ref",
    "merge_evidence_refs",
    "resolve_provider",
]


def __getattr__(name: str) -> Any:
    # resolve_provider is exported lazily: registry imports the runtime
    # package, and an eager import here closes an import cycle
    # (receipt.store -> provider -> registry -> runtime.instance ->
    # receipt.store) for entry points that reach receipts before runtime —
    # e.g. scripts/publish_kev_release.py via service -> query.types.
    if name == "resolve_provider":
        from cruxible_core.provider.registry import resolve_provider

        return resolve_provider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
