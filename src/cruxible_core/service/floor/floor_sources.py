"""``sources/INDEX``: one line per evidence source current Claims cite.

A line names the source, the CaptureContract its Captures were taken under, a
locator (the Document a ledger source names, the selector type of an external
source), and how many current Claims cite it. Self-source Captures, the
coordinator's record of a Claim's own value, are no evidence source and are
left out. Every accepted Document is a source too, cited or not, so the
client can bind its workspace files to them (``projections/INDEX``).

Captures are read by digest from the body store, so what a Capture says about
its source is fixed by its digest (the accepted-body retention invariant).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    CaptureContractV1,
    capture_contract_digest,
    parse_capture_envelope,
)
from cruxible_client.contracts.claims import ClaimArtifactAny
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor_current import stamp_text
from cruxible_core.storage.cas import BodyAccessContext

SOURCES_INDEX_PATH = "sources/INDEX"
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
    if isinstance(source, ExternalSourceReferenceV1):
        return CaptureSource(
            contract_digest,
            source.source_identity,
            f"{source.coordinate_type}/{source.selector_type}",
        )
    if isinstance(source, LedgerSourceReferenceV1):
        path = source.address.artifact_path
        ref = document_ref(path) if path.startswith(DOCUMENTS_PREFIX) else f"ledger:{path}"
        return CaptureSource(contract_digest, ref, ref)
    if isinstance(source, CasSourceReferenceV1):
        return CaptureSource(contract_digest, "cas", "cas")
    return CaptureSource(contract_digest, "unknown", "unknown")


_CACHE: WeakKeyDictionary[PlaybillInstance, dict[str, CaptureSource | None]] = WeakKeyDictionary()
_CACHE_LOCK = threading.Lock()


def capture_sources(
    instance: PlaybillInstance, digests: Iterable[str]
) -> dict[str, CaptureSource | None]:
    """Each Capture's source, read once per digest; ``None`` when the body is gone."""

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
        except (PlaybillError, OSError, ValueError):
            # A lost body says nothing about its source; it is not cached, so a
            # restored one is read again.
            known[digest] = None
            continue
        fresh[digest] = _describe(envelope.capture_contract_digest, envelope.source)
    if fresh:
        with _CACHE_LOCK:
            _CACHE.setdefault(instance, {}).update(fresh)
    return {**known, **fresh}


def render_sources_index(
    instance: PlaybillInstance,
    *,
    claims: Iterable[ClaimArtifactAny],
    claim_latest: Mapping[str, int],
    contracts: Mapping[str, CaptureContractV1],
    documents: Mapping[str, int],
    changed_at: int,
) -> bytes:
    """Render ``sources/INDEX`` from the live Claims, the contracts and the Documents.

    ``claim_latest`` maps a Claim name to its latest generation, ``contracts``
    maps a contract digest to the contract, and ``documents`` maps a Document
    path to its latest generation.
    """

    live = [claim for claim in claims if claim.lifecycle.state == "live"]
    sources = capture_sources(
        instance, (digest for claim in live for digest in claim.backing.capture_digests)
    )
    lines: dict[tuple[str, str], tuple[str, set[str], int]] = {}
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
            name = "-" if contract is None else contract.identity.name
            locator, citing, latest = lines.get((found.source, name), (found.locator, set(), 0))
            citing.add(claim.identity.name)
            lines[(found.source, name)] = (
                locator,
                citing,
                max(latest, claim_latest.get(claim.identity.name, 0)),
            )
    for path, latest in documents.items():
        ref = document_ref(path)
        if not any(source == ref for source, _contract in lines):
            lines[(ref, "-")] = (ref, set(), latest)
        else:
            for key in [key for key in lines if key[0] == ref]:
                locator, citing, seen = lines[key]
                lines[key] = (locator, citing, max(seen, latest))
    rows = [
        "\t".join((source, contract, locator, str(len(citing)), str(latest)))
        for (source, contract), (locator, citing, latest) in sorted(
            lines.items(), key=lambda item: (item[0][0].encode(), item[0][1].encode())
        )
    ]
    header = (
        f"# sources INDEX  {len(rows)} sources  columns: source, contract, locator, "
        f"citing claims, changed gen  {stamp_text(changed_at)}"
    )
    return "".join(f"{line}\n" for line in (header, *rows)).encode("utf-8")


__all__ = [
    "SOURCES_INDEX_PATH",
    "CaptureSource",
    "capture_sources",
    "document_ref",
    "render_sources_index",
]
