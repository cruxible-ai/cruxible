# DP-0 destructive-convergence review guide

This is the cumulative review guide for DP-0A through DP-0E. It records the
public product left after the destructive pass, the legacy code deliberately
deleted, and the donor behavior deliberately retained for the Claims +
Procedures convergence batches. It describes the `playbill` branch at DP-0E;
later PC batches must update it when they remove donors or change the public
surface.

DP-0 does not implement first-class Claims or the successor Procedure runtime.
It establishes a Playbill-only served core and keeps only the legacy behavior
needed as a parity oracle for those transplants.

## Surviving public command inventory

The public CLI has these leaf commands and no legacy graph/config/kit command
groups:

```text
context clear
context connect
context show
context use
credential claim-bootstrap
credential list
credential mint
credential recover-admin
credential revoke
credential rotate
playbill body store
playbill document body
playbill document get
playbill document history
playbill document list
playbill document propose
playbill explain
playbill host create
playbill init
playbill principal list
playbill principal recover
playbill principal revoke
playbill principal rotate
playbill proposal activate
playbill proposal approve
playbill proposal inspect
playbill proposal refusal
playbill proposal review
playbill sources check
playbill sources compile
playbill sources propose
server info
server restart
server start
server status
```

The four top-level groups are therefore `context`, `credential`, `playbill`,
and `server`. See the [CLI reference](cli-reference.md) for arguments and
operator semantics.

## Surviving public route inventory

FastAPI also serves its standard OpenAPI endpoints (`/openapi.json`, `/docs`,
`/docs/oauth2-redirect`, and `/redoc`). The product routes are exactly:

```text
GET  /health
GET  /version
POST /api/v1/runtime/instances
GET  /api/v1/server/info
POST /api/v1/server/restart
POST /api/v1/{instance_id}/runtime/bootstrap/claim
GET  /api/v1/{instance_id}/runtime/credentials
POST /api/v1/{instance_id}/runtime/credentials
POST /api/v1/{instance_id}/runtime/credentials/{credential_id}/revoke
POST /api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate
POST /api/v1/{instance_id}/playbill/init
POST /api/v1/{instance_id}/playbill/bodies
GET  /api/v1/{instance_id}/playbill/documents
POST /api/v1/{instance_id}/playbill/documents/proposals
GET  /api/v1/{instance_id}/playbill/documents/{identity}
GET  /api/v1/{instance_id}/playbill/documents/{identity}/body
GET  /api/v1/{instance_id}/playbill/documents/{identity}/history
POST /api/v1/{instance_id}/playbill/explain
GET  /api/v1/{instance_id}/playbill/principals
POST /api/v1/{instance_id}/playbill/principals/proposals
GET  /api/v1/{instance_id}/playbill/proposals/{proposal_id}
GET  /api/v1/{instance_id}/playbill/proposals/{proposal_id}/refusal
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/review
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate
GET  /api/v1/{instance_id}/playbill/sources/context
POST /api/v1/{instance_id}/playbill/sources/check
POST /api/v1/{instance_id}/playbill/sources/proposals
```

## Surviving public MCP tool inventory

The registered MCP tools are exactly:

```text
cruxible_version
cruxible_server_info
cruxible_playbill_host_create
cruxible_playbill_init
cruxible_playbill_store_body
cruxible_playbill_propose_document
cruxible_playbill_inspect_proposal
cruxible_playbill_inspect_refusal
cruxible_playbill_review
cruxible_playbill_prepare_approval
cruxible_playbill_submit_approval
cruxible_playbill_activate
cruxible_playbill_list_documents
cruxible_playbill_get_document
cruxible_playbill_dereference
cruxible_playbill_history
cruxible_playbill_explain
cruxible_playbill_source_context
cruxible_playbill_check_source_bundle
cruxible_playbill_propose_source_bundle
cruxible_playbill_list_principals
cruxible_playbill_propose_principal_change
```

The permission catalog and MCP registration catalog are equality-tested. See
the [MCP reference](mcp-tools.md) for permissions and payload intent.

## Deleted directory and service inventory

DP-0 removed these legacy product packages in full:

- `cruxible_core.bindings`
- `cruxible_core.blueprint`
- `cruxible_core.canonical_views`
- `cruxible_core.decision`
- `cruxible_core.feedback`
- `cruxible_core.installs`
- `cruxible_core.kit_distribution`
- `cruxible_core.snapshot`
- `cruxible_core.telemetry`
- `cruxible_core.transport`
- `cruxible_core.ui_static`

It removed the legacy CLI modules for attestations, config views, decision
records, feedback, gates, groups, instances, kits, lifecycle verbs, list/read
surfaces, mutations, outcome contracts, Procedures, source artifacts, state,
telemetry, workflows, and working sets. The matching legacy HTTP route modules
were removed, as were the mixed `runtime.api` facade and MCP working-set/kit
surfaces. None remains registered indirectly.

These legacy service modules were deleted:

```text
service/analysis.py
service/bindings.py
service/config_mutations.py
service/decisions.py
service/feedback.py
service/installs.py
service/snapshots.py
service/state.py
service/state_diff.py
service/telemetry.py
service/views.py
```

The pass also deleted `working_set.py`, `runtime/instance_manager.py`,
`mcp/kit_surface.py`, `server/telemetry.py`, all first-party `kits/`, the kit
bundle/lock/release/doc generation scripts, the snapshot/state publication
products, and their product-specific tests. Legacy config-authority, mutable
graph, snapshot/overlay, kit-distribution, provider, and deep-dive documentation
was removed or rewritten around the surviving Playbill surface.

## Donor allowlist and removal batches

This table is the complete import-level donor manifest. Served Playbill code
may not import these modules; any Playbill transplant must go through the named
adapter. Every row has an owning removal batch.

| Donor module prefix | Removal batch | Why retained | Adapter |
|---|---:|---|---|
| `cruxible_core.procedure` | PC-E2 | Definition, digest, static-law, and run/read oracle | — |
| `cruxible_core.workflow` | PC-E2 | Compiler, contract, transform, and executor oracle | — |
| `cruxible_core.config.schema` | PC-F | Selected step, query, provider, and contract schema donor | — |
| `cruxible_core.predicate` | PC-F | Typed comparison and coercion donor | — |
| `cruxible_core.query` | PC-F | Traversal, filtering, and projection donor | — |
| `cruxible_core.graph` | PC-F | Query-oracle types and `EvidenceRef` behavior | — |
| `cruxible_core.receipt` | PC-E1 | Exhaust receipt-tree donor | — |
| `cruxible_core.attestation` | PC-C | Stance, disposition, and idempotency donor | — |
| `cruxible_core.resolution_contracts` | PC-E1 | Resolution declaration, activation, and disposition donor | — |
| `cruxible_core.source_artifacts.markdown` | PC-C | Deterministic Markdown span-extraction donor | — |
| `cruxible_core.provider` | PC-E2 | Provider contract, registry, and trace donor | — |
| `cruxible_core.providers` | PC-E2 | Provider implementation donor | — |
| `cruxible_core.group` | PC-D | Frozen `propose_group_from` verifier support | — |
| `cruxible_core.kits` | PC-D | Old-compiler helpers for frozen verification | — |
| `cruxible_core.runtime.instance` | PC-F | Temporary donor-parity harness | — |
| `cruxible_core.storage.sqlite` | PC-F | Temporary donor-parity storage harness | — |
| `cruxible_core.instance_protocol` | PC-F | Temporary interface, metadata, and integrity harness | — |

The unserved lifecycle/group/feedback service code is behavior corpus, not a
hidden product surface. Its operation names live in
`DONOR_OPERATION_PERMISSIONS`, disjoint from both public MCP tools and active
HTTP/CLI runtime operations, so parity tests retain the original authority law
without re-registering deleted endpoints. Those service donors leave with the
owning PC-C through PC-F transplants.

## Exact frozen goldens retained

The complete tracked `tests/goldens/` inventory is:

```text
tests/goldens/cruxible_client/contracts_snapshot.json
tests/goldens/http_surface/http_surface_snapshot.json
tests/goldens/kev/asset_exposure_proposal.json
tests/goldens/kev/asset_products_proposal.json
tests/goldens/kev/auto_resolve_branches.json
tests/goldens/kev/exposure_reconciliation_proposal.json
tests/goldens/kev/intermediate_payloads/asset_exposure_workflow.json
tests/goldens/kev/intermediate_payloads/asset_products_workflow.json
tests/goldens/kev/intermediate_payloads/exposure_reconciliation_workflow.json
tests/goldens/kev/named_query_surfaces.json
tests/goldens/kev/overlay_review_state.json
tests/goldens/kev/reference_build_state.json
tests/goldens/kev/relationship_state_visibility.json
tests/goldens/playbill/attestation-v1.json
tests/goldens/playbill/candidate-v1.json
tests/goldens/playbill/claim-type-v1.json
tests/goldens/playbill/oracles-v1.json
tests/goldens/playbill/projection-v1.json
tests/goldens/playbill/semantic-genesis-v1.json
tests/goldens/playbill/served-surface-dp0b-v1.json
tests/goldens/playbill/settlement-roots-v1.json
tests/goldens/playbill/source-reference-v1.json
tests/goldens/playbill/subject-v1.json
tests/goldens/state_cross_section/car_parts_state_diff.json
```

`tests/goldens/playbill/claim-type-v1.json` preserves the canonical policy-bearing
ClaimType v1 wire and digest contract.

`tests/goldens/playbill/source-reference-v1.json` preserves locator-free external
source identity and remote-state refusal behavior.

`tests/goldens/playbill/oracles-v1.json` pins the Family-1 oracle at
`e3fe35b360d098f14a5d59bf770ffee401224f0c` and the Procedure graph-program
oracle at `986307d56649eb51747ca227228fbe19f73e3895`.

Other deliberately frozen donor inputs are the 48-file
`tests/data/procedure_digest_corpus/` (whose exact membership is asserted by
`test_definition_digest_corpus.py`) and the byte-preserved
`tests/data/config_donors/agent-operation/config.yaml` plus
`cruxible-kit.yaml`. They remain verification fixtures, not distributed kits.

## Intentionally retained package dependencies

Dependency pruning is PC-H work and follows actual imports after the final
transplant. DP-0 intentionally retains:

| Dependency | Why retained after DP-0 |
|---|---|
| `cruxible-client` | Reduced Playbill HTTP client shipped with the core |
| `pydantic` | Active Playbill/client contracts and donor validation models |
| `packaging` | Version comparison in the PC-D kit/compiler donor |
| `networkx` | Graph/query parity oracle through PC-F |
| `polars` | Tabular provider/workflow parity through PC-E2/PC-F |
| `pyyaml` | Active source-catalog CLI plus config/workflow donors |
| `structlog` | Active daemon audit/request logging and service donors |
| `click` | Active four-group CLI |
| `cryptography` | Ed25519 principal keys, signatures, and Git verification |
| `rich` | Packaging cleanup deferred to the import-based PC-H prune; no new Playbill contract depends on it |
| `httpx` | Reduced client/CLI transport and provider donor |
| `pypdf` | PDF provider donor through PC-E2 |
| `markdown-it-py` | Deterministic Markdown source-span donor through PC-C |
| `fastapi` | Active HTTP daemon |
| `python-multipart` | Daemon dependency retained until the PC-H import audit |
| `uvicorn` | Active daemon launcher |
| `mcp` (optional extra) | Active Playbill MCP server |
| `docling` (optional `pdf` extra) | Layout-aware PDF provider donor through PC-E2 |

No dependency in this table is evidence that its legacy product is still
public. The served dependency-closure guard separately proves that Playbill
transport code cannot reach legacy graph/config/storage donors.

## Verification

DP-0E is accepted only when the full surviving test suite is green, in addition
to the required focused block:

```bash
uv run pytest -q tests/test_playbill tests/test_architecture/test_playbill_*.py
uv run mypy src/cruxible_core/playbill src/cruxible_core/service
uv run ruff check src tests
```

The architecture suite pins the public registration catalogs, the donor
manifest, the private donor permission seam, the immutable oracle commits, and
this guide's inventories. The RR `change_head` must name the exact commit that
passed those checks.
