"""Per-terminal-item dependency closure derived from actual run dataflow.

A ``TerminalItemDependencyManifestV1`` is never assembled from a caller-supplied
evidence list.  The executor threads a :class:`DependencyToken` set alongside
every runtime alias, propagates it through dataflow, branch, and fanout edges,
and materializes the manifest from the exact closure one terminal item reached.
Grade, taint, sensitivity, and source coverage are folds over that same closure,
so a run-wide input set can neither over-taint an unrelated output nor let one
item inherit evidence it did not consume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.claim_verdicts import (
    EvidenceEpistemicGrade,
    EvidenceProvenanceGrade,
)

TerminalDependencySlotV1 = Literal[
    "accepted_state",
    "admitted_capture",
    "produced_capture",
    "exhaust",
    "receipt",
    "policy",
]

TerminalItemSelectorPrivacyV1 = Literal["direct_allowed", "pseudonymous_required"]
TerminalItemCoverageDispositionV1 = Literal["consumed", "defaulted", "omitted", "absent"]

#: Derived taint labels.  Each is produced by the closure, never declared by a
#: caller, and each names a real epistemic constraint on the item it marks.
TAINT_ACCEPTED_STATE = "playbill.taint.accepted-state"
TAINT_UNPROMOTED_EXHAUST = "playbill.taint.unpromoted-exhaust"
TAINT_CONSERVATIVE_DEFAULT = "playbill.taint.conservative-default"
TAINT_OMITTED_OPTIONAL = "playbill.taint.omitted-optional"

_ITEM_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,255}$")

_SLOT_FIELDS: dict[TerminalDependencySlotV1, str] = {
    "accepted_state": "accepted_state_input_digests",
    "admitted_capture": "admitted_capture_digests",
    "produced_capture": "produced_capture_digests",
    "exhaust": "exhaust_input_digests",
    "receipt": "receipt_digests",
    "policy": "policy_and_law_digests",
}

_EPISTEMIC_STRENGTH: dict[EvidenceEpistemicGrade, int] = {
    "observed": 2,
    "derived": 1,
    "predicted": 0,
}
_PROVENANCE_STRENGTH: dict[EvidenceProvenanceGrade, int] = {
    "witnessed": 3,
    "provider-signed": 2,
    "daemon-fetched": 1,
    "self-asserted": 0,
}


class _StrictDependencyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _tagged(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a tagged lowercase SHA-256") from exc
    return value


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if values != tuple(sorted(set(values), key=lambda item: item.encode("ascii"))):
        raise ValueError(f"{label} must be sorted and unique")
    for item in values:
        _tagged(item, label=label)
    return values


@dataclass(frozen=True, slots=True)
class DependencyToken:
    """One exact evidence, receipt, or law digest an item actually consumed."""

    slot: TerminalDependencySlotV1
    digest: str


def accepted_state_token(digest: str) -> DependencyToken:
    return DependencyToken(slot="accepted_state", digest=digest)


def admitted_capture_token(digest: str) -> DependencyToken:
    return DependencyToken(slot="admitted_capture", digest=digest)


def produced_capture_token(digest: str) -> DependencyToken:
    return DependencyToken(slot="produced_capture", digest=digest)


def exhaust_token(digest: str) -> DependencyToken:
    return DependencyToken(slot="exhaust", digest=digest)


def receipt_token(digest: str) -> DependencyToken:
    return DependencyToken(slot="receipt", digest=digest)


def policy_token(digest: str) -> DependencyToken:
    return DependencyToken(slot="policy", digest=digest)


@dataclass(frozen=True)
class AliasProvenanceV1:
    """Runtime provenance of one alias: whole-value plus optional per-item closure."""

    whole: frozenset[DependencyToken]
    items: tuple[frozenset[DependencyToken], ...] | None = None

    def item(self, index: int) -> frozenset[DependencyToken]:
        """Return the closure of one item, falling back to the whole-value closure."""

        if self.items is None or index >= len(self.items):
            return self.whole
        return self.whole | self.items[index]

    def merged(self, extra: frozenset[DependencyToken]) -> "AliasProvenanceV1":
        return AliasProvenanceV1(whole=self.whole | extra, items=self.items)


class DependencyEvidenceFactsV1(_StrictDependencyModel):
    """The grade/taint/sensitivity facts one dependency digest contributes."""

    tag: Literal["playbill-dependency-evidence-facts-v1"] = "playbill-dependency-evidence-facts-v1"
    epistemic_grade: EvidenceEpistemicGrade | None = None
    provenance_grade: EvidenceProvenanceGrade | None = None
    taint_labels: tuple[str, ...] = ()
    selector_privacy: TerminalItemSelectorPrivacyV1 | None = None
    acquisition_input_name: str | None = None

    @field_validator("taint_labels")
    @classmethod
    def _labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("dependency taint labels must be sorted and unique")
        return value


class TerminalItemDependencyManifestV1(_StrictDependencyModel):
    """The frozen §8.5.3 per-item closure; every tuple is sorted and unique."""

    tag: Literal["playbill-terminal-item-dependencies-v1"] = (
        "playbill-terminal-item-dependencies-v1"
    )
    run_id: str
    terminal_node_id: str
    item_key: str
    accepted_state_input_digests: tuple[str, ...] = ()
    admitted_capture_digests: tuple[str, ...] = ()
    produced_capture_digests: tuple[str, ...] = ()
    exhaust_input_digests: tuple[str, ...] = ()
    receipt_digests: tuple[str, ...] = ()
    policy_and_law_digests: tuple[str, ...] = ()

    @field_validator("item_key")
    @classmethod
    def _item_key(cls, value: str) -> str:
        if not _ITEM_KEY_RE.fullmatch(value):
            raise ValueError("terminal item_key is not canonical")
        return value

    @field_validator(
        "accepted_state_input_digests",
        "admitted_capture_digests",
        "produced_capture_digests",
        "exhaust_input_digests",
        "receipt_digests",
        "policy_and_law_digests",
    )
    @classmethod
    def _digests(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        return _sorted_unique(value, label=str(getattr(info, "field_name", "dependency digests")))


def terminal_item_manifest_digest(manifest: TerminalItemDependencyManifestV1) -> str:
    payload = manifest.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-terminal-item-dependencies-v1",
        payload,
    ).tagged


def terminal_item_key(*, terminal_node_id: str, child_index: int, item: object) -> str:
    """Address one fanout child by node, deterministic index, and exact content."""

    digest = typed_digest(
        Sha256Value,
        "playbill-terminal-item-key-v1",
        {
            "child_index": child_index,
            "item": item,
            "terminal_node_id": terminal_node_id,
        },
    ).tagged
    return f"{child_index:08d}.{digest.split(':', 1)[1][:32]}"


def build_terminal_item_manifest(
    tokens: frozenset[DependencyToken],
    *,
    run_id: str,
    terminal_node_id: str,
    item_key: str,
) -> TerminalItemDependencyManifestV1:
    """Materialize the manifest from one item's exact dependency closure."""

    grouped: dict[str, list[str]] = {field: [] for field in _SLOT_FIELDS.values()}
    for token in tokens:
        grouped[_SLOT_FIELDS[token.slot]].append(token.digest)
    return TerminalItemDependencyManifestV1(
        run_id=run_id,
        terminal_node_id=terminal_node_id,
        item_key=item_key,
        **{
            field: tuple(sorted(set(values), key=lambda item: item.encode("ascii")))
            for field, values in grouped.items()
        },  # type: ignore[arg-type]
    )


class TerminalItemSourceCoverageV1(_StrictDependencyModel):
    """One declared acquisition input's disposition inside a single item's closure."""

    tag: Literal["playbill-terminal-item-source-coverage-v1"] = (
        "playbill-terminal-item-source-coverage-v1"
    )
    input_name: str
    disposition: TerminalItemCoverageDispositionV1
    capture_digests: tuple[str, ...] = ()

    @field_validator("capture_digests")
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="terminal item coverage capture digests")


class TerminalItemDerivedFactsV1(_StrictDependencyModel):
    """Grade, taint, sensitivity, and coverage folded over one item's closure."""

    tag: Literal["playbill-terminal-item-derived-facts-v1"] = (
        "playbill-terminal-item-derived-facts-v1"
    )
    run_id: str
    terminal_node_id: str
    item_key: str
    child_index: int = Field(ge=0)
    manifest_digest: str
    epistemic_grade: EvidenceEpistemicGrade
    provenance_grade: EvidenceProvenanceGrade
    selector_privacy: TerminalItemSelectorPrivacyV1
    taint_labels: tuple[str, ...] = ()
    source_coverage: tuple[TerminalItemSourceCoverageV1, ...] = ()

    @field_validator("manifest_digest")
    @classmethod
    def _manifest(cls, value: str) -> str:
        return _tagged(value, label="terminal item manifest digest")

    @field_validator("taint_labels")
    @classmethod
    def _labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("terminal item taint labels must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _coverage(self) -> "TerminalItemDerivedFactsV1":
        names = tuple(item.input_name for item in self.source_coverage)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("terminal item source coverage must be sorted and unique")
        return self


class TerminalChildReceiptV1(_StrictDependencyModel):
    """One fanout child's receipt: index order is declared, never completion order."""

    tag: Literal["playbill-terminal-child-receipt-v1"] = "playbill-terminal-child-receipt-v1"
    child_index: int = Field(ge=0)
    item_key: str
    manifest_digest: str
    record_digest: str
    sequence: int = Field(ge=1)

    @field_validator("manifest_digest", "record_digest")
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _tagged(value, label=str(getattr(info, "field_name", "child receipt digest")))


class AcquisitionInputOutcomeV1(_StrictDependencyModel):
    """Run-level disposition of one declared acquisition input, for coverage folding."""

    tag: Literal["playbill-acquisition-input-outcome-v1"] = "playbill-acquisition-input-outcome-v1"
    input_name: str
    disposition: Literal["selected", "omitted", "defaulted", "acquired", "refused"]
    capture_digests: tuple[str, ...] = ()

    @field_validator("capture_digests")
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="acquisition outcome capture digests")


def derive_terminal_item_facts(
    tokens: frozenset[DependencyToken],
    *,
    manifest: TerminalItemDependencyManifestV1,
    child_index: int,
    facts: dict[str, DependencyEvidenceFactsV1],
    outcomes: tuple[AcquisitionInputOutcomeV1, ...] = (),
) -> TerminalItemDerivedFactsV1:
    """Fold grade, taint, sensitivity, and coverage over exactly this item's closure."""

    epistemic: EvidenceEpistemicGrade = "observed"
    provenance: EvidenceProvenanceGrade = "witnessed"
    privacy: TerminalItemSelectorPrivacyV1 = "direct_allowed"
    taint: set[str] = set()
    consumed: dict[str, set[str]] = {}
    for token in sorted(tokens, key=lambda item: (item.slot, item.digest)):
        fact = facts.get(token.digest)
        if fact is None:
            continue
        if fact.epistemic_grade is not None and (
            _EPISTEMIC_STRENGTH[fact.epistemic_grade] < _EPISTEMIC_STRENGTH[epistemic]
        ):
            epistemic = fact.epistemic_grade
        if fact.provenance_grade is not None and (
            _PROVENANCE_STRENGTH[fact.provenance_grade] < _PROVENANCE_STRENGTH[provenance]
        ):
            provenance = fact.provenance_grade
        if fact.selector_privacy == "pseudonymous_required":
            privacy = "pseudonymous_required"
        taint.update(fact.taint_labels)
        if fact.acquisition_input_name is not None and token.slot in {
            "admitted_capture",
            "produced_capture",
        }:
            consumed.setdefault(fact.acquisition_input_name, set()).add(token.digest)
    coverage: list[TerminalItemSourceCoverageV1] = []
    for outcome in outcomes:
        digests = consumed.get(outcome.input_name)
        if digests:
            disposition: TerminalItemCoverageDispositionV1 = "consumed"
        elif outcome.disposition == "defaulted" and TAINT_CONSERVATIVE_DEFAULT in taint:
            disposition = "defaulted"
        elif outcome.disposition == "omitted" and TAINT_OMITTED_OPTIONAL in taint:
            disposition = "omitted"
        else:
            disposition = "absent"
        coverage.append(
            TerminalItemSourceCoverageV1(
                input_name=outcome.input_name,
                disposition=disposition,
                capture_digests=tuple(sorted(digests or (), key=lambda item: item.encode("ascii"))),
            )
        )
    return TerminalItemDerivedFactsV1(
        run_id=manifest.run_id,
        terminal_node_id=manifest.terminal_node_id,
        item_key=manifest.item_key,
        child_index=child_index,
        manifest_digest=terminal_item_manifest_digest(manifest),
        epistemic_grade=epistemic,
        provenance_grade=provenance,
        selector_privacy=privacy,
        taint_labels=tuple(sorted(taint, key=lambda item: item.encode("utf-8"))),
        source_coverage=tuple(
            sorted(coverage, key=lambda item: item.input_name.encode("utf-8")),
        ),
    )


__all__ = [
    "TAINT_ACCEPTED_STATE",
    "TAINT_CONSERVATIVE_DEFAULT",
    "TAINT_OMITTED_OPTIONAL",
    "TAINT_UNPROMOTED_EXHAUST",
    "AcquisitionInputOutcomeV1",
    "AliasProvenanceV1",
    "DependencyEvidenceFactsV1",
    "DependencyToken",
    "TerminalChildReceiptV1",
    "TerminalDependencySlotV1",
    "TerminalItemCoverageDispositionV1",
    "TerminalItemDependencyManifestV1",
    "TerminalItemDerivedFactsV1",
    "TerminalItemSelectorPrivacyV1",
    "TerminalItemSourceCoverageV1",
    "accepted_state_token",
    "admitted_capture_token",
    "build_terminal_item_manifest",
    "derive_terminal_item_facts",
    "exhaust_token",
    "policy_token",
    "produced_capture_token",
    "receipt_token",
    "terminal_item_key",
    "terminal_item_manifest_digest",
]
