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
from collections import deque
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
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


def terminal_item_manifest_digest(manifest: TerminalItemDependencyManifestV1) -> str:
    payload = manifest.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-terminal-item-dependencies-v1",
        payload,
    ).tagged


def manifest_dependency_tokens(
    manifest: TerminalItemDependencyManifestV1,
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


def witnessed_list_capture(closure: Collection[str], receipt: object) -> str | None:
    """The Capture an absence over a Capture list cites, when its witness names the closure.

    Absence over a list is the one Claim closure of several unrelated
    Captures that is admitted (D-R37 a6): its witness cites every member and
    the receipt cites the first. It is admitted only when the witness's
    members are exactly the item's closure and the receipt cites the first;
    ``None`` otherwise.
    """

    witness = getattr(receipt, "absence", None)
    members = getattr(witness, "members", ())
    cited = getattr(getattr(receipt, "value", None), "capture_digest", None)
    if not members or set(members) != set(closure) or cited != members[0]:
        return None
    return str(members[0])


def citable_captures(manifest: TerminalItemDependencyManifestV1 | None) -> tuple[str, ...]:
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
    "CaptureRootCache",
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
