"""Per-terminal-item dependency closure derived from actual run dataflow.

A terminal item's dependency manifest is never assembled from a caller-supplied
evidence list.  The executor threads a :class:`DependencyToken` set alongside
every runtime alias, propagates it through dataflow, branch, and fanout edges,
and materializes the manifest from the exact closure one terminal item reached.
Grade, taint, sensitivity, and source coverage are folds over that same closure,
so a run-wide input set can neither over-taint an unrelated output nor let one
item inherit evidence it did not consume.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_verdicts import (
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
    """Runtime provenance of one alias: whole-value plus optional per-item closure.

    ``items_path`` names the list the per-item closures index: a transform's
    or projection's ``items``, or an extract's ``records`` when its records
    come from several Captures (a list's members, D-R37 a5).
    """

    whole: frozenset[DependencyToken]
    items: tuple[frozenset[DependencyToken], ...] | None = None
    items_path: str = "items"

    def item(self, index: int) -> frozenset[DependencyToken]:
        """Return the closure of one item, falling back to the whole-value closure."""

        if self.items is None or index >= len(self.items):
            return self.whole
        return self.whole | self.items[index]

    def merged(self, extra: frozenset[DependencyToken]) -> "AliasProvenanceV1":
        return AliasProvenanceV1(
            whole=self.whole | extra, items=self.items, items_path=self.items_path
        )


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


class TerminalItemDependencyManifestV2(_StrictDependencyModel):
    """One terminal item's §8.5.3 closure, bound to no run.

    The v1 manifest names its run, so no item closure could ever be
    byte-identical to the previous run's and content addressing never applied
    to it. The v2 manifest is a function of the closure alone: the run binding
    is the citing journal record's. Its digest is taken under its own domain;
    a stored v1 digest keeps its v1 rule.
    """

    tag: Literal["playbill-terminal-item-dependencies-v2"] = (
        "playbill-terminal-item-dependencies-v2"
    )
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


#: A terminal item's closure in either stored shape: v1 (run-bound, still read
#: and still the shape of a run's return lineage) or v2 (run-free, written for
#: every terminal item).
TerminalItemDependencyManifest = TerminalItemDependencyManifestV1 | TerminalItemDependencyManifestV2


def terminal_item_manifest_digest(manifest: TerminalItemDependencyManifest) -> str:
    payload = manifest.model_dump(mode="json")
    tag = payload.pop("tag")
    return typed_digest(Sha256Value, tag, payload).tagged


def manifest_dependency_tokens(
    manifest: TerminalItemDependencyManifest,
) -> frozenset[DependencyToken]:
    return frozenset(
        DependencyToken(slot, digest)
        for slot, field in _SLOT_FIELDS.items()
        for digest in getattr(manifest, field)
    )


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

    return TerminalItemDependencyManifestV1(
        run_id=run_id,
        terminal_node_id=terminal_node_id,
        item_key=item_key,
        **_slot_digests(tokens),  # type: ignore[arg-type]
    )


def build_terminal_item_manifest_v2(
    tokens: frozenset[DependencyToken],
    *,
    terminal_node_id: str,
    item_key: str,
) -> TerminalItemDependencyManifestV2:
    """Materialize one terminal item's run-free manifest from its exact closure."""

    return TerminalItemDependencyManifestV2(
        terminal_node_id=terminal_node_id,
        item_key=item_key,
        **_slot_digests(tokens),  # type: ignore[arg-type]
    )


def _slot_digests(tokens: frozenset[DependencyToken]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {field: [] for field in _SLOT_FIELDS.values()}
    for token in tokens:
        grouped[_SLOT_FIELDS[token.slot]].append(token.digest)
    return {
        field: tuple(sorted(set(values), key=lambda item: item.encode("ascii")))
        for field, values in grouped.items()
    }


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


class TerminalItemDerivedFactsV2(_StrictDependencyModel):
    """The folded facts of one v2 (run-free) manifest: v1's fields less ``run_id``."""

    tag: Literal["playbill-terminal-item-derived-facts-v2"] = (
        "playbill-terminal-item-derived-facts-v2"
    )
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
    def _coverage(self) -> "TerminalItemDerivedFactsV2":
        names = tuple(item.input_name for item in self.source_coverage)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("terminal item source coverage must be sorted and unique")
        return self


TerminalItemDerivedFacts = TerminalItemDerivedFactsV1 | TerminalItemDerivedFactsV2


#: The per-item ``item_dependencies`` journal payloads of 0.5.0 development
#: builds: v1 carries the whole closure inline, v2 cites its terminal's shared
#: set. Both are still read (``resolve_item_dependencies``), never written: a
#: terminal now journals one grouped record (``TERMINAL_ITEMS_RECORD_V1``).
ITEM_DEPENDENCIES_RECORD_V1 = "playbill-procedure-item-dependencies-v1"
ITEM_DEPENDENCIES_RECORD_V2 = "playbill-procedure-item-dependencies-v2"


class TerminalDependencyDigestsV1(_StrictDependencyModel):
    """One closure's digests by slot, without the item they belong to.

    The six slots of :class:`TerminalItemDependencyManifestV1`; every tuple is
    sorted and unique.
    """

    accepted_state_input_digests: tuple[str, ...] = ()
    admitted_capture_digests: tuple[str, ...] = ()
    produced_capture_digests: tuple[str, ...] = ()
    exhaust_input_digests: tuple[str, ...] = ()
    receipt_digests: tuple[str, ...] = ()
    policy_and_law_digests: tuple[str, ...] = ()

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


class TerminalSharedCoverageV1(_StrictDependencyModel):
    """The Captures of one acquisition input that every item of a terminal consumed."""

    input_name: str
    capture_digests: tuple[str, ...]

    @field_validator("capture_digests")
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="shared coverage capture digests")


class TerminalSharedDependenciesV1(_StrictDependencyModel):
    """What every item of one terminal consumed: stored once, cited by each item's record.

    A terminal whose items all derive from one list of Captures would
    otherwise restate that list in every item's record; the shared part is
    content-addressed instead, so an item's record grows with what that item
    alone consumed, never with the run's Capture count.
    """

    tag: Literal["playbill-terminal-shared-dependencies-v1"] = (
        "playbill-terminal-shared-dependencies-v1"
    )
    digests: TerminalDependencyDigestsV1
    coverage: tuple[TerminalSharedCoverageV1, ...] = ()

    @model_validator(mode="after")
    def _coverage(self) -> "TerminalSharedDependenciesV1":
        names = tuple(item.input_name for item in self.coverage)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("shared coverage must be sorted and unique")
        return self


class TerminalSharedDependenciesV2(_StrictDependencyModel):
    """The shared set, naming each accepted-state read by its run-free identity.

    v1 names a read by its run input digest, which also commits the read's
    evaluation instant, so an unchanged run on an unchanged generation stored
    a new shared set (listing every fetched Capture) every run. v2 names it by
    ``accepted_read_identity`` (as a derive's manifest v2 does), and the run's
    own items record maps each identity to its run input digest, so the item
    manifests restore exactly. Stored v1 sets stay readable as they are.
    """

    tag: Literal["playbill-terminal-shared-dependencies-v2"] = (
        "playbill-terminal-shared-dependencies-v2"
    )
    digests: TerminalDependencyDigestsV1
    accepted_reads: tuple[str, ...] = Field(min_length=1)
    coverage: tuple[TerminalSharedCoverageV1, ...] = ()

    @field_validator("accepted_reads")
    @classmethod
    def _reads(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="shared accepted-state read identities")

    @model_validator(mode="after")
    def _coverage(self) -> "TerminalSharedDependenciesV2":
        names = tuple(item.input_name for item in self.coverage)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("shared coverage must be sorted and unique")
        return self


class TerminalItemDependencyAuditV1(_StrictDependencyModel):
    deployment_digest: str | None = None
    recorded_at: str


class TerminalItemDependencyRecordV1(_StrictDependencyModel):
    """The inline ``item_dependencies`` payload: the whole closure in every item's record."""

    tag: Literal["playbill-procedure-item-dependencies-v1"] = (
        "playbill-procedure-item-dependencies-v1"
    )
    manifest: TerminalItemDependencyManifestV1
    derived: TerminalItemDerivedFactsV1
    audit: TerminalItemDependencyAuditV1


class TerminalItemDependencyRecordV2(_StrictDependencyModel):
    """One terminal item's ``item_dependencies`` payload, citing its terminal's shared set.

    ``own`` is the item's closure less the shared digests, and ``derived`` is
    its folded facts with each coverage entry less the shared Captures of
    that input. :func:`join_item_dependencies` restores the item's exact
    manifest and facts; the manifest digest is computed as it always was,
    over the whole closure, and must equal ``derived.manifest_digest``.
    """

    tag: Literal["playbill-procedure-item-dependencies-v2"] = (
        "playbill-procedure-item-dependencies-v2"
    )
    shared_digest: str
    own: TerminalDependencyDigestsV1
    derived: TerminalItemDerivedFactsV1
    audit: TerminalItemDependencyAuditV1

    @field_validator("shared_digest")
    @classmethod
    def _shared(cls, value: str) -> str:
        return _tagged(value, label="shared dependencies digest")


def _digest_union(left: tuple[str, ...], right: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({*left, *right}, key=lambda item: item.encode("ascii")))


def shared_terminal_dependencies(
    manifests: Sequence[TerminalItemDependencyManifest],
    derived: Sequence[TerminalItemDerivedFacts],
) -> TerminalSharedDependenciesV1:
    """The digests and coverage every item holds: one pass over the items' own closures."""

    shared: dict[str, set[str]] | None = None
    for manifest in manifests:
        if shared is None:
            shared = {field: set(getattr(manifest, field)) for field in _SLOT_FIELDS.values()}
        else:
            for field, values in shared.items():
                values.intersection_update(getattr(manifest, field))
    coverage: dict[str, set[str]] | None = None
    for facts in derived:
        entries = {item.input_name: item.capture_digests for item in facts.source_coverage}
        if coverage is None:
            coverage = {name: set(digests) for name, digests in entries.items()}
        else:
            for name, digests in coverage.items():
                digests.intersection_update(entries.get(name, ()))
    return TerminalSharedDependenciesV1(
        digests=TerminalDependencyDigestsV1(
            **{
                field: tuple(sorted(values, key=lambda item: item.encode("ascii")))
                for field, values in (shared or {}).items()
            }
        ),
        coverage=tuple(
            TerminalSharedCoverageV1(
                input_name=name,
                capture_digests=tuple(sorted(digests, key=lambda item: item.encode("ascii"))),
            )
            for name, digests in sorted(
                (coverage or {}).items(), key=lambda item: item[0].encode("utf-8")
            )
            if digests
        ),
    )


def render_shared_terminal_dependencies(
    shared: TerminalSharedDependenciesV1 | TerminalSharedDependenciesV2,
) -> bytes:
    """The exact CAS bytes an item record's ``shared_digest`` names."""

    return canonical_bytes(shared.model_dump(mode="json"))


#: The one ``item_dependencies`` payload a terminal journals for all its items.
TERMINAL_ITEMS_RECORD_V1 = "playbill-terminal-items-v1"
#: The same, citing a v2 shared set and mapping each read identity it names to
#: this run's input digest of that read.
TERMINAL_ITEMS_RECORD_V2 = "playbill-terminal-items-v2"
#: One item's closure beyond its terminal's shared set: a run-free CAS body.
ITEM_DEPENDENCIES_BODY_V3 = "playbill-procedure-item-dependencies-v3"
#: An ordered run of up to ``TERMINAL_ITEM_CHUNK_SIZE`` item entries.
TERMINAL_ITEM_CHUNK_V1 = "playbill-terminal-item-chunk-v1"
TERMINAL_ITEM_CHUNK_SIZE = 64


@dataclass(frozen=True)
class TerminalItemsBuildV1:
    """A terminal's grouped item closures: the bodies to land, then the one payload.

    ``bodies`` holds each distinct body once (the shared set, item bodies,
    chunks), in the order they may be stored; ``payload`` is the single
    ``item_dependencies`` record that cites them.
    """

    bodies: tuple[bytes, ...]
    payload: dict[str, object]


def build_terminal_items(
    manifests: Sequence[TerminalItemDependencyManifestV2],
    derived: Sequence[TerminalItemDerivedFactsV2],
    *,
    terminal_node_id: str,
    shared: TerminalSharedDependenciesV1,
    digest: Callable[[bytes], str],
    read_identities: Mapping[str, str] | None = None,
) -> TerminalItemsBuildV1:
    """Group one terminal's item closures into run-free, content-addressed bodies.

    Nothing in a body names the run, its time or a journal position, so an item
    whose closure did not change reproduces the body it had in an earlier run
    and content addressing stores nothing new for it. Each item body holds
    what the item consumed beyond the shared set and its folded facts; a chunk
    lists up to 64 items as ``(item_key, body digest, manifest digest)``; the
    payload names the terminal, the shared set and the chunks in order.

    Each body is built directly as JSON: one pass over the items, with the
    shared set's lookups built once for the terminal (the manifests and facts
    it splits are already validated, and removing digests from a sorted
    unique tuple keeps it sorted and unique).
    """

    held = {field: frozenset(getattr(shared.digests, field)) for field in _SLOT_FIELDS.values()}
    common = {item.input_name: frozenset(item.capture_digests) for item in shared.coverage}
    empty: frozenset[str] = frozenset()
    # Each shared state read with a run-free identity is named by it (v2), and
    # this run's record maps the identity back to its run input digest.
    named = {
        value: read_identities[value]
        for value in shared.digests.accepted_state_input_digests
        if read_identities is not None and value in read_identities
    }
    stored: TerminalSharedDependenciesV1 | TerminalSharedDependenciesV2 = shared
    if named:
        stored = TerminalSharedDependenciesV2(
            digests=shared.digests.model_copy(
                update={
                    "accepted_state_input_digests": tuple(
                        value
                        for value in shared.digests.accepted_state_input_digests
                        if value not in named
                    )
                }
            ),
            accepted_reads=tuple(sorted(set(named.values()), key=lambda item: item.encode())),
            coverage=shared.coverage,
        )
    shared_bytes = render_shared_terminal_dependencies(stored)
    shared_digest = digest(shared_bytes)
    bodies: dict[str, bytes] = {shared_digest: shared_bytes}
    entries: list[list[str]] = []
    for index, (manifest, facts) in enumerate(zip(manifests, derived, strict=True)):
        if (
            manifest.terminal_node_id != terminal_node_id
            or facts.item_key != manifest.item_key
            or facts.child_index != index
        ):
            raise ValueError("terminal items must be one terminal's items in child order")
        body = canonical_bytes(
            {
                "tag": ITEM_DEPENDENCIES_BODY_V3,
                "own": {
                    field: [value for value in getattr(manifest, field) if value not in held[field]]
                    for field in _SLOT_FIELDS.values()
                },
                "facts": {
                    "epistemic_grade": facts.epistemic_grade,
                    "provenance_grade": facts.provenance_grade,
                    "selector_privacy": facts.selector_privacy,
                    "taint_labels": list(facts.taint_labels),
                    "source_coverage": [
                        {
                            "input_name": entry.input_name,
                            "disposition": entry.disposition,
                            "capture_digests": [
                                value
                                for value in entry.capture_digests
                                if value not in common.get(entry.input_name, empty)
                            ],
                        }
                        for entry in facts.source_coverage
                    ],
                },
            }
        )
        body_digest = digest(body)
        bodies.setdefault(body_digest, body)
        entries.append([manifest.item_key, body_digest, facts.manifest_digest])
    chunks: list[str] = []
    for start in range(0, len(entries), TERMINAL_ITEM_CHUNK_SIZE):
        chunk = canonical_bytes(
            {
                "tag": TERMINAL_ITEM_CHUNK_V1,
                "entries": entries[start : start + TERMINAL_ITEM_CHUNK_SIZE],
            }
        )
        chunk_digest = digest(chunk)
        bodies.setdefault(chunk_digest, chunk)
        chunks.append(chunk_digest)
    payload: dict[str, object] = {
        "tag": TERMINAL_ITEMS_RECORD_V2 if named else TERMINAL_ITEMS_RECORD_V1,
        "terminal_node_id": terminal_node_id,
        "shared_digest": shared_digest,
        "count": len(entries),
        "chunks": chunks,
    }
    if named:
        payload["accepted_reads"] = sorted(
            ([identity, value] for value, identity in named.items()),
            key=lambda pair: (pair[0].encode(), pair[1].encode()),
        )
    return TerminalItemsBuildV1(bodies=tuple(bodies.values()), payload=payload)


class _ItemFactsBodyV3(_StrictDependencyModel):
    epistemic_grade: EvidenceEpistemicGrade
    provenance_grade: EvidenceProvenanceGrade
    selector_privacy: TerminalItemSelectorPrivacyV1
    taint_labels: tuple[str, ...] = ()
    source_coverage: tuple[TerminalItemSourceCoverageV1, ...] = ()


class TerminalItemDependencyBodyV3(_StrictDependencyModel):
    """One item's run-free closure body: what it consumed beyond the shared set."""

    tag: Literal["playbill-procedure-item-dependencies-v3"] = (
        "playbill-procedure-item-dependencies-v3"
    )
    own: TerminalDependencyDigestsV1
    facts: _ItemFactsBodyV3


class TerminalItemChunkV1(_StrictDependencyModel):
    tag: Literal["playbill-terminal-item-chunk-v1"] = "playbill-terminal-item-chunk-v1"
    entries: tuple[tuple[str, str, str], ...] = Field(
        min_length=1, max_length=TERMINAL_ITEM_CHUNK_SIZE
    )


class TerminalItemsRecordV1(_StrictDependencyModel):
    """The one ``item_dependencies`` payload of a terminal: shared set and chunks."""

    tag: Literal["playbill-terminal-items-v1"] = "playbill-terminal-items-v1"
    terminal_node_id: str
    shared_digest: str
    count: int = Field(ge=1)
    chunks: tuple[str, ...] = Field(min_length=1)

    @field_validator("shared_digest")
    @classmethod
    def _shared(cls, value: str) -> str:
        return _tagged(value, label="shared dependencies digest")

    @field_validator("chunks")
    @classmethod
    def _chunks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _tagged(item, label="terminal item chunk digest")
        return value


class TerminalItemsRecordV2(TerminalItemsRecordV1):
    """``TerminalItemsRecordV1`` citing a v2 shared set: each read identity it
    names, with this run's input digest of that read."""

    tag: Literal["playbill-terminal-items-v2"] = "playbill-terminal-items-v2"  # type: ignore[assignment]
    accepted_reads: tuple[tuple[str, str], ...] = Field(min_length=1)

    @field_validator("accepted_reads")
    @classmethod
    def _reads(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        if value != tuple(
            sorted(set(value), key=lambda pair: (pair[0].encode(), pair[1].encode()))
        ):
            raise ValueError("accepted read identities must be sorted and unique")
        for identity, digest in value:
            _tagged(identity, label="accepted read identity")
            _tagged(digest, label="accepted-state run input digest")
        return value


def _canonical_body(read_body: Callable[[str], bytes], digest: str, *, label: str) -> object:
    content = read_body(digest)
    value = json.loads(content)
    if canonical_bytes(value) != content:
        raise ValueError(f"a {label} is not canonical")
    return value


def resolve_terminal_items(
    payload: object,
    read_body: Callable[[str], bytes],
) -> tuple[tuple[TerminalItemDependencyManifestV2, TerminalItemDerivedFactsV2], ...]:
    """Every item's exact manifest and facts from a terminal's one grouped record.

    ``read_body`` returns the verified CAS bytes of a digest. Each item's
    manifest is restored as its own digests joined with the shared set, and
    must reproduce the manifest digest its chunk entry names.
    """

    tag = payload.get("tag") if isinstance(payload, Mapping) else None
    record: TerminalItemsRecordV1 = (
        TerminalItemsRecordV2.model_validate(payload)
        if tag == TERMINAL_ITEMS_RECORD_V2
        else TerminalItemsRecordV1.model_validate(payload)
    )
    shared_value = _canonical_body(read_body, record.shared_digest, label="shared dependency set")
    if isinstance(record, TerminalItemsRecordV2):
        stored = TerminalSharedDependenciesV2.model_validate(shared_value)
        mapping = dict(record.accepted_reads)
        if set(mapping) != set(stored.accepted_reads) or len(mapping) != len(record.accepted_reads):
            raise ValueError("a terminal items record maps other reads than its shared set names")
        overlap = set(mapping.values()) & set(stored.digests.accepted_state_input_digests)
        if overlap:
            raise ValueError("a shared set names a read both by identity and by digest")
        shared = TerminalSharedDependenciesV1(
            digests=stored.digests.model_copy(
                update={
                    "accepted_state_input_digests": _digest_union(
                        stored.digests.accepted_state_input_digests, mapping.values()
                    )
                }
            ),
            coverage=stored.coverage,
        )
    else:
        shared = TerminalSharedDependenciesV1.model_validate(shared_value)
    common_coverage = {item.input_name: item.capture_digests for item in shared.coverage}
    restored: list[tuple[TerminalItemDependencyManifestV2, TerminalItemDerivedFactsV2]] = []
    bodies: dict[str, TerminalItemDependencyBodyV3] = {}
    for chunk_digest in record.chunks:
        chunk = TerminalItemChunkV1.model_validate(
            _canonical_body(read_body, chunk_digest, label="terminal item chunk")
        )
        for item_key, body_digest, manifest_digest in chunk.entries:
            body = bodies.get(body_digest)
            if body is None:
                body = TerminalItemDependencyBodyV3.model_validate(
                    _canonical_body(read_body, body_digest, label="terminal item body")
                )
                bodies[body_digest] = body
            fields: dict[str, tuple[str, ...]] = {}
            for field in _SLOT_FIELDS.values():
                own, common = getattr(body.own, field), getattr(shared.digests, field)
                if set(own) & set(common):
                    raise ValueError("an item body restates a shared dependency digest")
                fields[field] = _digest_union(own, common)
            manifest = TerminalItemDependencyManifestV2(
                terminal_node_id=record.terminal_node_id,
                item_key=item_key,
                **fields,  # type: ignore[arg-type]
            )
            if terminal_item_manifest_digest(manifest) != manifest_digest:
                raise ValueError("an item body does not reproduce its manifest digest")
            restored.append(
                (
                    manifest,
                    TerminalItemDerivedFactsV2(
                        terminal_node_id=record.terminal_node_id,
                        item_key=item_key,
                        child_index=len(restored),
                        manifest_digest=manifest_digest,
                        epistemic_grade=body.facts.epistemic_grade,
                        provenance_grade=body.facts.provenance_grade,
                        selector_privacy=body.facts.selector_privacy,
                        taint_labels=body.facts.taint_labels,
                        source_coverage=tuple(
                            TerminalItemSourceCoverageV1(
                                input_name=item.input_name,
                                disposition=item.disposition,
                                capture_digests=_digest_union(
                                    item.capture_digests,
                                    common_coverage.get(item.input_name, ()),
                                ),
                            )
                            for item in body.facts.source_coverage
                        ),
                    ),
                )
            )
    if len(restored) != record.count:
        raise ValueError("a terminal items record's count differs from its chunks")
    return tuple(restored)


def join_item_dependencies(
    record: TerminalItemDependencyRecordV2,
    shared: TerminalSharedDependenciesV1,
) -> tuple[TerminalItemDependencyManifestV1, TerminalItemDerivedFactsV1]:
    """Restore one item's exact manifest and facts; refuse a record the digest disowns."""

    derived = record.derived
    fields: dict[str, tuple[str, ...]] = {}
    for field in _SLOT_FIELDS.values():
        own, common = getattr(record.own, field), getattr(shared.digests, field)
        if set(own) & set(common):
            raise ValueError("an item record restates a shared dependency digest")
        fields[field] = _digest_union(own, common)
    manifest = TerminalItemDependencyManifestV1(
        run_id=derived.run_id,
        terminal_node_id=derived.terminal_node_id,
        item_key=derived.item_key,
        **fields,  # type: ignore[arg-type]
    )
    if terminal_item_manifest_digest(manifest) != derived.manifest_digest:
        raise ValueError("an item record does not reproduce its manifest digest")
    common_coverage = {item.input_name: item.capture_digests for item in shared.coverage}
    restored = derived.model_copy(
        update={
            "source_coverage": tuple(
                TerminalItemSourceCoverageV1(
                    input_name=item.input_name,
                    disposition=item.disposition,
                    capture_digests=_digest_union(
                        item.capture_digests, common_coverage.get(item.input_name, ())
                    ),
                )
                for item in derived.source_coverage
            )
        }
    )
    return manifest, restored


def resolve_item_dependencies(
    payload: object,
    read_body: Callable[[str], bytes],
) -> tuple[TerminalItemDependencyManifestV1, TerminalItemDerivedFactsV1]:
    """One ``item_dependencies`` payload's exact manifest and facts, either shape.

    ``read_body`` returns the verified CAS bytes of a digest (the shared set a
    v2 record cites). A v1 record carries its whole closure inline.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("an item_dependencies payload must be an object")
    tag = payload.get("tag")
    if tag == ITEM_DEPENDENCIES_RECORD_V1:
        inline = TerminalItemDependencyRecordV1.model_validate(payload)
        if terminal_item_manifest_digest(inline.manifest) != inline.derived.manifest_digest:
            raise ValueError("an item record does not reproduce its manifest digest")
        return inline.manifest, inline.derived
    if tag != ITEM_DEPENDENCIES_RECORD_V2:
        raise ValueError(f"unknown item_dependencies payload {tag!r}")
    record = TerminalItemDependencyRecordV2.model_validate(payload)
    content = read_body(record.shared_digest)
    shared = TerminalSharedDependenciesV1.model_validate(json.loads(content))
    if render_shared_terminal_dependencies(shared) != content:
        raise ValueError("a shared dependency set is not canonical")
    return join_item_dependencies(record, shared)


class TerminalChildReceiptV2(_StrictDependencyModel):
    """One fanout child's receipt: index order is declared, never completion order.

    The child names its item and the manifest digest its closure reproduces.
    The terminal's one grouped ``item_dependencies`` record (whose digest the
    terminal's record carries once, ``items_record_digest``) holds the closure;
    v1 children named a per-item record's digest and sequence instead.
    """

    tag: Literal["playbill-terminal-child-receipt-v2"] = "playbill-terminal-child-receipt-v2"
    child_index: int = Field(ge=0)
    item_key: str
    manifest_digest: str
    #: A claims item's change (D-R37 g): its identity, stable across runs
    #: ({node, ClaimType, Subject, unit}), and the kind and digest of the one
    #: canonical change it made. Digests only, never the change body; absent on
    #: every other terminal's items.
    item_identity_digest: str | None = Field(default=None, exclude_if=lambda value: value is None)
    change_kind: Literal["claim", "subject", "reobservation", "retirement"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    change_digest: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("manifest_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _tagged(value, label="child receipt manifest digest")

    @field_validator("item_identity_digest", "change_digest")
    @classmethod
    def _change_digests(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _tagged(value, label=str(getattr(info, "field_name", "child change digest")))
        )

    @model_validator(mode="after")
    def _change(self) -> "TerminalChildReceiptV2":
        missing = {
            self.item_identity_digest is None,
            self.change_kind is None,
            self.change_digest is None,
        }
        if len(missing) != 1:
            raise ValueError("a child's change identity, kind and digest come together")
        return self


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
    manifest: TerminalItemDependencyManifestV2,
    child_index: int,
    facts: dict[str, DependencyEvidenceFactsV1],
    outcomes: tuple[AcquisitionInputOutcomeV1, ...] = (),
) -> TerminalItemDerivedFactsV2:
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
    return TerminalItemDerivedFactsV2(
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


def witnessed_list_capture(
    closure: Collection[str], receipt: object, *, cache: CaptureRootCache | None = None
) -> str | None:
    """The Capture an absence over a Capture list cites, when its witness names the closure.

    Absence over a list is the one Claim closure of several unrelated
    Captures that is admitted (D-R37 a6): its witness cites every member and
    the receipt cites the first. It is admitted only when the witness's
    members are exactly the item's closure and the receipt cites the first;
    ``None`` otherwise.

    ``cache`` (the run's) keeps each answer per distinct closure, witness and
    cited Capture: many absence items on one witness check it once, and each
    further item costs one lookup, as ``capture_root`` does.
    """

    witness = getattr(receipt, "absence", None)
    members = getattr(witness, "members", ())
    cited = getattr(getattr(receipt, "value", None), "capture_digest", None)
    if not members:
        return None
    listed = frozenset(closure)
    key = (listed, tuple(members), cited)
    if cache is not None and key in cache.witnessed:
        return cache.witnessed[key]
    found = (
        str(members[0]) if cited == members[0] and _witness_member_set(members) == listed else None
    )
    if cache is not None:
        cache.witnessed[key] = found
    return found


def _witness_member_set(members: Sequence[str]) -> frozenset[str]:
    """A list witness's members as a set: built once per distinct witness (tests count it)."""

    return frozenset(members)


def citable_captures(manifest: TerminalItemDependencyManifest | None) -> tuple[str, ...]:
    """Every Capture in an item's closure: those it produced AND those it was admitted.

    The item cites the one root of this whole set (``capture_root``, D-R37
    d1). A run fed only a retained Capture (a Line's trigger input) produced
    none, so its admitted Capture is the set. When the closure holds both, an
    admitted Capture is never dropped in favour of a produced one: the root
    must be proven over the union, so an unrelated admitted Capture refuses.
    """

    if manifest is None:
        return ()
    return tuple(
        dict.fromkeys((*manifest.produced_capture_digests, *manifest.admitted_capture_digests))
    )


@dataclass
class CaptureRootCache:
    """Per-run memo for :func:`capture_root`: the proven root per distinct closure.

    A derived Capture's recorded inputs are fixed the moment it is derived
    (D-R37 c1) and never change afterward, so a closure's root stays valid for
    the rest of the run. Sharing one cache across every ``capture_root`` call
    in a run -- across Claims, delivery, and journaling -- means many items
    citing the same closure prove its root once. Only that per-closure result
    is retained: no per-vertex ancestor set is ever materialized.
    """

    roots: dict[frozenset[str], str | None] = dataclass_field(default_factory=dict)
    #: ``witnessed_list_capture``'s answer per (closure, witness members, cited).
    witnessed: dict[tuple[frozenset[str], tuple[str, ...], object], str | None] = dataclass_field(
        default_factory=dict
    )


@dataclass
class _RootProof:
    """One :func:`_prove_root` result and the work it did (tests read the counts)."""

    root: str | None
    #: Recorded input entries read from ``derivations``, across both phases.
    edges_read: int = 0
    #: Peak entries held in the traversal's sets and queue (none is kept after).
    held: int = 0


def _prove_root(listed: frozenset[str], derivations: Mapping[str, Collection[str]]) -> _RootProof:
    """Find and prove the one member of ``listed`` whose recorded inputs reach all the rest.

    Recorded inputs form a DAG (a derived Capture's inputs are digest-fixed
    before it exists), so the root, when there is one, is the only member no
    other member reaches.

    1. **Select.** One traversal from the members' recorded inputs, sharing
       its visited set across members: each node's inputs are read at most
       once, and a member another member reaches is eliminated. It stops as
       soon as one member is left. Every member still in the running reads
       its own inputs before anything deeper is read, widest first, so the
       executor's cumulative manifests (each derive lists every prior input)
       settle on the root's own input list alone.
    2. **Prove.** When the selection's first walk was not the survivor's own,
       a second traversal from the survivor alone confirms it reaches every
       other member, stopping once they are all covered.

    Each phase reads every edge at most once, so the work is linear in the
    edges actually traversed (at worst twice the reachable graph's edges),
    never a product of candidates and ancestors.
    A returned root is always proven by a walk from it alone; the DAG property
    only steers selection, so malformed (cyclic) input can only fail closed.
    """

    proof = _RootProof(root=None)
    needed = len(listed) - 1
    order = sorted(listed, key=lambda digest: (-len(derivations.get(digest, ())), digest))
    visited: set[str] = set()  # reached from some member: never the root
    expanded: set[str] = set()  # inputs already read (at most once each)
    deeper: list[str] = []  # reached, inputs not yet read
    eliminated = 0

    def expand(node: str) -> str | None:
        """Read ``node``'s inputs once; the survivor once only one member is left."""

        nonlocal eliminated
        expanded.add(node)
        for source in derivations.get(node, ()):
            proof.edges_read += 1
            if source in visited:
                continue
            visited.add(source)
            if source in listed:
                eliminated += 1
                if eliminated == needed:
                    break
            if source not in expanded:
                deeper.append(source)
        proof.held = max(proof.held, len(visited) + len(expanded) + len(deeper))
        if eliminated == needed:
            return next(digest for digest in order if digest not in visited)
        return None

    # Every member still in the running reads its own inputs first: a member
    # another member lists directly is eliminated without walking past it.
    survivor: str | None = None
    for position, start in enumerate(order):
        if start in visited:
            continue
        survivor = expand(start)
        if survivor is not None:
            if position == 0 and survivor == start:
                # Nothing else was read: ``start``'s own inputs eliminated
                # every other member, which proves it is the root.
                proof.root = survivor
                return proof
            break
    # Then deeper, most recently reached first, each node read at most once.
    while survivor is None and deeper:
        node = deeper.pop()
        if node not in expanded:
            survivor = expand(node)
    if survivor is None:
        # Two or more members no other reaches (or a cycle eliminated them all).
        return proof

    covered: set[str] = set()
    seen = {survivor}
    walk: deque[str] = deque((survivor,))
    while walk:
        node = walk.popleft()
        for source in derivations.get(node, ()):
            proof.edges_read += 1
            if source in seen:
                continue
            seen.add(source)
            if source in listed:
                covered.add(source)
                if len(covered) == needed:
                    proof.root = survivor
                    proof.held = max(proof.held, len(seen) + len(walk) + len(covered))
                    return proof
            walk.append(source)
        proof.held = max(proof.held, len(seen) + len(walk) + len(covered))
    return proof


def capture_root(
    captures: Iterable[str],
    derivations: Mapping[str, Collection[str]],
    *,
    cache: CaptureRootCache | None = None,
) -> str | None:
    """The one Capture a value cites: the one whose recorded inputs cover the rest (D-R37 d1).

    ``derivations`` maps each derived Capture to the input Captures its
    manifest records. One Capture is its own root. Several have a root only
    when one of them transitively derives from all the others; two unrelated
    Captures have none (combine them through a recorded derive, or a Query).

    The root is selected and proven by one bounded traversal
    (:func:`_prove_root`), linear in the edges it reads. ``cache``, when
    given, retains only the result per distinct closure (D-R37 cost):
    repeating this call for the same closure costs one dict lookup.
    """

    listed = frozenset(captures)
    if len(listed) <= 1:
        return next(iter(listed), None)
    if cache is not None and listed in cache.roots:
        return cache.roots[listed]
    root = _prove_root(listed, derivations).root
    if cache is not None:
        cache.roots[listed] = root
    return root


__all__ = [
    "ITEM_DEPENDENCIES_RECORD_V1",
    "ITEM_DEPENDENCIES_RECORD_V2",
    "CaptureRootCache",
    "TerminalDependencyDigestsV1",
    "TerminalItemDependencyAuditV1",
    "TerminalItemDependencyRecordV1",
    "TerminalItemDependencyRecordV2",
    "TerminalSharedCoverageV1",
    "TerminalSharedDependenciesV1",
    "TerminalSharedDependenciesV2",
    "ITEM_DEPENDENCIES_BODY_V3",
    "TERMINAL_ITEM_CHUNK_SIZE",
    "TERMINAL_ITEM_CHUNK_V1",
    "TERMINAL_ITEMS_RECORD_V1",
    "TerminalItemChunkV1",
    "TerminalItemDependencyBodyV3",
    "TerminalItemDependencyManifest",
    "TerminalItemDependencyManifestV2",
    "TerminalItemDerivedFacts",
    "TerminalItemDerivedFactsV2",
    "TerminalItemsBuildV1",
    "TerminalItemsRecordV1",
    "build_terminal_item_manifest_v2",
    "build_terminal_items",
    "resolve_terminal_items",
    "join_item_dependencies",
    "render_shared_terminal_dependencies",
    "resolve_item_dependencies",
    "shared_terminal_dependencies",
    "capture_root",
    "citable_captures",
    "witnessed_list_capture",
    "TAINT_ACCEPTED_STATE",
    "TAINT_CONSERVATIVE_DEFAULT",
    "TAINT_OMITTED_OPTIONAL",
    "TAINT_UNPROMOTED_EXHAUST",
    "AcquisitionInputOutcomeV1",
    "AliasProvenanceV1",
    "DependencyEvidenceFactsV1",
    "DependencyToken",
    "TerminalChildReceiptV2",
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
