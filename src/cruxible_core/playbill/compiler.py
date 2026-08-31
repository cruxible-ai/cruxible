"""Versioned Playbill compiler coordinates and their exact projection registries."""

from __future__ import annotations

from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.projection_extensions import (
    ProjectionExtensionRegistry,
    fixture_extension_registry,
    playbill_claim_extension_registry,
    playbill_claim_type_extension_registry,
    playbill_evidence_extension_registry,
    playbill_extension_registry,
    playbill_governance_extension_registry,
    playbill_procedure_extension_registry,
    playbill_runtime_extension_registry,
    playbill_subject_extension_registry,
)
from cruxible_client.contracts.types import CompilerCoordinate


def _coordinate(
    *,
    projection_content: str | None = None,
    semantic_revision: int | None = None,
) -> CompilerCoordinate:
    payload: dict[str, object] = {
        "implementation": "python-reference",
        "schema_version": 1,
    }
    if projection_content is not None:
        payload["projection_content"] = projection_content
    if semantic_revision is not None:
        payload["semantic_revision"] = semantic_revision
    return CompilerCoordinate(
        rule_digest=f"sha256:{canonical_digest('playbill-compiler-v1', payload)}"
    )


PB_B_COMPILER = _coordinate()
PB_C_COMPILER = _coordinate(projection_content="family-1-document-v1")
PB_D_COMPILER = _coordinate(projection_content="family-1-document-governance-v1")
PC_A1_COMPILER = _coordinate(projection_content="claims-procedures-subject-v1")
PC_A2_COMPILER = _coordinate(projection_content="claims-procedures-claim-type-v1")
PC_B_COMPILER = _coordinate(projection_content="claims-procedures-claim-v1")
PC_C_COMPILER = _coordinate(projection_content="claims-procedures-evidence-v1")
PC_D_COMPILER = _coordinate(projection_content="claims-procedures-procedure-v1")
PC_E1_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=10,
)
P2_B0_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=11,
)
SUPPORTED_COMPILERS = (
    PB_B_COMPILER,
    PB_C_COMPILER,
    PB_D_COMPILER,
    PC_A1_COMPILER,
    PC_A2_COMPILER,
    PC_B_COMPILER,
    PC_C_COMPILER,
    PC_D_COMPILER,
    PC_E1_COMPILER,
    P2_B0_COMPILER,
)


def current_compiler_coordinate() -> CompilerCoordinate:
    return P2_B0_COMPILER


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
    if compiler == PC_A2_COMPILER:
        return playbill_claim_type_extension_registry()
    if compiler == PC_B_COMPILER:
        return playbill_claim_extension_registry()
    if compiler == PC_C_COMPILER:
        return playbill_evidence_extension_registry()
    if compiler == PC_D_COMPILER:
        return playbill_procedure_extension_registry()
    if compiler in {PC_E1_COMPILER, P2_B0_COMPILER}:
        return playbill_runtime_extension_registry()
    raise PlaybillFormatError("compiler coordinate has no installed deterministic registry")


__all__ = [
    "PB_B_COMPILER",
    "PB_C_COMPILER",
    "PB_D_COMPILER",
    "PC_A1_COMPILER",
    "PC_A2_COMPILER",
    "PC_B_COMPILER",
    "PC_C_COMPILER",
    "PC_D_COMPILER",
    "PC_E1_COMPILER",
    "P2_B0_COMPILER",
    "SUPPORTED_COMPILERS",
    "current_compiler_coordinate",
    "projection_registry_for_compiler",
]
