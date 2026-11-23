"""The two disposable indexes coverage is delivered from.

```
evidence commitment digest                     -> citing Captures / Claims
working source identity + observed occurrence  -> observed commitment digest
```

Neither is an authority and neither is a second compiler. The first is a pure
function of accepted Capture envelopes and accepted Claim backing at exactly one
accepted coordinate -- the read-time consumer of the §11.3 write-time reuse
check, turned around. The second is a pure function of the working snapshot's
bytes. Delete both and rebuild them from the same accepted state and the same
snapshot and every digest reproduces; delete both and never rebuild them and
nothing accepted is lost, because neither ever held anything accepted.

Why exact coverage is not `content_digest -> Claims`
----------------------------------------------------
Identical bytes occur in several sources with entirely different meaning, so the
evidence index alone cannot answer "is this occurrence governed?" It answers
"who cited these bytes," and the overlay answers "where do these bytes currently
sit and under which logical source." Only the resolver's join of the two, over a
matching logical source identity, is `exact`; a join across differing logical
sources is at most a labeled `content_equivalent` candidate.

The scan, and why truncation is honest
--------------------------------------
The accepted-state caller may supply exact selected bytes after verifying them
against an accepted commitment. Those bytes are only a search needle: every
candidate occurrence is still verified byte for byte against the needle that
reproduced the commitment. When retained bytes are not available, commitments
of one length share one exhaustive window-hashing pass. Both routes are bounded
explicitly and admit each source/commitment proof only after its full debit
fits. Skipping work is never silent -- the overlay records the truncation and
withholds the affected proof. Recall shrinks; nothing lies.

What the needle route costs
---------------------------
Every retained needle is searched in every source in one pass per source: one
automaton over all needles' anchors walks the source once, and each anchor hit
is verified against the needles it can start. A source is therefore charged its
length once, however many citations it holds, and the debit for one
source/commitment proof is

* the source's length, on the first needle admitted in that source;
* the needle's length, on the first source that admits that needle (the
  automaton is built once per overlay, not once per source);
* the needle's length per candidate verified in that source.

The debit is a deterministic model of the work the scan stands for, never a
measure of work this process happened to do, so the overlay -- truncation
included -- is a pure function of the snapshot, the wanted set and the budget.

Unchanged sources are not searched again. Occurrences of a needle in a source
are a pure function of the two byte strings, so the per-process memo keyed by
(whole-source digest, needle digest) answers an unchanged file without a pass.
A citation's recorded home offset alone could not do this: it shows the cited
bytes are still there, not that they occur exactly once, and the ordinal and
ambiguity laws below need the exact enumeration.
"""

from __future__ import annotations

import bisect
import functools
import hashlib
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    ExternalSourceReference,
    LedgerSourceReference,
    SourceAccessClass,
    SourceReference,
)
from cruxible_core.coverage.contracts import (
    CoverageClaimCitationV2,
    CoverageCommitmentScanProof,
    CoverageError,
    CoverageLineOverlay,
    LogicalSourceIdentity,
    logical_sources_sorted,
    occurrence_identity_digest,
)
from cruxible_core.indexes.projection import AcceptedCoordinate

EVIDENCE_INDEX_DIGEST_DOMAIN = "playbill-coverage-evidence-index-v1"
EVIDENCE_INDEX_V2_DIGEST_DOMAIN = "playbill-coverage-evidence-index-v2"
OCCURRENCE_OVERLAY_DIGEST_DOMAIN = "playbill-coverage-occurrence-overlay-v1"
OCCURRENCE_OVERLAY_V2_DIGEST_DOMAIN = "playbill-coverage-occurrence-overlay-v2"

DEFAULT_MAX_SCANNED_BYTES = 32 * 1024 * 1024

# How many leading bytes of a needle the shared automaton matches before the
# whole needle is compared. Long enough that anchor hits are nearly always real
# matches in prose; short enough that the automaton stays small.
_NEEDLE_ANCHOR_BYTES = 32
# The memo of complete per-(source content, needle) scans. Bounded in entries
# and in offsets per entry, so a pathological source cannot grow it without
# limit; a result it declines to keep is only searched again.
_SCAN_MEMO_MAX_ENTRIES = 32768
_SCAN_MEMO_MAX_OFFSETS = 4096


class _StrictCoverageIndexModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def accepted_logical_source(source: SourceReference) -> LogicalSourceIdentity | None:
    """Name the logical source an accepted reference cites, when it has one.

    A CAS reference returns ``None`` deliberately: it is addressed by content,
    so it names no source that an edit could relocate content within, and the
    resolver treats a byte match against it as foreign rather than inventing a
    logical source the ledger never recorded.
    """

    if isinstance(source, LedgerSourceReference):
        return LogicalSourceIdentity(plane="ledger", identity=source.address.artifact_path)
    if isinstance(source, ExternalSourceReference):
        return LogicalSourceIdentity(plane="external", identity=source.source_identity)
    if isinstance(source, CasSourceReference):
        return None
    raise CoverageError("unknown source reference kind in coverage index")


# -- reverse evidence index ------------------------------------------------


class EvidenceCitationV1(_StrictCoverageIndexModel):
    """Every accepted citation of one evidence commitment, in one row."""

    tag: Literal["playbill-coverage-evidence-citation-v1"] = (
        "playbill-coverage-evidence-citation-v1"
    )
    commitment_digest: str
    digest_kind: Literal["exact_bytes", "canonical_value", "query_result", "provider_statement"]
    byte_length: int | None = Field(default=None, ge=0)
    accepted_source: LogicalSourceIdentity | None = None
    access_class: SourceAccessClass
    capture_digests: tuple[str, ...] = ()
    claim_addresses: tuple[SemanticAddress, ...] = ()
    dereference_handle_digest: str | None = None

    @field_validator("commitment_digest", "dereference_handle_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("capture_digests")
    @classmethod
    def _captures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("evidence citation capture digests must be sorted and unique")
        return value

    @property
    def dependent_claim_count(self) -> int:
        return len(self.claim_addresses)

    @property
    def sort_key(self) -> tuple[bytes, bytes]:
        return (
            self.commitment_digest.encode("ascii"),
            self.accepted_source.sort_key if self.accepted_source is not None else b"",
        )


class EvidenceCitationV2(EvidenceCitationV1):
    """Every accepted citation of one observed content, in one row.

    Captures that observed the same content -- the same payload bytes -- are one
    row however their envelopes differ; ``commitment_digest`` names that content.
    """

    tag: Literal["playbill-coverage-evidence-citation-v2"] = (
        "playbill-coverage-evidence-citation-v2"  # type: ignore[assignment]
    )
    citation_associations: tuple[CoverageClaimCitationV2, ...] = ()

    @field_validator("citation_associations")
    @classmethod
    def _associations(
        cls,
        value: tuple[CoverageClaimCitationV2, ...],
    ) -> tuple[CoverageClaimCitationV2, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("evidence citation associations must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _association_projection(self) -> "EvidenceCitationV2":
        if not {item.capture_digest for item in self.citation_associations}.issubset(
            self.capture_digests
        ):
            raise ValueError("evidence citation associations must name its Capture set")
        if not {item.claim_address for item in self.citation_associations}.issubset(
            self.claim_addresses
        ):
            raise ValueError("evidence citation associations must name its Claim set")
        return self


class EvidenceCitationIndexV1(_StrictCoverageIndexModel):
    """`evidence commitment digest -> citing Captures/Claims` at one coordinate."""

    tag: Literal["playbill-coverage-evidence-index-v1"] = "playbill-coverage-evidence-index-v1"
    at: AcceptedCoordinate
    citations: tuple[EvidenceCitationV1, ...] = ()
    truncated: bool = False

    @field_validator("citations")
    @classmethod
    def _citations(cls, value: tuple[EvidenceCitationV1, ...]) -> tuple[EvidenceCitationV1, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError(
                "evidence citations must be sorted and unique by commitment and source"
            )
        return value

    def by_commitment(self, commitment_digest: str) -> tuple[EvidenceCitationV1, ...]:
        return tuple(item for item in self.citations if item.commitment_digest == commitment_digest)

    def by_logical_source(self, source: LogicalSourceIdentity) -> tuple[EvidenceCitationV1, ...]:
        key = source.sort_key
        return tuple(
            item
            for item in self.citations
            if item.accepted_source is not None and item.accepted_source.sort_key == key
        )

    def wanted_selections(self) -> tuple[tuple[str, int], ...]:
        """Return the `(digest, byte_length)` pairs a working scan must look for."""

        wanted = {
            (item.commitment_digest, item.byte_length)
            for item in self.citations
            if item.digest_kind == "exact_bytes" and item.byte_length is not None
        }
        return tuple(sorted(wanted))


class EvidenceCitationIndexV2(EvidenceCitationIndexV1):
    tag: Literal["playbill-coverage-evidence-index-v2"] = "playbill-coverage-evidence-index-v2"  # type: ignore[assignment]
    citations: tuple[EvidenceCitationV2, ...] = ()


def evidence_citation_index_digest(
    index: EvidenceCitationIndexV1 | EvidenceCitationIndexV2,
) -> str:
    """Digest the index so a manifest can name exactly which generation it bound."""

    payload = index.model_dump(mode="json")
    payload.pop("tag")
    domain = (
        EVIDENCE_INDEX_V2_DIGEST_DOMAIN
        if isinstance(index, EvidenceCitationIndexV2)
        else EVIDENCE_INDEX_DIGEST_DOMAIN
    )
    return typed_digest(Sha256Value, domain, payload).tagged


# -- working-source occurrence overlay -------------------------------------


@dataclass(frozen=True)
class WorkingSourceContent:
    """One observed working source: its declared logical identity and its bytes.

    The bytes stay out of every model. An overlay is a map of where content sits,
    not a second copy of the content, and a disposable local record that carried
    working bodies would be a body store nobody asked for.
    """

    source: LogicalSourceIdentity
    content: bytes


class CoverageScanBudget(_StrictCoverageIndexModel):
    """The bound on relocation scanning, in bytes fed through SHA-256."""

    tag: Literal["playbill-coverage-scan-budget-v1"] = "playbill-coverage-scan-budget-v1"
    max_scanned_bytes: int = Field(default=DEFAULT_MAX_SCANNED_BYTES, ge=0)


class WorkingSourceCommitmentV1(_StrictCoverageIndexModel):
    """One working source's whole observed content, as a commitment."""

    tag: Literal["playbill-coverage-working-source-commitment-v1"] = (
        "playbill-coverage-working-source-commitment-v1"
    )
    source: LogicalSourceIdentity
    content_digest: str
    byte_length: int = Field(ge=0)

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class WorkingOccurrence(_StrictCoverageIndexModel):
    """One observed occurrence: stable identity, presentation overlay beside it."""

    tag: Literal["playbill-coverage-working-occurrence-v1"] = (
        "playbill-coverage-working-occurrence-v1"
    )
    source: LogicalSourceIdentity
    observed_commitment_digest: str
    byte_length: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    identity_digest: str
    line_overlay: CoverageLineOverlay

    @field_validator("observed_commitment_digest", "identity_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _identity_reproduces(self) -> "WorkingOccurrence":
        expected = occurrence_identity_digest(
            source=self.source,
            observed_commitment_digest=self.observed_commitment_digest,
            ordinal=self.ordinal,
        )
        if expected != self.identity_digest:
            raise ValueError("working occurrence identity does not reproduce from its own fields")
        return self

    @property
    def sort_key(self) -> tuple[bytes, bytes, int]:
        return (
            self.source.sort_key,
            self.observed_commitment_digest.encode("ascii"),
            self.ordinal,
        )


class WorkingOccurrenceOverlayV1(_StrictCoverageIndexModel):
    """`working source identity + observed occurrence -> observed commitment`."""

    tag: Literal["playbill-coverage-occurrence-overlay-v1"] = (
        "playbill-coverage-occurrence-overlay-v1"
    )
    sources: tuple[WorkingSourceCommitmentV1, ...] = ()
    occurrences: tuple[WorkingOccurrence, ...] = ()
    scanned_selections: tuple[str, ...] = ()
    truncated: bool = False
    truncation_reason_codes: tuple[str, ...] = ()

    @field_validator("sources")
    @classmethod
    def _sources(
        cls, value: tuple[WorkingSourceCommitmentV1, ...]
    ) -> tuple[WorkingSourceCommitmentV1, ...]:
        keys = tuple(item.source.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("overlay sources must be sorted and unique by logical source")
        return value

    @field_validator("occurrences")
    @classmethod
    def _occurrences(cls, value: tuple[WorkingOccurrence, ...]) -> tuple[WorkingOccurrence, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("overlay occurrences must be sorted and unique by occurrence identity")
        return value

    @field_validator("scanned_selections", "truncation_reason_codes")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("overlay string sets must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _truncation_is_explained(self) -> "WorkingOccurrenceOverlayV1":
        if self.truncated != bool(self.truncation_reason_codes):
            raise ValueError("overlay truncation must be stated with its reason codes")
        return self

    def commitment_for(self, source: LogicalSourceIdentity) -> WorkingSourceCommitmentV1 | None:
        key = source.sort_key
        for item in self.sources:
            if item.source.sort_key == key:
                return item
        return None

    def occurrences_for(self, source: LogicalSourceIdentity) -> tuple[WorkingOccurrence, ...]:
        key = source.sort_key
        return tuple(item for item in self.occurrences if item.source.sort_key == key)

    def scanned(self, commitment_digest: str) -> bool:
        """Whether absence of this commitment was actually looked for."""

        return commitment_digest in self.scanned_selections

    @property
    def scope(self) -> tuple[LogicalSourceIdentity, ...]:
        return logical_sources_sorted(tuple(item.source for item in self.sources))


class WorkingOccurrenceOverlayV2(_StrictCoverageIndexModel):
    """Per-source complete scan proofs beside disposable source occurrences."""

    tag: Literal["playbill-coverage-occurrence-overlay-v2"] = (
        "playbill-coverage-occurrence-overlay-v2"
    )
    sources: tuple[WorkingSourceCommitmentV1, ...] = ()
    occurrences: tuple[WorkingOccurrence, ...] = ()
    source_scan_proofs: tuple[CoverageCommitmentScanProof, ...] = ()
    truncated: bool = False
    truncation_reason_codes: tuple[str, ...] = ()

    @field_validator("sources")
    @classmethod
    def _sources(
        cls, value: tuple[WorkingSourceCommitmentV1, ...]
    ) -> tuple[WorkingSourceCommitmentV1, ...]:
        keys = tuple(item.source.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("overlay sources must be sorted and unique by logical source")
        return value

    @field_validator("occurrences")
    @classmethod
    def _occurrences(cls, value: tuple[WorkingOccurrence, ...]) -> tuple[WorkingOccurrence, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("overlay occurrences must be sorted and unique by occurrence identity")
        return value

    @field_validator("source_scan_proofs")
    @classmethod
    def _source_scan_proofs(
        cls, value: tuple[CoverageCommitmentScanProof, ...]
    ) -> tuple[CoverageCommitmentScanProof, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("source scan proofs must be sorted and unique")
        return value

    @field_validator("truncation_reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("overlay truncation reasons must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _truncation_is_explained(self) -> "WorkingOccurrenceOverlayV2":
        if self.truncated != bool(self.truncation_reason_codes):
            raise ValueError("overlay truncation must be stated with its reason codes")
        return self

    def commitment_for(self, source: LogicalSourceIdentity) -> WorkingSourceCommitmentV1 | None:
        key = source.sort_key
        for item in self.sources:
            if item.source.sort_key == key:
                return item
        return None

    def occurrences_for(self, source: LogicalSourceIdentity) -> tuple[WorkingOccurrence, ...]:
        key = source.sort_key
        return tuple(item for item in self.occurrences if item.source.sort_key == key)

    def scanned(
        self,
        source: LogicalSourceIdentity,
        commitment_digest: str,
        byte_length: int,
    ) -> bool:
        key = (source.sort_key, commitment_digest.encode("ascii"), byte_length)
        return any(item.sort_key == key for item in self.source_scan_proofs)

    @property
    def scope(self) -> tuple[LogicalSourceIdentity, ...]:
        return logical_sources_sorted(tuple(item.source for item in self.sources))


def _line_starts(content: bytes) -> Sequence[int]:
    starts = [0]
    offset = content.find(b"\n")
    while offset != -1:
        starts.append(offset + 1)
        offset = content.find(b"\n", offset + 1)
    return starts


def _overlay(starts: Sequence[int], *, start_byte: int, end_byte: int) -> CoverageLineOverlay:
    start_line = bisect.bisect_right(starts, start_byte)
    last_byte = end_byte - 1 if end_byte > start_byte else start_byte
    end_line = bisect.bisect_right(starts, last_byte)
    return CoverageLineOverlay(
        start_byte=start_byte,
        end_byte=end_byte,
        start_line=max(start_line, 1),
        end_line=max(end_line, 1),
    )


@dataclass(frozen=True)
class _NeedleScan:
    """Every overlapping occurrence of one needle in one source, in byte order.

    ``candidates`` counts the positions where the needle's anchor matched and the
    whole needle fits, each of which was compared in full: the verification work
    the budget charges for, and itself a pure function of the two byte strings.
    """

    offsets: tuple[int, ...]
    candidates: int


@dataclass(frozen=True)
class _NeedleAutomaton:
    """One compiled pass over a set of needles, shared by every source."""

    pattern: re.Pattern[bytes]
    # needle indexes whose anchor is exactly this anchor
    by_anchor: Mapping[bytes, tuple[int, ...]]
    # every anchor that is a prefix of this anchor, itself included
    prefix_anchors: Mapping[bytes, tuple[bytes, ...]]


_TrieNode = dict[int, "_TrieNode"]


def _anchor(needle: bytes) -> bytes:
    return needle[:_NEEDLE_ANCHOR_BYTES]


def _trie_pattern(anchors: Iterable[bytes]) -> bytes:
    """A regular expression that matches the longest anchor starting at a position.

    The anchors form a trie: siblings start with distinct bytes, so at most one
    branch can proceed, and a node that ends an anchor tries to continue first
    (a greedy optional group). The first match found is therefore the longest
    anchor at that position, and every other anchor matching there is one of its
    prefixes.
    """

    root: _TrieNode = {}
    terminal = -1  # not a byte value, so it never collides with a child
    for anchor in anchors:
        node = root
        for byte in anchor:
            node = node.setdefault(byte, {})
        node[terminal] = {}

    def emit(node: _TrieNode) -> bytes:
        parts = [
            re.escape(bytes((byte,))) + emit(child)
            for byte, child in sorted(node.items())
            if byte != terminal
        ]
        if not parts:
            return b""
        body = parts[0] if len(parts) == 1 else b"(?:" + b"|".join(parts) + b")"
        return b"(?:" + body + b")?" if terminal in node else body

    return emit(root)


@functools.lru_cache(maxsize=16)
def _needle_automaton(needles: tuple[bytes, ...]) -> _NeedleAutomaton:
    anchors = sorted({_anchor(needle) for needle in needles})
    by_anchor: dict[bytes, list[int]] = {}
    for index, needle in enumerate(needles):
        by_anchor.setdefault(_anchor(needle), []).append(index)
    known = set(anchors)
    prefix_anchors = {
        anchor: tuple(anchor[:size] for size in range(1, len(anchor) + 1) if anchor[:size] in known)
        for anchor in anchors
    }
    return _NeedleAutomaton(
        pattern=re.compile(b"(?=(" + _trie_pattern(anchors) + b"))"),
        by_anchor={anchor: tuple(indexes) for anchor, indexes in by_anchor.items()},
        prefix_anchors=prefix_anchors,
    )


def _multi_needle_scan(
    content: bytes,
    needles: tuple[bytes, ...],
    *,
    max_candidates: Sequence[int],
) -> tuple[_NeedleScan | None, ...]:
    """Find every overlapping occurrence of every needle in one pass over ``content``.

    A needle whose candidate count passes its ``max_candidates`` can never be
    admitted by the budget, so its scan stops there and it answers ``None``.
    """

    automaton = _needle_automaton(needles)
    offsets: list[list[int]] = [[] for _ in needles]
    candidates = [0] * len(needles)
    alive = [True] * len(needles)
    remaining_alive = len(needles)
    size = len(content)
    for match in automaton.pattern.finditer(content):
        position = match.start()
        longest = match.group(1)
        for anchor in automaton.prefix_anchors[longest]:
            for index in automaton.by_anchor[anchor]:
                if not alive[index]:
                    continue
                needle = needles[index]
                if position + len(needle) > size:
                    continue
                candidates[index] += 1
                if candidates[index] > max_candidates[index]:
                    alive[index] = False
                    remaining_alive -= 1
                    continue
                if len(needle) == len(anchor) or content.startswith(needle, position):
                    offsets[index].append(position)
        if not remaining_alive:
            break
    return tuple(
        _NeedleScan(offsets=tuple(offsets[index]), candidates=candidates[index])
        if alive[index]
        else None
        for index in range(len(needles))
    )


_scan_memo: OrderedDict[tuple[str, str], _NeedleScan] = OrderedDict()
_scan_memo_lock = threading.Lock()


def _clear_scan_memo() -> None:
    with _scan_memo_lock:
        _scan_memo.clear()


def _needle_scans(
    content: bytes,
    *,
    content_digest: str,
    needles: Mapping[str, bytes],
    remaining: int,
) -> dict[str, _NeedleScan]:
    """Scan one source for every needle that fits in it, answering from the memo first.

    A needle missing from the answer was abandoned because no remaining budget
    could admit it. Only the needles the memo does not hold for this exact
    content are searched, all of them in one pass.
    """

    found: dict[str, _NeedleScan] = {}
    missing: list[str] = []
    with _scan_memo_lock:
        for digest in sorted(needles):
            cached = _scan_memo.get((content_digest, digest))
            if cached is None:
                missing.append(digest)
            else:
                _scan_memo.move_to_end((content_digest, digest))
                found[digest] = cached
    # Admitting any needle here costs the source's length; with less budget than
    # that left, nothing could be admitted, so nothing is searched.
    if not missing or remaining < len(content):
        return found
    ordered = tuple(needles[digest] for digest in missing)
    scanned = _multi_needle_scan(
        content,
        ordered,
        max_candidates=tuple(remaining // len(needle) for needle in ordered),
    )
    with _scan_memo_lock:
        for digest, scan in zip(missing, scanned):
            if scan is None:
                continue
            found[digest] = scan
            if len(scan.offsets) <= _SCAN_MEMO_MAX_OFFSETS:
                _scan_memo[(content_digest, digest)] = scan
                _scan_memo.move_to_end((content_digest, digest))
        while len(_scan_memo) > _SCAN_MEMO_MAX_ENTRIES:
            _scan_memo.popitem(last=False)
    return found


def build_working_occurrence_overlay(
    sources: Iterable[WorkingSourceContent],
    *,
    wanted: Iterable[tuple[str, int, bytes | None]] = (),
    budget: CoverageScanBudget = CoverageScanBudget(),
) -> WorkingOccurrenceOverlayV2:
    """Observe the working snapshot: whole-source content, then cited selections.

    Two passes, both deterministic and both free of a clock:

    1. every working source contributes its whole observed content as one
       occurrence, which is what makes a whole-file citation resolvable and what
       the manifest binds its per-source commitments to;
    2. every wanted `(digest, byte_length, verified_needle)` drawn from accepted
       evidence is exhaustively searched in every named source, by one shared
       overlapping multi-needle pass per source when retained bytes are
       available and by one length-shared digest pass otherwise.

    Ordinals are assigned per `(source, observed digest)` in byte order, so an
    occurrence that is unique in its source keeps ordinal 0 no matter where it
    moves to, and duplicated content produces the several distinct identities the
    resolver needs in order to refuse to bind one of them.
    """

    materials = tuple(sorted(sources, key=lambda item: item.source.sort_key))
    keys = [item.source.sort_key for item in materials]
    if len(set(keys)) != len(keys):
        raise CoverageError("a working snapshot names each logical source at most once")

    wanted_values: dict[tuple[str, int], bytes | None] = {}
    for digest, byte_length, verified_needle in wanted:
        Sha256Value.from_tagged(digest)
        if byte_length < 0:
            raise CoverageError("a wanted selection length must be non-negative")
        if byte_length == 0:
            # The empty string has an occurrence at every byte boundary and
            # therefore cannot be enumerated soundly under the bounded scanner.
            continue
        if verified_needle is not None:
            if len(verified_needle) != byte_length:
                raise CoverageError("a verified needle must have the committed byte length")
            if Sha256Value(hashlib.sha256(verified_needle).hexdigest()).tagged != digest:
                raise CoverageError("a verified needle must reproduce the accepted commitment")
        selection_key = (digest, byte_length)
        if selection_key in wanted_values and wanted_values[selection_key] != verified_needle:
            raise CoverageError("a wanted commitment has conflicting materializations")
        wanted_values[selection_key] = verified_needle

    wanted_by_length: dict[int, tuple[tuple[str, bytes | None], ...]] = {}
    for byte_length in sorted({item[1] for item in wanted_values}):
        wanted_by_length[byte_length] = tuple(
            (digest, wanted_values[(digest, byte_length)])
            for digest, length in sorted(wanted_values)
            if length == byte_length
        )

    commitments: list[WorkingSourceCommitmentV1] = []
    found: dict[tuple[bytes, str], list[tuple[int, int]]] = {}
    line_starts: dict[bytes, Sequence[int]] = {}
    proofs: list[CoverageCommitmentScanProof] = []
    reasons: set[str] = set()
    remaining = budget.max_scanned_bytes

    commitments_by_key: dict[bytes, WorkingSourceCommitmentV1] = {}
    for material in materials:
        content = material.content
        source_key = material.source.sort_key
        line_starts[source_key] = _line_starts(content)
        whole = Sha256Value(hashlib.sha256(content).hexdigest()).tagged
        commitment = WorkingSourceCommitmentV1(
            source=material.source,
            content_digest=whole,
            byte_length=len(content),
        )
        commitments.append(commitment)
        commitments_by_key[source_key] = commitment
        found.setdefault((source_key, whole), []).append((0, len(content)))

    needles = {
        digest: needle for (digest, _length), needle in wanted_values.items() if needle is not None
    }
    charged_needles: set[str] = set()
    for material in materials:
        source_key = material.source.sort_key
        content = material.content
        scans = _needle_scans(
            content,
            content_digest=commitments_by_key[source_key].content_digest,
            needles={
                digest: needle for digest, needle in needles.items() if len(needle) <= len(content)
            },
            remaining=remaining,
        )
        pass_charged = False
        for byte_length in sorted(wanted_by_length):
            selections = wanted_by_length[byte_length]
            if len(content) < byte_length:
                proofs.extend(
                    CoverageCommitmentScanProof(
                        source=material.source,
                        commitment_digest=digest,
                        byte_length=byte_length,
                    )
                    for digest, _ in selections
                )
                continue

            for digest, needle in selections:
                if needle is None:
                    continue
                scan = scans.get(digest)
                if scan is None:
                    reasons.add("scan_budget_exceeded")
                    continue
                debit = (
                    (0 if pass_charged else len(content))
                    + (0 if digest in charged_needles else byte_length)
                    + scan.candidates * byte_length
                )
                if debit > remaining:
                    reasons.add("scan_budget_exceeded")
                    continue
                remaining -= debit
                pass_charged = True
                charged_needles.add(digest)
                found.setdefault((source_key, digest), []).extend(
                    (offset, offset + byte_length) for offset in scan.offsets
                )
                proofs.append(
                    CoverageCommitmentScanProof(
                        source=material.source,
                        commitment_digest=digest,
                        byte_length=byte_length,
                    )
                )

            fallback_digests = {digest for digest, needle in selections if needle is None}
            if not fallback_digests:
                continue
            windows = len(content) - byte_length + 1
            debit = windows * byte_length
            if debit > remaining:
                reasons.add("scan_budget_exceeded")
                continue
            fallback_spans: dict[str, list[tuple[int, int]]] = {
                digest: [] for digest in fallback_digests
            }
            for offset in range(windows):
                window = content[offset : offset + byte_length]
                observed = Sha256Value(hashlib.sha256(window).hexdigest()).tagged
                if observed in fallback_digests:
                    fallback_spans[observed].append((offset, offset + byte_length))
            remaining -= debit
            for digest in sorted(fallback_digests):
                found.setdefault((source_key, digest), []).extend(fallback_spans[digest])
                proofs.append(
                    CoverageCommitmentScanProof(
                        source=material.source,
                        commitment_digest=digest,
                        byte_length=byte_length,
                    )
                )

    by_key = {item.source.sort_key: item.source for item in materials}
    occurrences: list[WorkingOccurrence] = []
    for (source_key, digest), spans in found.items():
        source = by_key[source_key]
        for ordinal, (start_byte, end_byte) in enumerate(sorted(set(spans))):
            occurrences.append(
                WorkingOccurrence(
                    source=source,
                    observed_commitment_digest=digest,
                    byte_length=end_byte - start_byte,
                    ordinal=ordinal,
                    identity_digest=occurrence_identity_digest(
                        source=source,
                        observed_commitment_digest=digest,
                        ordinal=ordinal,
                    ),
                    line_overlay=_overlay(
                        line_starts[source_key], start_byte=start_byte, end_byte=end_byte
                    ),
                )
            )

    return WorkingOccurrenceOverlayV2(
        sources=tuple(sorted(commitments, key=lambda item: item.source.sort_key)),
        occurrences=tuple(sorted(occurrences, key=lambda item: item.sort_key)),
        source_scan_proofs=tuple(sorted(proofs, key=lambda item: item.sort_key)),
        truncated=bool(reasons),
        truncation_reason_codes=byte_sorted(tuple(reasons)),
    )


def working_occurrence_overlay_digest(
    overlay: WorkingOccurrenceOverlayV1 | WorkingOccurrenceOverlayV2,
) -> str:
    """Digest the overlay so a manifest binds the exact snapshot it observed."""

    payload = overlay.model_dump(mode="json")
    payload.pop("tag")
    domain = (
        OCCURRENCE_OVERLAY_V2_DIGEST_DOMAIN
        if isinstance(overlay, WorkingOccurrenceOverlayV2)
        else OCCURRENCE_OVERLAY_DIGEST_DOMAIN
    )
    return typed_digest(Sha256Value, domain, payload).tagged


__all__ = [
    "DEFAULT_MAX_SCANNED_BYTES",
    "EVIDENCE_INDEX_DIGEST_DOMAIN",
    "EVIDENCE_INDEX_V2_DIGEST_DOMAIN",
    "OCCURRENCE_OVERLAY_DIGEST_DOMAIN",
    "OCCURRENCE_OVERLAY_V2_DIGEST_DOMAIN",
    "CoverageScanBudget",
    "EvidenceCitationIndexV1",
    "EvidenceCitationIndexV2",
    "EvidenceCitationV1",
    "EvidenceCitationV2",
    "WorkingOccurrenceOverlayV1",
    "WorkingOccurrenceOverlayV2",
    "WorkingOccurrence",
    "WorkingSourceCommitmentV1",
    "WorkingSourceContent",
    "accepted_logical_source",
    "build_working_occurrence_overlay",
    "evidence_citation_index_digest",
    "working_occurrence_overlay_digest",
]
