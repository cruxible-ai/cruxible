"""Shared output-profile serializer for entity-shaped read payloads.

One module owns the ``compact | standard | full`` output profiles consumed by
query rows (``dump_query_row``), inspect, ``get_entity``, edge list payloads,
and the CLI JSON helpers. It lives in ``cruxible_core.query`` because the query
package already owns row serialization (``dump_query_row``) and is importable
from every consumer layer (service, runtime API, server routes, CLI) without
creating an import cycle.

Design rules:

* **standard** is today's shape, bit-for-bit. Every function returns its input
  object UNCHANGED (the same object, not a copy) for ``standard``/``full`` so
  existing consumers cannot observe any difference.
* **full** is an alias of standard today. The tier exists so later heavy
  expansions (evidence dereference, lineage attachments) can hang off it while
  preserving the ``full ⊇ standard`` contract; it is deliberately NOT a copy of
  the standard branch.
* **compact** is an identity card built from the standard serialized dict:
  entity identity plus bounded display properties and the governance markers
  that MUST survive (entity ``metadata.lifecycle``; edge
  ``metadata.assertion`` review + lifecycle). No ``actor_context``, no
  provenance blobs, no full property bags.
* Envelope fields (``total``/``limit``/``offset``/``truncated``/counts and
  truncation flags) are NEVER trimmed by any profile: the transforms here only
  touch item payloads, never envelopes.

Compact property selection: the well-known display keys
(``name``/``title``/``label``/``summary``/``status``) are kept when present;
when NONE of them are present, the first ``COMPACT_MAX_SCALAR_PROPERTIES`` (5)
scalar-valued properties in SORTED-KEY order are kept instead. Sorted-key order
is deliberate: persistence canonicalizes property JSON with sorted keys
(``storage/sqlite.py`` via ``primitives.canonical_json``) while fresh writes
keep the caller's in-memory insertion order in the graph cache, so any
insertion-order-dependent selection would pick a DIFFERENT five for the same
entity before vs after a restart. Sorting the keys before selecting makes the
compact card a pure function of the property set, matching the persistence
canonicalization.
"""

from __future__ import annotations

from typing import Any, Literal

ReadProfile = Literal["compact", "standard", "full"]

READ_PROFILES: tuple[ReadProfile, ...] = ("compact", "standard", "full")

# Well-known display properties preferred by the compact identity card.
COMPACT_DISPLAY_PROPERTY_KEYS: tuple[str, ...] = (
    "name",
    "title",
    "label",
    "summary",
    "status",
)

# Fallback bound when no display key is present: keep the first N scalar
# properties in sorted-key order (see module docstring — insertion order is NOT
# stable across a persistence round-trip). N=5 keeps the card useful without
# re-growing the property bag compact exists to drop.
COMPACT_MAX_SCALAR_PROPERTIES = 5


def compact_display_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded display-property slice of a full property bag.

    Deterministic by construction: the display branch follows the fixed
    ``COMPACT_DISPLAY_PROPERTY_KEYS`` order and the scalar fallback walks the
    keys in sorted order, so the same property SET always yields the same
    compact card regardless of dict insertion order (in-memory graph cache vs
    the sorted-key canonical JSON restored from storage).
    """
    display = {key: properties[key] for key in COMPACT_DISPLAY_PROPERTY_KEYS if key in properties}
    if display:
        return display
    scalars: dict[str, Any] = {}
    for key in sorted(properties):
        value = properties[key]
        if value is None or isinstance(value, (str, int, float, bool)):
            scalars[key] = value
            if len(scalars) >= COMPACT_MAX_SCALAR_PROPERTIES:
                break
    return scalars


def _compact_entity_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep only the entity lifecycle governance marker; drop null subfields."""
    lifecycle = metadata.get("lifecycle")
    if not lifecycle:
        return {}
    return {"lifecycle": {key: value for key, value in lifecycle.items() if value is not None}}


def _compact_edge_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep the edge review/lifecycle governance markers from ``assertion``.

    Drops ``provenance`` and ``evidence`` blobs entirely, and the reviewer
    ``actor_context`` plus null subfields inside the retained assertion slices.
    """
    assertion = metadata.get("assertion")
    if not assertion:
        return {}
    compact_assertion: dict[str, Any] = {}
    review = assertion.get("review")
    if review:
        compact_review = {
            key: value
            for key, value in review.items()
            if key != "actor_context" and value is not None
        }
        if compact_review:
            compact_assertion["review"] = compact_review
    lifecycle = assertion.get("lifecycle")
    if lifecycle:
        compact_lifecycle = {key: value for key, value in lifecycle.items() if value is not None}
        if compact_lifecycle:
            compact_assertion["lifecycle"] = compact_lifecycle
    if assertion.get("group_override"):
        compact_assertion["group_override"] = assertion["group_override"]
    if not compact_assertion:
        return {}
    return {"assertion": compact_assertion}


def profile_entity_payload(payload: dict[str, Any], profile: ReadProfile) -> dict[str, Any]:
    """Profile one serialized entity payload (``EntityInstance``-shaped dict)."""
    if profile != "compact" or not payload:
        return payload
    return {
        "entity_type": payload.get("entity_type"),
        "entity_id": payload.get("entity_id"),
        "properties": compact_display_properties(payload.get("properties") or {}),
        "metadata": _compact_entity_metadata(payload.get("metadata") or {}),
    }


def profile_edge_payload(payload: dict[str, Any], profile: ReadProfile) -> dict[str, Any]:
    """Profile one serialized edge payload (``RelationshipInstance``-shaped dict).

    Compact keeps the relationship identity (type, endpoints, ``edge_key``),
    the full ``properties`` bag (edge properties ARE the assertion payload),
    and the review/lifecycle markers; key order follows the input payload.
    """
    if profile != "compact":
        return payload
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key in ("relationship_type", "from_type", "from_id", "to_type", "to_id", "edge_key"):
            compact[key] = value
        elif key == "properties":
            compact[key] = dict(value or {})
        elif key == "metadata":
            compact[key] = _compact_edge_metadata(value or {})
        elif key == "alias":
            compact[key] = value
        elif key == "corroboration":
            compact[key] = value
    return compact


def _profile_include_result(payload: dict[str, Any], profile: ReadProfile) -> dict[str, Any]:
    """Profile one include-result; counts and truncation flags pass through."""
    if profile != "compact":
        return payload
    compact = dict(payload)
    compact["items"] = [
        {
            "edge": profile_edge_payload(item.get("edge") or {}, profile),
            "source": profile_entity_payload(item.get("source") or {}, profile),
            "target": profile_entity_payload(item.get("target") or {}, profile),
        }
        for item in payload.get("items") or []
    ]
    return compact


def _profile_includes(payload: dict[str, Any], profile: ReadProfile) -> dict[str, Any]:
    return {
        alias: _profile_include_result(include, profile)
        for alias, include in (payload.get("includes") or {}).items()
    }


def profile_query_item(payload: dict[str, Any], profile: ReadProfile) -> dict[str, Any]:
    """Profile one serialized query row of any result shape."""
    if profile != "compact":
        return payload
    if "values" in payload and "entity_type" not in payload:
        # Projected row: `values` is already a bounded caller-chosen projection;
        # only the preserved source evidence row is compacted.
        compact = dict(payload)
        source = compact.get("source")
        if source is not None:
            compact["source"] = profile_query_item(source, profile)
        return compact
    if "relationship_type" in payload:
        compact = profile_edge_payload(payload, profile)
        if "entry" in payload:
            compact["entry"] = profile_entity_payload(payload["entry"], profile)
        for endpoint_key in ("from_entity", "to_entity"):
            if endpoint_key in payload:
                endpoint = payload[endpoint_key]
                compact[endpoint_key] = (
                    profile_entity_payload(endpoint, profile) if endpoint is not None else None
                )
        if "includes" in payload:
            compact["includes"] = _profile_includes(payload, profile)
        return compact
    if "entry" in payload and "result" in payload:
        return {
            "entry": profile_entity_payload(payload["entry"], profile),
            "result": profile_entity_payload(payload["result"], profile),
            "entities": [
                profile_entity_payload(entity, profile) for entity in payload.get("entities") or []
            ],
            "path": [
                profile_edge_payload(segment, profile) for segment in payload.get("path") or []
            ],
            "includes": _profile_includes(payload, profile),
        }
    if "entity_type" in payload:
        return profile_entity_payload(payload, profile)
    return payload


def profile_query_items(
    items: list[dict[str, Any]],
    profile: ReadProfile,
) -> list[dict[str, Any]]:
    """Profile a list of serialized query rows."""
    if profile != "compact":
        return items
    return [profile_query_item(item, profile) for item in items]


def inspect_neighbor_payload(
    *,
    direction: str,
    relationship_type: str,
    edge_key: int | None,
    properties: dict[str, Any],
    metadata: dict[str, Any],
    entity: dict[str, Any],
    profile: ReadProfile = "standard",
) -> dict[str, Any]:
    """Build one inspect neighbor row (the single local/remote assembly point).

    Both the CLI local and remote inspect paths previously hand-built this dict;
    they now both route here so the key order and profile trimming cannot drift.
    """
    row = {
        "direction": direction,
        "relationship_type": relationship_type,
        "edge_key": edge_key,
        "properties": properties,
        "metadata": metadata,
        "entity": entity,
    }
    return profile_inspect_neighbor(row, profile)


def profile_inspect_neighbor(payload: dict[str, Any], profile: ReadProfile) -> dict[str, Any]:
    """Profile one serialized inspect neighbor row."""
    if profile != "compact":
        return payload
    return {
        "direction": payload.get("direction"),
        "relationship_type": payload.get("relationship_type"),
        "edge_key": payload.get("edge_key"),
        "properties": dict(payload.get("properties") or {}),
        "metadata": _compact_edge_metadata(payload.get("metadata") or {}),
        **({"corroboration": payload["corroboration"]} if "corroboration" in payload else {}),
        "entity": profile_entity_payload(payload.get("entity") or {}, profile),
    }


def profile_inspect_payload(payload: dict[str, Any], profile: ReadProfile) -> dict[str, Any]:
    """Profile a full inspect-entity payload; ``found``/counts pass through."""
    if profile != "compact":
        return payload
    compact = dict(payload)
    compact["properties"] = compact_display_properties(payload.get("properties") or {})
    compact["metadata"] = _compact_entity_metadata(payload.get("metadata") or {})
    compact["neighbors"] = [
        profile_inspect_neighbor(neighbor, profile) for neighbor in payload.get("neighbors") or []
    ]
    return compact


def neighborhood_node_payload(
    *,
    entity: dict[str, Any],
    depth: int,
    projection: list[str] | None = None,
    profile: ReadProfile = "standard",
) -> dict[str, Any]:
    """Build one neighborhood node card (the single local/remote assembly point).

    Composition rule: ``projection`` selects PROPERTIES (caller-chosen names,
    in caller order, silently omitting names the entity does not carry);
    ``profile`` shapes METADATA (compact keeps only the lifecycle governance
    marker). When both are given, projection wins for ``properties`` and the
    profile still shapes ``metadata`` — identity and lifecycle/review markers
    always survive.
    """
    source_properties = entity.get("properties") or {}
    metadata = entity.get("metadata") or {}
    if projection is not None:
        properties = {
            name: source_properties[name] for name in projection if name in source_properties
        }
        shaped_metadata = _compact_entity_metadata(metadata) if profile == "compact" else metadata
    else:
        shaped = profile_entity_payload(entity, profile)
        properties = dict(shaped.get("properties") or {})
        shaped_metadata = shaped.get("metadata") or {}
    return {
        "entity_type": entity.get("entity_type"),
        "entity_id": entity.get("entity_id"),
        "depth": depth,
        "properties": properties,
        "metadata": shaped_metadata,
    }


def neighborhood_edge_payload(
    edge: dict[str, Any],
    profile: ReadProfile = "standard",
) -> dict[str, Any]:
    """Build one neighborhood edge row in canonical key order, then profile it.

    Compact keeps the full edge identity, the edge properties (they ARE the
    assertion payload), and the review/lifecycle markers — pending, rejected,
    and superseded edges never flatten into live ones.
    """
    payload = {
        "relationship_type": edge.get("relationship_type"),
        "from_type": edge.get("from_type"),
        "from_id": edge.get("from_id"),
        "to_type": edge.get("to_type"),
        "to_id": edge.get("to_id"),
        "edge_key": edge.get("edge_key"),
        "properties": dict(edge.get("properties") or {}),
        "metadata": edge.get("metadata") or {},
        **({"corroboration": edge["corroboration"]} if "corroboration" in edge else {}),
    }
    return profile_edge_payload(payload, profile)


def profile_get_entity_payload(payload: dict[str, Any], profile: ReadProfile) -> dict[str, Any]:
    """Profile a get-entity payload; ``found`` passes through."""
    if profile != "compact":
        return payload
    compact = dict(payload)
    if "properties" in compact:
        compact["properties"] = compact_display_properties(payload.get("properties") or {})
    if "metadata" in compact:
        compact["metadata"] = _compact_entity_metadata(payload.get("metadata") or {})
    return compact


def profile_list_items(
    items: list[Any],
    resource_type: str,
    profile: ReadProfile,
) -> list[Any]:
    """Profile serialized list items; only entities and edges are entity-shaped.

    Receipts, feedback, and outcomes are audit/event documents, not
    entity-shaped payloads: profiles pass them through unchanged.
    """
    if profile != "compact":
        return items
    if resource_type == "entities":
        return [profile_entity_payload(item, profile) for item in items]
    if resource_type == "edges":
        return [profile_edge_payload(item, profile) for item in items]
    return items


__all__ = [
    "COMPACT_DISPLAY_PROPERTY_KEYS",
    "COMPACT_MAX_SCALAR_PROPERTIES",
    "READ_PROFILES",
    "ReadProfile",
    "compact_display_properties",
    "inspect_neighbor_payload",
    "neighborhood_edge_payload",
    "neighborhood_node_payload",
    "profile_edge_payload",
    "profile_entity_payload",
    "profile_get_entity_payload",
    "profile_inspect_neighbor",
    "profile_inspect_payload",
    "profile_list_items",
    "profile_query_item",
    "profile_query_items",
]
