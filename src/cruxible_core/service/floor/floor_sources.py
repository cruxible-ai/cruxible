"""``sources/LEDGER``: one line per evidence source, as accepted state alone names it.

A line names the source, the CaptureContract(s) its Captures were taken under,
a ledger-derived locator, how many current Claims cite it, and the generation
it last changed. The locator is the source's Document ref when an accepted
Document carries the source's name (a ledger source, or a foreign source
compiled from the same catalog entry), an external source's coordinate and
selector types when it is no foreign source, and ``-`` otherwise. Self-source
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    FOREIGN_SOURCE_COORDINATE_TYPE,
    CaptureContractV1,
    capture_contract_digest,
    parse_capture_envelope,
)
from cruxible_client.contracts.claims import ClaimArtifactAny
from cruxible_client.contracts.errors import PlaybillError, ProjectionIntegrityError
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
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

    if isinstance(source, ExternalSourceReferenceV1):
        # A foreign source's coordinate is a content digest, which locates
        # nothing; another external source is located by its selector.
        locator = (
            "-"
            if source.coordinate_type == FOREIGN_SOURCE_COORDINATE_TYPE
            else f"{source.coordinate_type}/{source.selector_type}"
        )
        return CaptureSource(contract_digest, source.source_identity, locator)
    if isinstance(source, LedgerSourceReferenceV1):
        path = source.address.artifact_path
        if path.startswith(DOCUMENTS_PREFIX):
            ref = document_ref(path)
            return CaptureSource(contract_digest, ref.removeprefix("Document:"), ref)
        return CaptureSource(contract_digest, f"ledger:{path}", f"ledger:{path}")
    if isinstance(source, CasSourceReferenceV1):
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
        except (PlaybillError, OSError, ValueError) as exc:
            raise ProjectionIntegrityError(
                f"floor cannot render sources/LEDGER: the cited Capture {digest} is not retained"
            ) from exc
        fresh[digest] = _describe(envelope.capture_contract_digest, envelope.source)
    if fresh:
        with _CACHE_LOCK:
            _CACHE.setdefault(instance, {}).update(fresh)
    return {**known, **fresh}


@dataclass
class _Line:
    contracts: set[str]
    locator: str
    citing: set[str]
    changed: int


def render_sources_ledger(
    instance: PlaybillInstance,
    *,
    claims: Iterable[ClaimArtifactAny],
    claim_latest: Mapping[str, int],
    contracts: Mapping[str, CaptureContractV1],
    documents: Mapping[str, int],
    changed_at: int,
) -> bytes:
    """Render ``sources/LEDGER``: one line per source, from the Claims, contracts and Documents.

    ``claim_latest`` maps a Claim name to its latest generation, ``contracts``
    maps a contract digest to the contract, and ``documents`` maps a Document
    path to its latest generation.
    """

    live = [claim for claim in claims if claim.lifecycle.state == "live"]
    sources = capture_sources(
        instance, (digest for claim in live for digest in claim.backing.capture_digests)
    )
    lines: dict[str, _Line] = {}
    for claim in live:
        for digest in claim.backing.capture_digests:
            found = sources.get(digest)
            if found is None or found.contract_digest == _SELF_SOURCE_DIGEST:
                continue
            contract = contracts.get(found.contract_digest)
            if contract is not None and contract.identity == (
                COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity
            ):
                continue
            line = lines.setdefault(found.source, _Line(set(), found.locator, set(), 0))
            line.contracts.add("-" if contract is None else contract.identity.name)
            if line.locator == "-":
                line.locator = found.locator
            line.citing.add(claim.identity.name)
            line.changed = max(line.changed, claim_latest.get(claim.identity.name, 0))
    for path, latest in documents.items():
        ref = document_ref(path)
        line = lines.setdefault(ref.removeprefix("Document:"), _Line(set(), ref, set(), 0))
        # A Document of the source's own name is where the source lives in the ledger.
        line.locator = ref
        line.changed = max(line.changed, latest)
    rows = [
        "\t".join(
            (
                source,
                ",".join(sorted(line.contracts, key=str.encode)) or "-",
                line.locator,
                str(len(line.citing)),
                str(line.changed),
            )
        )
        for source, line in sorted(lines.items(), key=lambda item: item[0].encode())
    ]
    header = (
        f"# sources LEDGER  {len(rows)} sources  columns: source, contracts, ledger locator, "
        f"citing claims, changed gen  {stamp_text(changed_at)}"
    )
    return "".join(f"{line}\n" for line in (header, *rows)).encode("utf-8")


__all__ = [
    "SOURCES_LEDGER_PATH",
    "CaptureSource",
    "capture_sources",
    "document_ref",
    "render_sources_ledger",
]
