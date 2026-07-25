"""Backfilling claim identity onto LEGACY (pre-identity) graph images.

Some images are materialized forever, not once: a pre-migration snapshot can be
cloned or rolled back to years from now, an overlay is created from whatever
release the publisher last cut, and an overlay pulls an upstream that may never
upgrade. Those images carry no ``claim_id``, so every path that materializes one
normalizes it here -- minting the missing ids in memory and persisting them to
live SQLite in the SAME transaction as the graph write they accompany.

Two rules the whole module exists to keep:

* **Artifact bytes are immutable.** Nothing here rewrites ``graph.json``, a
  bundle member, or a snapshot artifact. Pull verification, ``members.json``,
  ``snapshot.json``, and same-release immutability all hash exact bytes; a
  backfill that touched them would break every one of those checks. The image on
  disk keeps its original bytes and its original digest forever.
* **Missing-only.** A post-upgrade image already serializes live ids and those
  are PRESERVED. Re-minting an id that already exists is the failure mode this
  work removes, not one it may reintroduce.

The reconcile map is what bounds churn for the legacy-upstream case. Naive
re-minting would give a NO-OP re-pull of the same pre-upgrade release a fresh id
for every upstream edge, staling every record-time stamp while every recorded
digest stayed byte-identical -- invisible damage. The map records the ids this
instance minted for that upstream's tuples and reuses them, and the normalized
map digest recorded on the upstream metadata makes the churn visible on the
occasions it genuinely happens.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.primitives import canonical_json

LegacyIdentityKey = tuple[str, str, str, str, str]


def load_legacy_identity_map(raw: Any) -> dict[LegacyIdentityKey, str]:
    """Decode the persisted reconcile map; tolerate absent/garbage as empty."""
    entries: dict[LegacyIdentityKey, str] = {}
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            key = (
                str(item["relationship_type"]),
                str(item["from_type"]),
                str(item["from_id"]),
                str(item["to_type"]),
                str(item["to_id"]),
            )
            claim_id = str(item["claim_id"])
        except (KeyError, TypeError):
            continue
        if claim_id:
            entries[key] = claim_id
    return entries


def dump_legacy_identity_map(entries: dict[LegacyIdentityKey, str]) -> list[dict[str, str]]:
    """Encode the reconcile map in a deterministic, sorted, JSON-safe shape."""
    return [
        {
            "relationship_type": key[0],
            "from_type": key[1],
            "from_id": key[2],
            "to_type": key[3],
            "to_id": key[4],
            "claim_id": claim_id,
        }
        for key, claim_id in sorted(entries.items())
    ]


def legacy_identity_map_digest(entries: dict[LegacyIdentityKey, str]) -> str:
    """Digest the normalized reconcile map so id churn is VISIBLE when it happens.

    Recorded on the upstream metadata beside the member digests: two pulls of
    the same release whose upstream ids moved produce different digests here
    even though every content digest is identical.
    """
    payload = canonical_json(dump_legacy_identity_map(entries)).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def record_minted_identities(
    entries: dict[LegacyIdentityKey, str],
    minted: Sequence[RelationshipInstance],
) -> dict[LegacyIdentityKey, str]:
    """Fold freshly minted legacy ids into the reconcile map."""
    updated = dict(entries)
    for relationship in minted:
        claim_id = relationship.claim_id
        if claim_id is None:
            continue
        updated[
            (
                relationship.relationship_type,
                relationship.from_type,
                relationship.from_id,
                relationship.to_type,
                relationship.to_id,
            )
        ] = claim_id
    return updated


def backfill_legacy_graph(
    graph: EntityGraph,
    *,
    reuse: dict[LegacyIdentityKey, str] | None = None,
) -> list[RelationshipInstance]:
    """Mint the missing claim ids on one materialized legacy image, in memory.

    Thin, NAMED wrapper over ``EntityGraph.backfill_missing_claim_ids`` so the
    legacy-materialization sites are greppable as a set and so the
    artifact-immutability rule has one documented home.
    """
    return graph.backfill_missing_claim_ids(reuse=reuse)


__all__ = [
    "LegacyIdentityKey",
    "backfill_legacy_graph",
    "dump_legacy_identity_map",
    "legacy_identity_map_digest",
    "load_legacy_identity_map",
    "record_minted_identities",
]
