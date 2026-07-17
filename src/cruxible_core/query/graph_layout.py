"""Normalized graph transport for query output (``layout="graph"``).

Transforms ALREADY-BUILT serialized query rows — post-filter, post-paginate,
post-profile — into the normalized nodes/edges wire representation shared with
the bounded-neighborhood inspect contract. Operating on the serialized dicts
(rather than the engine row models) is deliberate:

* profile composition is free — the rows were already shaped by
  ``cruxible_core.query.profiles``, so every node/edge card here is exactly the
  payload the rows layout would have carried (governance markers included);
* the CLI local and remote emit paths normalize the identical dicts, so key
  order and trimming cannot drift between modes;
* losslessness is structural — node cards are the row payloads themselves and
  edge cards are the row payloads minus their per-occurrence ``alias`` key,
  deduped, never re-serialized through a second shaping pass.

Identity rules:

* node identity = ``(entity_type, entity_id)``; first occurrence wins and rows
  within one result set serialize the same entity identically.
* edge identity = PHYSICAL relationship identity only (``relationship_type`` +
  endpoints + ``edge_key``, ``None``-safe). The traversal-step ``alias`` is
  per-occurrence metadata, not identity: a physical edge visited under two
  different step aliases is ONE ``edges[]`` card. Cards therefore never carry
  an ``alias`` key — the alias lives on the REFERENCES:

  - each ``paths[]`` entry is a list of step refs ``{"edge": <index>,
    "alias": <alias-or-null>}`` (the alias the traversal attached to that
    occurrence);
  - each include item ref is ``{"edge": <index>, "alias": <alias-or-null>,
    "source": <index>, "target": <index>}``.

  Relationship-shaped result refs stay bare ``edge`` indexes: relationship
  rows carry no alias in the rows layout, so there is nothing to restore.
  Reconstruction of a rows-layout segment is exactly ``{**edges[ref["edge"]],
  "alias": ref["alias"]}``.
* path identity = its step-ref sequence (edge index + alias per step);
  identical sequences dedupe to one ``paths[]`` entry. ``dedupe=path`` rows
  with distinct paths therefore keep distinct path entries and distinct
  ``results[]`` entries — multiple valid paths to one result are never
  collapsed.

The per-row ``entities`` array of path rows is NOT materialized: it is the
visited-entity walk, recoverable from ``entry`` plus the path's edge sequence
(each segment connects the current node to its other endpoint; traversal never
revisits an entity within one path). This is the dominant byte win — the entry
and intermediate entities are exactly what the rows layout duplicates per row.

Envelope fields (totals, ordering, offset, limits, truncation, relationship
visibility, policy summaries, receipts) are never touched here: the caller
copies them verbatim from the rows result.
"""

from __future__ import annotations

from typing import Any

# Canonical serialized-edge keys, in contract order (RelationshipInstance
# field order). Deliberately excludes ``alias``: cards are physical, aliases
# live on the references.
_EDGE_PAYLOAD_KEYS: tuple[str, ...] = (
    "relationship_type",
    "from_type",
    "from_id",
    "to_type",
    "to_id",
    "edge_key",
    "properties",
    "metadata",
)


class _GraphBuilder:
    """Accumulates unique nodes, edges, and paths while preserving first-seen order."""

    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.paths: list[list[dict[str, Any]]] = []
        self._node_index: dict[tuple[Any, Any], int] = {}
        self._edge_index: dict[tuple[Any, ...], int] = {}
        self._path_index: dict[tuple[tuple[int, str | None], ...], int] = {}

    def add_node(self, entity: dict[str, Any]) -> int:
        key = (entity.get("entity_type"), entity.get("entity_id"))
        index = self._node_index.get(key)
        if index is None:
            index = len(self.nodes)
            self._node_index[key] = index
            self.nodes.append(entity)
        return index

    def add_edge(self, edge: dict[str, Any]) -> int:
        """Register one PHYSICAL edge card, stripping any per-occurrence alias."""
        edge_key = edge.get("edge_key")
        key = (
            edge.get("relationship_type"),
            edge.get("from_type"),
            edge.get("from_id"),
            edge.get("to_type"),
            edge.get("to_id"),
            edge_key is None,
            edge_key if edge_key is not None else -1,
        )
        index = self._edge_index.get(key)
        if index is None:
            index = len(self.edges)
            self._edge_index[key] = index
            if "alias" in edge:
                edge = {field: value for field, value in edge.items() if field != "alias"}
            self.edges.append(edge)
        return index

    def add_path(self, steps: tuple[tuple[int, str | None], ...]) -> int:
        index = self._path_index.get(steps)
        if index is None:
            index = len(self.paths)
            self._path_index[steps] = index
            self.paths.append([{"edge": edge, "alias": alias} for edge, alias in steps])
        return index


def _relationship_edge_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Extract the edge card from a relationship-shaped row payload.

    Relationship rows carry the edge fields at the row's top level; the card
    keeps exactly the serialized edge keys present on the row (relationship
    rows have no ``alias``), in canonical order.
    """
    return {key: item[key] for key in _EDGE_PAYLOAD_KEYS if key in item}


def _include_refs(
    includes: dict[str, Any] | None,
    builder: _GraphBuilder,
) -> dict[str, dict[str, Any]]:
    """Transform per-row include results into node/edge references.

    Include envelope fields pass through verbatim; only ``items`` payloads are
    replaced by references. Include neighbors and edges dedupe into the shared
    top-level arrays like every other card; the per-occurrence edge ``alias``
    is carried on the item ref, never on the shared card.
    """
    refs: dict[str, dict[str, Any]] = {}
    for alias, include in (includes or {}).items():
        items: list[dict[str, Any]] = []
        for entry in include.get("items") or []:
            edge = entry.get("edge") or {}
            items.append(
                {
                    "edge": builder.add_edge(edge),
                    "alias": edge.get("alias"),
                    "source": builder.add_node(entry.get("source") or {}),
                    "target": builder.add_node(entry.get("target") or {}),
                }
            )
        refs[alias] = {
            "alias": include.get("alias", alias),
            "many": include.get("many", False),
            "exists": include.get("exists", False),
            "count": include.get("count", 0),
            "limit": include.get("limit"),
            "truncated": include.get("truncated", False),
            "items": items,
        }
    return refs


def _base_result_ref(item: dict[str, Any], builder: _GraphBuilder) -> dict[str, Any]:
    """Normalize one non-projected serialized row into a reference entry.

    Shape detection mirrors ``profiles.profile_query_item`` exactly: the same
    dicts flow through both, for any profile.
    """
    if "relationship_type" in item:
        ref: dict[str, Any] = {"entry": builder.add_node(item.get("entry") or {})}
        ref["edge"] = builder.add_edge(_relationship_edge_payload(item))
        for endpoint_key in ("from_entity", "to_entity"):
            endpoint = item.get(endpoint_key)
            ref[endpoint_key] = builder.add_node(endpoint) if endpoint is not None else None
        ref["includes"] = _include_refs(item.get("includes"), builder)
        return ref
    if "entry" in item and "result" in item:
        entry_index = builder.add_node(item["entry"])
        # Register the visited entities in walk order so nodes[] keeps
        # discovery order; the walk itself is reconstructed from the path.
        for entity in item.get("entities") or []:
            builder.add_node(entity)
        result_index = builder.add_node(item["result"])
        steps = tuple(
            (builder.add_edge(segment), segment.get("alias")) for segment in item.get("path") or []
        )
        return {
            "entry": entry_index,
            "result": result_index,
            "paths": [builder.add_path(steps)],
            "includes": _include_refs(item.get("includes"), builder),
        }
    return {"result": builder.add_node(item)}


def normalize_query_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize serialized query rows into the graph-layout sections.

    Returns ``{"nodes": [...], "edges": [...], "results": [...], "paths":
    [...]}`` where ``results`` preserves the input row order one-to-one and
    ``paths`` is non-empty only for path-shaped rows.
    """
    builder = _GraphBuilder()
    results: list[dict[str, Any]] = []
    for item in items:
        if "values" in item and "entity_type" not in item:
            source = item.get("source")
            results.append(
                {
                    "values": item["values"],
                    "source": _base_result_ref(source, builder) if source is not None else None,
                }
            )
        else:
            results.append(_base_result_ref(item, builder))
    return {
        "nodes": builder.nodes,
        "edges": builder.edges,
        "results": results,
        "paths": builder.paths,
    }


__all__ = ["normalize_query_items"]
