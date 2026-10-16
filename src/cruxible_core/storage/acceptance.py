"""Durability closure of the changed accepted artifacts, before main publication."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping

from cruxible_client.contracts.accepted_attestations import parse_accepted_attestation
from cruxible_client.contracts.captures import (
    InputReceiptSetManifest,
    parse_capture_envelope,
    required_capture_bodies,
)
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.documents import parse_document
from cruxible_core.exhaust.promotions import (
    ExhaustReceiptSetManifestV1,
    parse_exhaust_promotion,
)
from cruxible_core.proposals.settlement import ChangeSetRecordAnyVersion
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore

# These are immutable citation edges, not availability: another store still
# verifies/flushes every pending body. The digest alone names the same content
# across roots. A warm acceptance never re-reads a Capture already interpreted.
_CAPTURE_EDGES: OrderedDict[str, tuple[tuple[str, ...], tuple[str, ...]]] = OrderedDict()
_CAPTURE_EDGES_LOCK = threading.Lock()
_CAPTURE_EDGES_CAPACITY = 65536

_ACCESS = BodyAccessContext(principal_id="acceptance-durability", can_read_body=True)


def cited_bodies(
    record: ChangeSetRecordAnyVersion,
    tree: Mapping[str, bytes],
    bodies: ContentAddressedBodyStore,
) -> tuple[str, ...]:
    """Only changed members and their transitive CAS citations, O(change + closure).

    Typed artifacts distinguish CAS addresses from law, artifact and receipt
    digests. Capture manifests recurse only into input Captures; receipt ids
    themselves name journal records, not CAS objects.
    """
    found: set[str] = set()
    captures: list[str] = []
    for member in record.members:
        path = member.path
        if path not in tree:
            continue
        if path.startswith("claims/"):
            captures.extend(parse_claim(tree[path], path=path).backing.capture_digests)
        elif path.startswith("documents/"):
            found.add(parse_document(tree[path], path=path).body_digest)
        elif path.startswith("attestations/"):
            captures.extend(
                parse_accepted_attestation(tree[path], path=path).statement.cited_capture_digests
            )
        elif path.startswith("exhaust-promotions/"):
            promotion = parse_exhaust_promotion(tree[path], path=path)
            found.update((promotion.output_digest, promotion.receipt_set_manifest_digest))
            manifest = ExhaustReceiptSetManifestV1.model_validate_json(
                bodies.read(promotion.receipt_set_manifest_digest, access=_ACCESS)
            )
            found.update(manifest.payload_digests)
    visited: set[str] = set()
    while captures:
        digest = captures.pop()
        if digest in visited:
            continue
        visited.add(digest)
        with _CAPTURE_EDGES_LOCK:
            edges = _CAPTURE_EDGES.get(digest)
            if edges is not None:
                _CAPTURE_EDGES.move_to_end(digest)
        if edges is None:
            envelope = parse_capture_envelope(bodies.read(digest, access=_ACCESS))
            required = required_capture_bodies(envelope, digest)
            inputs: tuple[str, ...] = ()
            manifest_digest = envelope.input_receipt_set_manifest_digest
            if manifest_digest is not None:
                manifest_inputs = InputReceiptSetManifest.model_validate_json(
                    bodies.read(manifest_digest, access=_ACCESS)
                )
                inputs = manifest_inputs.input_capture_digests
            edges = (required, inputs)
            with _CAPTURE_EDGES_LOCK:
                _CAPTURE_EDGES[digest] = edges
                _CAPTURE_EDGES.move_to_end(digest)
                while len(_CAPTURE_EDGES) > _CAPTURE_EDGES_CAPACITY:
                    _CAPTURE_EDGES.popitem(last=False)
        found.update(edges[0])
        captures.extend(edges[1])
    return tuple(sorted(found))


def flush_acceptance(
    record: ChangeSetRecordAnyVersion,
    tree: Mapping[str, bytes],
    bodies: ContentAddressedBodyStore,
) -> None:
    """The single batched storage barrier immediately before main publication."""
    bodies.flush_cited(cited_bodies(record, tree, bodies))
