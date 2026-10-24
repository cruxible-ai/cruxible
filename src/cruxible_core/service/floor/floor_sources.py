"""``sources/LEDGER``: one line per evidence source, as accepted state alone names it.

A line names the source, the CaptureContract(s) its Captures were taken under,
a ledger-derived locator, how many current Claims cite it, and the generation
it last changed. The locator is the source's Document ref when an accepted
Document carries the source's name (a ledger source, or a foreign source
compiled from the same catalog entry); otherwise every distinct coordinate and
selector type pair its Captures were taken at (a foreign source has none: its
coordinate is a content digest), sorted and comma-separated; and ``-`` when
there is none. Self-source
Captures, the coordinator's record of a Claim's own value, are no evidence
source and are left out. Every accepted Document is a source too, cited or not,
on the one line of its name.

Accepted state is path-free, so no workspace path is here. The client joins its
own catalog's paths in after every apply and writes the agent's view,
``sources/INDEX`` (``write_projection_index``), outside the daemon manifest.

Captures are read by digest from the body store, so what a Capture says about
its source is fixed by its digest (the accepted-body retention invariant). A
cited Capture whose envelope is not retained refuses the render rather than
dropping its source.
"""

from __future__ import annotations

import bisect
import threading
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    FOREIGN_SOURCE_COORDINATE_TYPE,
    capture_contract_digest,
    parse_capture_envelope,
)
from cruxible_client.contracts.captures import (
    CaptureContractAny as CaptureContract,
)
from cruxible_client.contracts.claims import ClaimArtifactAny
from cruxible_client.contracts.errors import CruxibleError, ProjectionIntegrityError
from cruxible_client.contracts.public_names import public_name
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    ExternalSourceReference,
    LedgerSourceReference,
)
from cruxible_core.derived.persistent import EMPTY_PCOUNTS, EMPTY_PSET, PCounts, PMap, PSet
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor_current import stamp_text
from cruxible_core.storage.cas import BodyAccessContext

SOURCES_LEDGER_PATH = "sources/LEDGER"
DOCUMENTS_PREFIX = "documents/"
_SELF_SOURCE_DIGEST = capture_contract_digest(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT).tagged
_ACCESS = BodyAccessContext(principal_id="playbill-floor", can_read_body=True)


@dataclass(frozen=True)
class CaptureSource:
    """What one Capture says about where its evidence came from."""

    contract_digest: str
    source: str
    locator: str


def document_ref(path: str) -> str:
    return "Document:" + path.removeprefix(DOCUMENTS_PREFIX).removesuffix(".json")


def _describe(contract_digest: str, source: object) -> CaptureSource:
    """A Capture's source by its name, and the locator the ledger alone gives it."""

    if isinstance(source, ExternalSourceReference):
        # A foreign source's coordinate is a content digest, which locates
        # nothing; another external source is located by its selector.
        locator = (
            "-"
            if source.coordinate_type == FOREIGN_SOURCE_COORDINATE_TYPE
            else f"{source.coordinate_type}/{source.selector_type}"
        )
        return CaptureSource(contract_digest, source.source_identity, locator)
    if isinstance(source, LedgerSourceReference):
        path = source.address.artifact_path
        if path.startswith(DOCUMENTS_PREFIX):
            ref = document_ref(path)
            return CaptureSource(contract_digest, ref.removeprefix("Document:"), ref)
        return CaptureSource(contract_digest, f"ledger:{path}", f"ledger:{path}")
    if isinstance(source, CasSourceReference):
        return CaptureSource(contract_digest, "cas", "cas")
    return CaptureSource(contract_digest, "unknown", "-")


_CACHE: WeakKeyDictionary[PlaybillInstance, dict[str, CaptureSource | None]] = WeakKeyDictionary()
_CACHE_LOCK = threading.Lock()


def capture_sources(
    instance: PlaybillInstance, digests: Iterable[str]
) -> dict[str, CaptureSource | None]:
    """Each Capture's source, read once per digest.

    A Capture an accepted Claim cites is retained with it; one that is not
    refuses the render with a projection integrity failure.
    """

    digests = tuple(dict.fromkeys(digests))
    with _CACHE_LOCK:
        cache = _CACHE.setdefault(instance, {})
        known = {digest: cache[digest] for digest in digests if digest in cache}
    wanted = [digest for digest in digests if digest not in known]
    store = instance.body_store()
    fresh: dict[str, CaptureSource | None] = {}
    for digest in wanted:
        try:
            envelope = parse_capture_envelope(store.read(digest, access=_ACCESS))
        except (CruxibleError, OSError, ValueError) as exc:
            raise ProjectionIntegrityError(
                f"floor cannot render sources/LEDGER: the cited Capture {digest} is not retained"
            ) from exc
        fresh[digest] = _describe(envelope.capture_contract_digest, envelope.source)
    if fresh:
        with _CACHE_LOCK:
            _CACHE.setdefault(instance, {}).update(fresh)
    return {**known, **fresh}


# One Claim's contribution: its name, its latest sequence, and each (source,
# contract label, locator) its cited Captures give.
SourceContribution = tuple[str, int, tuple[tuple[str, str, str], ...]]


def claim_source_pairs(
    instance: PlaybillInstance,
    claim: ClaimArtifactAny,
    contracts: Mapping[str, CaptureContract],
) -> tuple[tuple[str, str, str], ...]:
    """Each (source, contract label, locator) a live Claim's cited Captures give.

    ``contracts`` maps a contract digest to the accepted contract. Self-source
    Captures, under the coordinator's own contract, name no evidence source.
    """

    return _claim_sources(instance, claim, contracts)[0]


def _claim_sources(
    instance: PlaybillInstance,
    claim: ClaimArtifactAny,
    contracts: Mapping[str, CaptureContract],
) -> tuple[tuple[tuple[str, str, str], ...], frozenset[str]]:
    """The Claim's pairs, and the contract digests its pairs were looked up under.

    The digests are what the pairs depend on besides the immutable Captures:
    an accepted contract at one of them changing is what can relabel them.
    """

    digests = claim.backing.capture_digests
    found = capture_sources(instance, digests)
    pairs: list[tuple[str, str, str]] = []
    looked_up: set[str] = set()
    for digest in digests:
        source = found.get(digest)
        if source is None or source.contract_digest == _SELF_SOURCE_DIGEST:
            continue
        looked_up.add(source.contract_digest)
        contract = contracts.get(source.contract_digest)
        if contract is not None and contract.identity == (
            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity
        ):
            continue
        pairs.append(
            (source.source, "-" if contract is None else contract.identity.name, source.locator)
        )
    return tuple(pairs), frozenset(looked_up)


@dataclass(frozen=True)
class SourceLine:
    """One line's inputs: the Claims citing the source, and its Document."""

    # Citing Claim name -> its latest generation.
    claims: PMap[str, int]
    # (contract label, locator) -> how many (Claim, Capture) pairs give it.
    pairs: PMap[tuple[str, str], int]
    document: str | None = None
    document_latest: int = 0
    # The citing Claims' latest generations, as counts with their maximum.
    latest: PCounts = EMPTY_PCOUNTS

    def row(self, source: str) -> str:
        """The line: the Document of the source's name, else every distinct locator, sorted.

        Sets, never the first one seen, so the line is the same whatever order
        the Claims arrive in. The work is the line's own output: its distinct
        (label, locator) pairs; the citing count and newest generation are
        maintained, not read off the citing Claims.
        """

        # Contract labels are kept as stored; the line shows their public names.
        contracts = sorted({public_name(label) for label, _locator in self.pairs}, key=str.encode)
        located = sorted(
            {locator for _label, locator in self.pairs if locator != "-"}, key=str.encode
        )
        changed = max(self.latest.max(0), self.document_latest)
        return "\t".join(
            (
                source,
                ",".join(contracts) or "-",
                self.document if self.document is not None else ",".join(located) or "-",
                str(len(self.claims)),
                str(changed),
            )
        )


_EMPTY_CLAIMS: PMap[str, int] = PMap()
_EMPTY_PAIRS: PMap[tuple[str, str], int] = PMap()


@dataclass(frozen=True)
class SourcesLedgerState:
    """``sources/LEDGER`` as maintained state: what each live Claim and Document adds.

    Every map is persistent, so a new version shares all untouched entries.
    """

    # Claim path -> its contribution, for the live Claims that cite a source.
    contributions: PMap[str, SourceContribution]
    # Document path -> (its source name, its latest generation).
    documents: PMap[str, tuple[str, int]]
    lines: PMap[str, SourceLine]
    rows: PMap[str, str]
    # The source names, byte-sorted.
    order: tuple[str, ...]
    # Contract digest -> the live Claims whose Captures were taken under it,
    # and each such Claim's digests: what a contract change can relabel.
    by_contract: PMap[str, PSet[str]] = field(default_factory=PMap)
    contracts_of: PMap[str, frozenset[str]] = field(default_factory=PMap)

    def render(self, changed_at: int) -> bytes:
        """The whole file: its bytes are the format's output, one row per source."""

        header = (
            f"# sources LEDGER  {len(self.order)} sources  columns: source, contracts, "
            f"ledger locator, citing claims, changed gen  {stamp_text(changed_at)}"
        )
        return "".join(
            f"{line}\n" for line in (header, *(self.rows[source] for source in self.order))
        ).encode("utf-8")


EMPTY_SOURCES_LEDGER = SourcesLedgerState(PMap(), PMap(), PMap(), PMap(), ())


def _contribution(
    instance: PlaybillInstance,
    claim: ClaimArtifactAny | None,
    latest: int,
    contracts: Mapping[str, CaptureContract],
) -> tuple[SourceContribution | None, frozenset[str]]:
    if claim is None or claim.lifecycle.state != "live":
        return None, frozenset()
    pairs, looked_up = _claim_sources(instance, claim, contracts)
    return (None if not pairs else (claim.identity.name, latest, pairs)), looked_up


def build_sources_ledger(
    instance: PlaybillInstance,
    *,
    claims: Mapping[str, ClaimArtifactAny],
    latest: Mapping[str, int],
    contracts: Mapping[str, CaptureContract],
    documents: Collection[str],
) -> SourcesLedgerState:
    """The state from every Claim (by path) and every accepted Document path."""

    return update_sources_ledger(
        instance,
        EMPTY_SOURCES_LEDGER,
        claims=claims,
        latest=latest,
        contracts=contracts,
        documents=documents,
        changed_claims=claims,
        changed_documents=documents,
    )


def update_sources_ledger(
    instance: PlaybillInstance,
    state: SourcesLedgerState,
    *,
    claims: Mapping[str, ClaimArtifactAny],
    latest: Mapping[str, int],
    contracts: Mapping[str, CaptureContract],
    documents: Collection[str],
    changed_claims: Iterable[str],
    changed_documents: Iterable[str],
    changed_contracts: Iterable[str] = (),
) -> SourcesLedgerState:
    """``state`` with the named Claim and Document paths re-read; only their lines move.

    ``changed_contracts`` are the contract digests whose accepted contract
    came, went or changed since ``state``: the Claims whose Captures were
    taken under one of them are re-read too, since only they can be
    relabelled. Work is the changed paths, those Claims, and the lines they
    reach; nothing else is read or copied.
    """

    contributions = state.contributions.evolver()
    entries = state.documents.evolver()
    contracts_of = state.contracts_of.evolver()
    contract_moves: dict[str, tuple[set[str], set[str]]] = {}
    staged: dict[str, _LineEdit] = {}
    documented: dict[str, tuple[str | None, int]] = {}

    def line(source: str) -> _LineEdit:
        if source not in staged:
            staged[source] = _LineEdit(state.lines.get(source))
        return staged[source]

    reread = dict.fromkeys(changed_claims)
    for digest in changed_contracts:
        reread.update(dict.fromkeys(state.by_contract.get(digest, ())))
    for path in reread:
        old = contributions.pop(path, None)
        new, looked_up = _contribution(instance, claims.get(path), latest.get(path, 0), contracts)
        if new is not None:
            contributions[path] = new
        was = contracts_of.get(path, frozenset())
        if looked_up != was:
            if looked_up:
                contracts_of[path] = looked_up
            else:
                contracts_of.discard(path)
            for digest in was - looked_up:
                contract_moves.setdefault(digest, (set(), set()))[1].add(path)
            for digest in looked_up - was:
                contract_moves.setdefault(digest, (set(), set()))[0].add(path)
        if old == new:
            continue
        if old is not None:
            name, _latest, pairs = old
            for source, label, locator in pairs:
                line(source).drop(name, (label, locator))
        if new is not None:
            name, at, pairs = new
            for source, label, locator in pairs:
                line(source).put(name, at, (label, locator))
    for path in changed_documents:
        old_entry = entries.pop(path, None)
        if old_entry is not None:
            documented.setdefault(old_entry[0], (None, 0))
        if path in documents:
            # A Document is a source of its own name, cited or not.
            source = document_ref(path).removeprefix("Document:")
            entries[path] = (source, latest.get(path, 0))
            documented[source] = (document_ref(path), latest.get(path, 0))
    lines = state.lines.evolver()
    rows = state.rows.evolver()
    added: list[str] = []
    removed: set[str] = set()
    for source in {*staged, *documented}:
        held = lines.get(source)
        if source in staged:
            line_claims = staged[source].citing.persistent()
            line_pairs = staged[source].counted.persistent()
            line_latest = staged[source].latest
        else:
            line_claims = _EMPTY_CLAIMS if held is None else held.claims
            line_pairs = _EMPTY_PAIRS if held is None else held.pairs
            line_latest = EMPTY_PCOUNTS if held is None else held.latest
        document, document_latest = (
            documented[source]
            if source in documented
            else (None, 0)
            if held is None
            else (held.document, held.document_latest)
        )
        if not line_claims and document is None:
            if held is not None:
                lines.discard(source)
                rows.discard(source)
                removed.add(source)
            continue
        if held is None:
            added.append(source)
        current = SourceLine(line_claims, line_pairs, document, document_latest, line_latest)
        lines[source] = current
        rows[source] = current.row(source)
    order = state.order
    if added or removed:
        # Inserting into the sorted names is a copy of them, the same size as
        # the whole-file output a new or dropped source re-renders anyway.
        keys = [name.encode() for name in order if name not in removed]
        names = [name for name in order if name not in removed]
        for source in added:
            at = bisect.bisect(keys, source.encode())
            keys.insert(at, source.encode())
            names.insert(at, source)
        order = tuple(names)
    by_contract = state.by_contract.evolver()
    for digest, (joined, left) in contract_moves.items():
        members = by_contract.get(digest, EMPTY_PSET).update(joined, left)
        if members:
            by_contract[digest] = members
        else:
            by_contract.discard(digest)
    return SourcesLedgerState(
        contributions.persistent(),
        entries.persistent(),
        lines.persistent(),
        rows.persistent(),
        order,
        by_contract.persistent(),
        contracts_of.persistent(),
    )


class _LineEdit:
    """One line's citing Claims, pairs and latest-generation counts, being updated."""

    def __init__(self, held: SourceLine | None) -> None:
        self.citing = (_EMPTY_CLAIMS if held is None else held.claims).evolver()
        self.counted = (_EMPTY_PAIRS if held is None else held.pairs).evolver()
        self.latest = EMPTY_PCOUNTS if held is None else held.latest

    def drop(self, name: str, pair: tuple[str, str]) -> None:
        at = self.citing.pop(name, None)
        if at is not None:
            self.latest = self.latest.remove(at)
        left = self.counted[pair] - 1
        if left:
            self.counted[pair] = left
        else:
            self.counted.discard(pair)

    def put(self, name: str, at: int, pair: tuple[str, str]) -> None:
        before = self.citing.get(name)
        if before is not None:
            self.latest = self.latest.remove(before)
        self.citing[name] = at
        self.latest = self.latest.add(at)
        self.counted[pair] = self.counted.get(pair, 0) + 1


__all__ = [
    "SOURCES_LEDGER_PATH",
    "CaptureSource",
    "EMPTY_SOURCES_LEDGER",
    "SourceContribution",
    "SourceLine",
    "SourcesLedgerState",
    "build_sources_ledger",
    "capture_sources",
    "claim_source_pairs",
    "document_ref",
    "update_sources_ledger",
]
