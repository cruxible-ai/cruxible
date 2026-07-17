# Read-ergonomics track — design contract

Milestone: `ms-dx-friction` (operation instance). Owner: fable-impl.
Goal: cut read-time cost (calls + tokens) for agents without weakening
governance visibility. Baseline captured in `benchmarks/read_anchor/` BEFORE
any change; rerun after each slice.

Loop vocabulary: **Discover → Anchor → Expand → Compute → Prove.**

## Ordering and scope

1. `wi-query-list-compact-catalog` — Discover
2. `wi-read-output-profiles` — cross-cutting projection contract
3. `wi-bounded-neighborhood-inspect` — Expand
4. `wi-read-revision-and-continuation` — freshness + no silent loss
5. `wi-agent-local-working-set` — CLI-side cache (prototype, opt-in)

Sequential on this branch; each WI = one commit, impl + independent review.

## 1. Compact query catalog

Today `api.list_queries` emits full `NamedQueryInfoResult` per query —
identical to `describe_query` (~17k tokens for 38 queries). Fix:

- New contract model `QueryDefinitionSummary`: `name`, `description`, `mode`,
  `entry_point`, `returns`, `result_shape`, `required_params`,
  `allow_relationship_state_override`. Nothing else — no traversal, include,
  select, order_by, or budget internals.
- `GET /queries` returns summaries by default; `?detail=full` preserves the
  full-definition mode (same envelope). `describe_query` stays the canonical
  detailed read, unchanged.
- Collapse the three parallel definition serializers
  (`service/queries.py:_query_definition`, the two constructions in
  `runtime/api.py`, CLI `_query_definition_payload`) into one summary builder
  + one full builder at the service layer; API/CLI/MCP consume them.
- CLI: `cruxible query list` prints/serializes summaries; `--detail full`
  restores today's payload. MCP `cruxible_list_queries` defaults to summary.
- Explicitly out of scope: topic search, fuzzy matching, new named queries.

## 2. Output profiles (compact / standard / full)

One shared entity-shaped serializer with `profile: Literal["compact",
"standard", "full"]`, consumed by query rows (`dump_query_row`), inspect,
`get_entity`, edge list payloads, and the CLI JSON helpers.

- **standard** = today's shape, bit-for-bit. Default everywhere existing.
  Preserving existing API behavior is a hard requirement.
- **compact** = identity card: `entity_type`, `entity_id`, a bounded set of
  display properties (`name`/`title`/`label`/`summary`/`status` when present,
  else first N scalar props), plus governance markers that MUST survive:
  lifecycle status and review status (from `metadata.assertion`). No
  `actor_context`, no provenance blobs, no full property bags. Edge compact:
  `relationship_type`, endpoints, `edge_key`, review/lifecycle markers,
  properties.
- **full** = standard plus anything standard elides (today: nothing — full is
  reserved so later heavy expansions like evidence/lineage hang off it, and
  full ⊇ standard is a contract).
- Surface: `?profile=` query param on list/query/inspect/get routes;
  `--profile` CLI option; MCP read tools accept `profile` and their agent
  descriptions recommend compact for discovery. MCP MAY default compact where
  the WI allows; server API default stays standard.
- Absorb duplication debt: the local/server inspect neighbor-row assembly
  (3 copies) and the scattered CLI JSON payload dicts route through the new
  serializer.

## 3. Bounded neighborhood inspect

Extend `entity inspect` into the generic exploration layer beneath named
queries. New parameters, threaded service → HTTP → client → MCP → CLI:

- `depth` (default 1, hard max 4), `direction`, `relationship_types` (repeat),
  `target_types` (repeat), `state` (same vocabulary as query relationship
  state: live/accepted/all/not-live/pending/reviewable; reuse
  `relationship_matches_query_state` so visibility semantics are identical to
  traversal), `projection` (property-name list applied to neighbor entities),
  `max_nodes`, `max_edges` (defaults 100/200, hard caps 500/1000).
- Deterministic BFS: frontier ordered by (depth, entity_type, entity_id);
  edges ordered by (relationship_type, to_id, edge_key). Cycles visited once.
- Response: root + `nodes[]` (with depth), `edges[]`; every node/edge keeps
  lifecycle + review markers — pending/live/rejected/superseded must never
  flatten together. `truncated: bool` + `truncation_reasons[]`
  (node_budget/edge_budget/depth) + counts. The existing single-hop
  `neighbors` shape stays for depth=1 default calls (backward compat);
  expanded shape returned when depth > 1 or new filters used — or versioned
  response field additions only, decided at implementation with the freeze
  snapshots as the check.
- Storage layer: BFS in `graph/entity_graph.py` beside
  `get_neighbor_relationships`, budget-aware (stop expanding, still report).

## 4. Read revision + explicit truncation + continuation

- Add a monotonic integer `read_revision` to `instance_state`, incremented in
  the same transaction as every mutation commit (alongside
  `head_snapshot_id`). Backfill = current snapshot count at migration.
  Exposed as `read_revision` on all read envelopes + inspect + stats.
  Receipts prove computation, never freshness.
- `ListEnvelopeFields` gains `read_revision`; `truncated` becomes accurate
  everywhere. Fix silent sites: `api.sample` (`total` = true type total,
  `truncated` set), inspect neighbor cap (`truncated` + reason), service-level
  `ListResult` carries envelope fields so the API layer stops re-deriving.
- Continuation: opaque token (base64 JSON) binding `instance_id`,
  config digest, `read_revision`, filter hash, and offset/frontier. Accepted
  by list, catalog, and neighborhood expansion. Token replay at a different
  revision/config → typed 409 `StaleContinuationError`; the caller restarts.
  Queries keep their existing truncation machinery (already explicit).
- Invariant: no read may report `total > 0` with empty `items` without
  `truncated=true` and a reason.

## 5. Agent-local working set (opt-in prototype)

CLI-side only; no server/contract change; will not touch freeze snapshots.

- Opt-in via `CRUXIBLE_WORKING_SET=1` or `--ws`. Off by default (promotion to
  default read path is gated on the RuneBench prototype).
- On any `--json` read, append normalized JSONL lines to
  `~/.cruxible/working-set/<instance_id>/records.jsonl` (credential-scoped
  dir): one line per entity/edge seen, `{kind, entity_type, entity_id,
  props (compact profile), lifecycle, review, read_revision, as_of,
  receipt_refs, source_cmd}`. Dedupe by (kind, type, id): newest revision wins.
- `cruxible ws verify` — compare cached `read_revision` against instance
  head; report stale counts. `cruxible ws refresh` — re-fetch stale records
  (compact). `cruxible ws clear`. `cruxible ws path` — print file path for rg.
- File header line marks the cache NON-AUTHORITATIVE; never consulted by any
  write path.

## Test + guardrail obligations (every WI)

- Scoped tests only (named per dispatch); goldens (`tests/test_golden`)
  forbidden — final integration decides on one golden run.
- API-widening WIs regenerate BOTH snapshots and review the diffs:
  `uv run python scripts/update_http_surface_snapshot.py`
  `uv run python scripts/update_client_contract_snapshot.py`
- Benchmark harness reruns after each slice; deltas recorded in
  `benchmarks/read_anchor/`.
