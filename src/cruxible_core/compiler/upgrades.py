"""Compiler transition rules shared by evaluation, activation and historical reads."""

from collections.abc import Sequence
from typing import Protocol, cast

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    MemberLawEvaluationV2,
)
from cruxible_client.contracts.compiler_upgrade import COMPILER_UPGRADE_PATH, CompilerUpgradeV1
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.laws import (
    COMPILER_UPGRADE_ACCEPTANCE_LAW,
    PROVIDER_CONTRACT_UPGRADE_LAW,
    PROVIDER_PACKAGE_UPGRADE_LAW,
    InstalledAcceptanceLaw,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import (
    ATTESTATION_COMPILER,
    ONTOLOGY_COMPILER,
    P2_B1_COMPILER,
    P2_B2_COMPILER,
    P2_B4_COMPILER,
    P2_B4_UNIT2_COMPILER,
    P2_B5_COMPILER,
    P2_C_COMPILER,
    PC_DF2_COMPILER,
    PC_HR_COMPILER,
    PROVIDER_CONTRACT_COMPILER,
    PROVIDER_PACKAGE_COMPILER,
    RESOLUTION_COMPILER,
    UPGRADE_COMPILER,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate

# Frozen by the v1 upgrade law. New installed compilers do not add edges.
UPGRADE_V1_SOURCES = frozenset(
    {
        PC_HR_COMPILER,
        P2_B1_COMPILER,
        P2_C_COMPILER,
        PC_DF2_COMPILER,
        P2_B2_COMPILER,
        P2_B4_COMPILER,
        P2_B4_UNIT2_COMPILER,
        P2_B5_COMPILER,
        ATTESTATION_COMPILER,
        RESOLUTION_COMPILER,
        ONTOLOGY_COMPILER,
    }
)


class CompilerBoundRecord(Protocol):
    @property
    def compiler_digest(self) -> str: ...

    @property
    def members(self) -> Sequence[CandidateMemberEvidence | CandidateMemberLawEvidenceV2]: ...


def upgrade_law(source: CompilerCoordinate, target: CompilerCoordinate) -> InstalledAcceptanceLaw:
    if source in UPGRADE_V1_SOURCES and target == UPGRADE_COMPILER:
        return COMPILER_UPGRADE_ACCEPTANCE_LAW
    if source in {*UPGRADE_V1_SOURCES, UPGRADE_COMPILER} and target == PROVIDER_CONTRACT_COMPILER:
        return PROVIDER_CONTRACT_UPGRADE_LAW
    if (
        source in {*UPGRADE_V1_SOURCES, UPGRADE_COMPILER, PROVIDER_CONTRACT_COMPILER}
        and target == PROVIDER_PACKAGE_COMPILER
    ):
        return PROVIDER_PACKAGE_UPGRADE_LAW
    raise ValueError("unsupported compiler transition; only explicit forward edges are allowed")


def validate_upgrade(value: CompilerUpgradeV1, base: AcceptedProjectionCoordinate) -> None:
    """Explicit supported edges; installing another compiler never implies permission to use it."""
    if (
        value.instance_id != base.instance_id
        or value.base.git_oid != base.git_oid
        or value.base.semantic_root != base.semantic_root
        or value.base.generation_root != base.generation_root
        or value.base.compiler_digest != base.compiler.rule_digest
    ):
        raise ValueError("compiler upgrade is bound to a different accepted base")
    upgrade_law(base.compiler, value.target)


def compiler_after_record(record: CompilerBoundRecord) -> CompilerCoordinate:
    """Read the resulting coordinate from replay-verified law evidence, never today's alias."""
    source = CompilerCoordinate(rule_digest=record.compiler_digest)
    members = record.members
    upgrades = [member for member in members if member.artifact_kind == "compiler-upgrade"]
    if not upgrades:
        return source
    if len(members) != 1 or upgrades[0].path != COMPILER_UPGRADE_PATH:
        raise SettlementIntegrityError("compiler upgrade must be a sole-purpose generation")
    evidence = cast(tuple[MemberLawEvaluationV2, ...], getattr(record, "law_evidence", ()))
    if len(evidence) != 1:
        raise SettlementIntegrityError("compiler upgrade law evidence is missing")
    target = CompilerCoordinate.model_validate(evidence[0].result["target_compiler"])
    try:
        law = upgrade_law(source, target).coordinate
    except ValueError as exc:
        raise SettlementIntegrityError(str(exc)) from exc
    if evidence[0].law_identifier != law.identifier or evidence[0].law_digest != law.digest:
        raise SettlementIntegrityError("compiler upgrade law evidence is missing")
    return target
