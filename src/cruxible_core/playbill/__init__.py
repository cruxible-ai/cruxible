"""Opt-in Playbill ledger and deterministic projection substrate.

PB-A/PB-B intentionally expose only internal/library initialization, reopening,
inspection, assembly, and immutable binding. Public CLI/HTTP/MCP surfaces arrive
with the Family-1 vertical slice after proposal and activation semantics exist.
"""

from typing import TYPE_CHECKING, Any

from cruxible_core.playbill.types import PlaybillTrustRoot, PrincipalRecord

if TYPE_CHECKING:
    from cruxible_core.playbill.assembler import ProjectionAssembler
    from cruxible_core.playbill.instance import PlaybillInstance
    from cruxible_core.playbill.projection import AssemblerRequest, AssemblerResult
    from cruxible_core.storage.playbill_projection import ProjectionHandle


def __getattr__(name: str) -> Any:
    """Load the PB-B storage-backed surface without creating package cycles."""

    if name == "ProjectionAssembler":
        from cruxible_core.playbill.assembler import ProjectionAssembler

        return ProjectionAssembler
    if name == "PlaybillInstance":
        from cruxible_core.playbill.instance import PlaybillInstance

        return PlaybillInstance
    if name in {"AssemblerRequest", "AssemblerResult"}:
        from cruxible_core.playbill import projection

        return getattr(projection, name)
    if name in {"ProjectionHandle", "bind_projection", "detect_projection_orphans"}:
        from cruxible_core.storage import playbill_projection

        return getattr(playbill_projection, name)
    raise AttributeError(name)


__all__ = [
    "AssemblerRequest",
    "AssemblerResult",
    "PlaybillInstance",
    "PlaybillTrustRoot",
    "PrincipalRecord",
    "ProjectionAssembler",
    "ProjectionHandle",
    "bind_projection",
    "detect_projection_orphans",
]
