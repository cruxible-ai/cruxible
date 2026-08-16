"""Versioned Playbill compiler coordinates and their exact projection registries."""

from __future__ import annotations

from cruxible_core.playbill.canonical import canonical_digest
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.projection_extensions import (
    ProjectionExtensionRegistry,
    fixture_extension_registry,
    playbill_extension_registry,
    playbill_governance_extension_registry,
    playbill_subject_extension_registry,
)
from cruxible_core.playbill.types import CompilerCoordinate


def _coordinate(*, projection_content: str | None = None) -> CompilerCoordinate:
    payload: dict[str, object] = {
        "implementation": "python-reference",
        "schema_version": 1,
    }
    if projection_content is not None:
        payload["projection_content"] = projection_content
    return CompilerCoordinate(
        rule_digest=f"sha256:{canonical_digest('playbill-compiler-v1', payload)}"
    )


PB_B_COMPILER = _coordinate()
PB_C_COMPILER = _coordinate(projection_content="family-1-document-v1")
PB_D_COMPILER = _coordinate(projection_content="family-1-document-governance-v1")
PC_A1_COMPILER = _coordinate(projection_content="claims-procedures-subject-v1")
SUPPORTED_COMPILERS = (PB_B_COMPILER, PB_C_COMPILER, PB_D_COMPILER, PC_A1_COMPILER)


def current_compiler_coordinate() -> CompilerCoordinate:
    return PC_A1_COMPILER


def projection_registry_for_compiler(
    compiler: CompilerCoordinate,
) -> ProjectionExtensionRegistry:
    if compiler == PB_B_COMPILER:
        return fixture_extension_registry()
    if compiler == PB_C_COMPILER:
        return playbill_extension_registry()
    if compiler == PB_D_COMPILER:
        return playbill_governance_extension_registry()
    if compiler == PC_A1_COMPILER:
        return playbill_subject_extension_registry()
    raise PlaybillFormatError("compiler coordinate has no installed deterministic registry")


__all__ = [
    "PB_B_COMPILER",
    "PB_C_COMPILER",
    "PB_D_COMPILER",
    "PC_A1_COMPILER",
    "SUPPORTED_COMPILERS",
    "current_compiler_coordinate",
    "projection_registry_for_compiler",
]
