"""Durability closure of the changed accepted artifacts, before main publication."""

from __future__ import annotations

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
        envelope = parse_capture_envelope(bodies.read(digest, access=_ACCESS))
        found.update(required_capture_bodies(envelope, digest))
        manifest_digest = envelope.input_receipt_set_manifest_digest
        if manifest_digest is not None:
            manifest_inputs = InputReceiptSetManifest.model_validate_json(
                bodies.read(manifest_digest, access=_ACCESS)
            )
            captures.extend(manifest_inputs.input_capture_digests)
    return tuple(sorted(found))
