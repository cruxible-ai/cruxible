"""Compiler transition rules shared by evaluation, activation and replay."""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Protocol, cast

from cruxible_client.contracts.candidates import (
    CandidateMemberLawEvidence,
    MemberLawEvaluation,
)
from cruxible_client.contracts.compiler_upgrade import (
    COMPILER_UPGRADE_PATH,
    CompilerUpgrade,
    CompilerUpgradeNotice,
)
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.laws import InstalledAcceptanceLaw
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import compiler_revision_label
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate

#: Explicit forward edges: (source, target) -> the law that judges the
#: transition. This release installs one compiler, so no edge exists; a future
#: revision adds its edge from the current coordinate here, and nothing else
#: implies permission to move.
_UPGRADE_EDGES: Mapping[tuple[CompilerCoordinate, CompilerCoordinate], InstalledAcceptanceLaw] = (
    MappingProxyType({})
)


class CompilerBoundRecord(Protocol):
    @property
    def compiler_digest(self) -> str: ...

    @property
    def members(self) -> Sequence[CandidateMemberLawEvidence]: ...


def upgrade_law(source: CompilerCoordinate, target: CompilerCoordinate) -> InstalledAcceptanceLaw:
    try:
        return _UPGRADE_EDGES[(source, target)]
    except KeyError:
        raise ValueError(
            "unsupported compiler transition; only explicit forward edges are allowed"
        ) from None


def supported_upgrade_targets(source: CompilerCoordinate) -> tuple[str, ...]:
    """Name the compiler digests one explicit forward edge reaches from `source`."""

    return tuple(
        target.rule_digest for edge_source, target in _UPGRADE_EDGES if edge_source == source
    )


def default_upgrade_proposal_name(target: CompilerCoordinate) -> str:
    """The proposal name an upgrade to `target` takes when the caller names none.

    The revision label is display metadata; the proposal itself pins the digest.
    """

    revision = compiler_revision_label(target) or target.rule_digest.removeprefix("sha256:")
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
    target_label = compiler_revision_label(target)
    named = f"{target_label} ({target.rule_digest})" if target_label else target.rule_digest
    return CompilerUpgradeNotice(
        source_compiler_digest=source.rule_digest,
        source_revision=compiler_revision_label(source),
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
