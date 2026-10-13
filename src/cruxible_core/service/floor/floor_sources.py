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

import threading
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    FOREIGN_SOURCE_COORDINATE_TYPE,
    CaptureContract,
    capture_contract_digest,
    parse_capture_envelope,
)
from cruxible_client.contracts.claims import ClaimArtifactAny
from cruxible_client.contracts.errors import CruxibleError, ProjectionIntegrityError
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    ExternalSourceReference,
    LedgerSourceReference,
)
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

    digests = claim.backing.capture_digests
    found = capture_sources(instance, digests)
    pairs: list[tuple[str, str, str]] = []
    for digest in digests:
        source = found.get(digest)
        if source is None or source.contract_digest == _SELF_SOURCE_DIGEST:
            continue
        contract = contracts.get(source.contract_digest)
        if contract is not None and contract.identity == (
            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity
        ):
            continue
        pairs.append(
            (source.source, "-" if contract is None else contract.identity.name, source.locator)
        )
    return tuple(pairs)


@dataclass(frozen=True)
class SourceLine:
    """One line's inputs: the Claims citing the source, and its Document."""

    # Citing Claim name -> its latest generation.
    claims: Mapping[str, int]
    # (contract label, locator) -> how many (Claim, Capture) pairs give it.
    pairs: Mapping[tuple[str, str], int]
    document: str | None = None
    document_latest: int = 0

    def row(self, source: str) -> str:
        """The line: the Document of the source's name, else every distinct locator, sorted.

        Sets, never the first one seen, so the line is the same whatever order
        the Claims arrive in.
        """

        contracts = sorted({label for label, _locator in self.pairs}, key=str.encode)
        located = sorted(
            {locator for _label, locator in self.pairs if locator != "-"}, key=str.encode
        )
        changed = max(max(self.claims.values(), default=0), self.document_latest)
        return "\t".join(
            (
                source,
                ",".join(contracts) or "-",
                self.document if self.document is not None else ",".join(located) or "-",
                str(len(self.claims)),
                str(changed),
            )
        )


@dataclass(frozen=True)
class SourcesLedgerState:
    """``sources/LEDGER`` as maintained state: what each live Claim and Document adds."""

    # Claim path -> its contribution, for the live Claims that cite a source.
    contributions: Mapping[str, SourceContribution]
    # Document path -> (its source name, its latest generation).
    documents: Mapping[str, tuple[str, int]]
    lines: Mapping[str, SourceLine]
    rows: Mapping[str, str]
    # The source names, byte-sorted.
    order: tuple[str, ...]

    def render(self, changed_at: int) -> bytes:
        header = (
            f"# sources LEDGER  {len(self.order)} sources  columns: source, contracts, "
            f"ledger locator, citing claims, changed gen  {stamp_text(changed_at)}"
        )
        return "".join(
            f"{line}\n" for line in (header, *(self.rows[source] for source in self.order))
        ).encode("utf-8")


def _contribution(
    instance: PlaybillInstance,
    claim: ClaimArtifactAny | None,
    latest: int,
    contracts: Mapping[str, CaptureContract],
) -> SourceContribution | None:
    if claim is None or claim.lifecycle.state != "live":
        return None
    pairs = claim_source_pairs(instance, claim, contracts)
    return None if not pairs else (claim.identity.name, latest, pairs)


def build_sources_ledger(
    instance: PlaybillInstance,
    *,
    claims: Mapping[str, ClaimArtifactAny],
    latest: Mapping[str, int],
    contracts: Mapping[str, CaptureContract],
    documents: Iterable[str],
) -> SourcesLedgerState:
    """The state from every Claim (by path) and every accepted Document path."""

    return update_sources_ledger(
        instance,
        SourcesLedgerState({}, {}, {}, {}, ()),
        claims=claims,
        latest=latest,
        contracts=contracts,
        documents=frozenset(documents),
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
) -> SourcesLedgerState:
    """``state`` with the named Claim and Document paths re-read; only their lines move.

    The contracts must be the ones ``state`` was built under: a contract
    change relabels every line, so the caller builds again instead.
    """

    contributions = dict(state.contributions)
    entries = dict(state.documents)
    staged: dict[str, tuple[dict[str, int], dict[tuple[str, str], int]]] = {}
    documented: dict[str, tuple[str | None, int]] = {}

    def line(source: str) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
        if source not in staged:
            held = state.lines.get(source)
            staged[source] = (
                {} if held is None else dict(held.claims),
                {} if held is None else dict(held.pairs),
            )
        return staged[source]

    for path in changed_claims:
        old = contributions.pop(path, None)
        new = _contribution(instance, claims.get(path), latest.get(path, 0), contracts)
        if new is not None:
            contributions[path] = new
        if old == new:
            continue
        if old is not None:
            name, _latest, pairs = old
            for source, label, locator in pairs:
                citing, counted = line(source)
                citing.pop(name, None)
                counted[(label, locator)] -= 1
                if not counted[(label, locator)]:
                    del counted[(label, locator)]
        if new is not None:
            name, at, pairs = new
            for source, label, locator in pairs:
                citing, counted = line(source)
                citing[name] = at
                counted[(label, locator)] = counted.get((label, locator), 0) + 1
    for path in changed_documents:
        old_entry = entries.pop(path, None)
        if old_entry is not None:
            documented.setdefault(old_entry[0], (None, 0))
        if path in documents:
            # A Document is a source of its own name, cited or not.
            source = document_ref(path).removeprefix("Document:")
            entries[path] = (source, latest.get(path, 0))
            documented[source] = (document_ref(path), latest.get(path, 0))
    lines = dict(state.lines)
    rows = dict(state.rows)
    reorder = False
    for source in {*staged, *documented}:
        held = lines.get(source)
        line_claims: Mapping[str, int]
        line_pairs: Mapping[tuple[str, str], int]
        if source in staged:
            line_claims, line_pairs = staged[source]
        else:
            line_claims = {} if held is None else held.claims
            line_pairs = {} if held is None else held.pairs
        document, document_latest = (
            documented[source]
            if source in documented
            else (None, 0)
            if held is None
            else (held.document, held.document_latest)
        )
        if not line_claims and document is None:
            if lines.pop(source, None) is not None:
                rows.pop(source)
                reorder = True
            continue
        reorder = reorder or held is None
        lines[source] = SourceLine(line_claims, line_pairs, document, document_latest)
        rows[source] = lines[source].row(source)
    order = tuple(sorted(rows, key=str.encode)) if reorder else state.order
    return SourcesLedgerState(contributions, entries, lines, rows, order)


__all__ = [
    "SOURCES_LEDGER_PATH",
    "CaptureSource",
    "SourceContribution",
    "SourceLine",
    "SourcesLedgerState",
    "build_sources_ledger",
    "capture_sources",
    "claim_source_pairs",
    "document_ref",
    "update_sources_ledger",
]
