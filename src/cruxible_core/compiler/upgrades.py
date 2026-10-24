"""Compiler transition rules shared by evaluation, activation and historical reads."""

from collections.abc import Sequence
from typing import Protocol, cast

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidence,
    MemberLawEvaluation,
)
from cruxible_client.contracts.compiler_upgrade import (
    COMPILER_UPGRADE_PATH,
    CompilerUpgrade,
    CompilerUpgradeNotice,
)
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.laws import (
    AUTHORITY_VERBS_UPGRADE_LAW,
    CLAIM_EVIDENCE_UPGRADE_LAW,
    CLAIM_REBIND_UPGRADE_LAW,
    COMPILER_UPGRADE_ACCEPTANCE_LAW,
    GOVERNED_TRIGGERS_UPGRADE_LAW,
    LIGHT_ARTIFACTS_UPGRADE_LAW,
    PROVIDER_BUILDS_UPGRADE_LAW,
    PROVIDER_CONTRACT_UPGRADE_LAW,
    PROVIDER_PACKAGE_UPGRADE_LAW,
    RESOURCE_BUDGET_UPGRADE_LAW,
    SDK_SOURCE_UPGRADE_LAW,
    SOURCE_CHECKED_UPGRADE_LAW,
    SUBJECT_IDENTITY_UPGRADE_LAW,
    TRIGGER_CAPTURE_UPGRADE_LAW,
    InstalledAcceptanceLaw,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import (
    ATTESTATION_COMPILER,
    AUTHORITY_VERBS_COMPILER,
    CLAIM_EVIDENCE_COMPILER,
    CLAIM_REBIND_COMPILER,
    COMPILER_REVISION_LABELS,
    GOVERNED_TRIGGERS_COMPILER,
    LIGHT_ARTIFACTS_COMPILER,
    ONTOLOGY_COMPILER,
    P2_B1_COMPILER,
    P2_B2_COMPILER,
    P2_B4_COMPILER,
    P2_B4_UNIT2_COMPILER,
    P2_B5_COMPILER,
    P2_C_COMPILER,
    PC_DF2_COMPILER,
    PC_HR_COMPILER,
    PROVIDER_BUILDS_COMPILER,
    PROVIDER_CONTRACT_COMPILER,
    PROVIDER_PACKAGE_COMPILER,
    RESOLUTION_COMPILER,
    RESOURCE_BUDGET_COMPILER,
    SDK_SOURCE_COMPILER,
    SOURCE_CHECKED_COMPILER,
    SUBJECT_IDENTITY_COMPILER,
    TRIGGER_CAPTURE_COMPILER,
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
    def members(self) -> Sequence[CandidateMemberEvidence | CandidateMemberLawEvidence]: ...


def upgrade_law(source: CompilerCoordinate, target: CompilerCoordinate) -> InstalledAcceptanceLaw:
    if target == LIGHT_ARTIFACTS_COMPILER and source in {
        SUBJECT_IDENTITY_COMPILER,
        PROVIDER_BUILDS_COMPILER,
        CLAIM_REBIND_COMPILER,
    }:
        return LIGHT_ARTIFACTS_UPGRADE_LAW
    if source in UPGRADE_V1_SOURCES and target == UPGRADE_COMPILER:
        return COMPILER_UPGRADE_ACCEPTANCE_LAW
    if source in {*UPGRADE_V1_SOURCES, UPGRADE_COMPILER} and target == PROVIDER_CONTRACT_COMPILER:
        return PROVIDER_CONTRACT_UPGRADE_LAW
    if (
        source in {*UPGRADE_V1_SOURCES, UPGRADE_COMPILER, PROVIDER_CONTRACT_COMPILER}
        and target == PROVIDER_PACKAGE_COMPILER
    ):
        return PROVIDER_PACKAGE_UPGRADE_LAW
    if (
        source
        in {
            *UPGRADE_V1_SOURCES,
            UPGRADE_COMPILER,
            PROVIDER_CONTRACT_COMPILER,
            PROVIDER_PACKAGE_COMPILER,
        }
        and target == RESOURCE_BUDGET_COMPILER
    ):
        return RESOURCE_BUDGET_UPGRADE_LAW
    if (
        source
        in {
            *UPGRADE_V1_SOURCES,
            UPGRADE_COMPILER,
            PROVIDER_CONTRACT_COMPILER,
            PROVIDER_PACKAGE_COMPILER,
            RESOURCE_BUDGET_COMPILER,
        }
        and target == SDK_SOURCE_COMPILER
    ):
        return SDK_SOURCE_UPGRADE_LAW
    if (
        source
        in {
            *UPGRADE_V1_SOURCES,
            UPGRADE_COMPILER,
            PROVIDER_CONTRACT_COMPILER,
            PROVIDER_PACKAGE_COMPILER,
            RESOURCE_BUDGET_COMPILER,
            SDK_SOURCE_COMPILER,
        }
        and target == CLAIM_EVIDENCE_COMPILER
    ):
        return CLAIM_EVIDENCE_UPGRADE_LAW
    if target == SOURCE_CHECKED_COMPILER and source in {
        *UPGRADE_V1_SOURCES,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
    }:
        return SOURCE_CHECKED_UPGRADE_LAW
    if target == TRIGGER_CAPTURE_COMPILER and source in {
        *UPGRADE_V1_SOURCES,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
    }:
        return TRIGGER_CAPTURE_UPGRADE_LAW
    if target == AUTHORITY_VERBS_COMPILER and source in {
        *UPGRADE_V1_SOURCES,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
    }:
        return AUTHORITY_VERBS_UPGRADE_LAW
    if target == GOVERNED_TRIGGERS_COMPILER and source in {
        *UPGRADE_V1_SOURCES,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
        AUTHORITY_VERBS_COMPILER,
    }:
        return GOVERNED_TRIGGERS_UPGRADE_LAW
    if target == SUBJECT_IDENTITY_COMPILER and source in {
        *UPGRADE_V1_SOURCES,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
        AUTHORITY_VERBS_COMPILER,
        GOVERNED_TRIGGERS_COMPILER,
    }:
        return SUBJECT_IDENTITY_UPGRADE_LAW
    if target == PROVIDER_BUILDS_COMPILER and source in {
        *UPGRADE_V1_SOURCES,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
        AUTHORITY_VERBS_COMPILER,
        GOVERNED_TRIGGERS_COMPILER,
        SUBJECT_IDENTITY_COMPILER,
    }:
        return PROVIDER_BUILDS_UPGRADE_LAW
    if target == CLAIM_REBIND_COMPILER and source in {
        *UPGRADE_V1_SOURCES,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
        AUTHORITY_VERBS_COMPILER,
        GOVERNED_TRIGGERS_COMPILER,
        SUBJECT_IDENTITY_COMPILER,
        PROVIDER_BUILDS_COMPILER,
    }:
        return CLAIM_REBIND_UPGRADE_LAW
    raise ValueError("unsupported compiler transition; only explicit forward edges are allowed")


# Every compiler an explicit forward edge can reach, in edge order.
_UPGRADE_TARGETS = (
    UPGRADE_COMPILER,
    PROVIDER_CONTRACT_COMPILER,
    PROVIDER_PACKAGE_COMPILER,
    RESOURCE_BUDGET_COMPILER,
    SDK_SOURCE_COMPILER,
    CLAIM_EVIDENCE_COMPILER,
    SOURCE_CHECKED_COMPILER,
    TRIGGER_CAPTURE_COMPILER,
    AUTHORITY_VERBS_COMPILER,
    GOVERNED_TRIGGERS_COMPILER,
    SUBJECT_IDENTITY_COMPILER,
    PROVIDER_BUILDS_COMPILER,
    CLAIM_REBIND_COMPILER,
    LIGHT_ARTIFACTS_COMPILER,
)


def supported_upgrade_targets(source: CompilerCoordinate) -> tuple[str, ...]:
    """Name the compiler digests one explicit forward edge reaches from `source`."""

    targets = []
    for target in _UPGRADE_TARGETS:
        try:
            upgrade_law(source, target)
        except ValueError:
            continue
        targets.append(target.rule_digest)
    return tuple(targets)


def default_upgrade_proposal_name(target: CompilerCoordinate) -> str:
    """The proposal name an upgrade to `target` takes when the caller names none.

    The revision label is display metadata; the proposal itself pins the digest.
    """

    revision = COMPILER_REVISION_LABELS.get(target) or target.rule_digest.removeprefix("sha256:")
    return f"upgrade-to-{revision[:64]}"


def upgrade_notice(value: CompilerUpgrade) -> CompilerUpgradeNotice:
    """Say, where activation is decided, that the transition cannot be undone.

    An activated upgrade makes the target the accepted head's compiler, and a
    daemon resolves every read and write through the compiler it installs
    under that digest. A release that predates the target installs no such
    compiler, so it cannot serve the instance; no edge leads back.
    """

    source = CompilerCoordinate(rule_digest=value.base.compiler_digest)
    target = value.target
    target_label = COMPILER_REVISION_LABELS.get(target)
    named = f"{target_label} ({target.rule_digest})" if target_label else target.rule_digest
    return CompilerUpgradeNotice(
        source_compiler_digest=source.rule_digest,
        source_revision=COMPILER_REVISION_LABELS.get(source),
        target_compiler_digest=target.rule_digest,
        target_revision=target_label,
        message=(
            f"One-way: activating moves this instance to compiler {named}. A release "
            "whose daemon does not install that compiler can no longer serve this "
            "instance, and no upgrade leads back. Back up the instance first "
            "(docs/upgrading.md, Back up first)."
        ),
    )


def upgrade_base_matches(value: CompilerUpgrade, base: AcceptedProjectionCoordinate) -> bool:
    """Whether the upgrade is bound to exactly this accepted base."""

    return (
        value.instance_id == base.instance_id
        and value.base.git_oid == base.git_oid
        and value.base.semantic_root == base.semantic_root
        and value.base.generation_root == base.generation_root
        and value.base.compiler_digest == base.compiler.rule_digest
    )


def validate_upgrade(value: CompilerUpgrade, base: AcceptedProjectionCoordinate) -> None:
    """Explicit supported edges; installing another compiler never implies permission to use it."""
    if not upgrade_base_matches(value, base):
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
    evidence = cast(tuple[MemberLawEvaluation, ...], getattr(record, "law_evidence", ()))
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
