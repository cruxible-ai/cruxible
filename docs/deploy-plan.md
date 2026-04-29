# Cruxible Deploy — Day 1 Plan

## Context

Cruxible's monetization has two axes: **world model subscriptions** (reference layers maintained by Cruxible) and **Cruxible Deploy** (managed cloud deployment with Postgres, RBAC, multi-user). This plan covers the initial Deploy product — enough to onboard a first paying law firm.

The goal is NOT to build a platform. It's to get one firm from "local SQLite demo" to "production instance my team uses" with the minimum infrastructure that supports recurring revenue.

## Day 1 Decisions

- **Deploy bootstrap model** — A deployment is bootstrapped by rebuilding the remote instance from declarative inputs: config, reference world release, artifacts in object storage, and workflow execution. We do **not** treat the local `.cruxible/` directory as a deployable unit.
- **Authoritative state after deploy** — Once deployed, the remote instance is authoritative. Local demo/dev instances remain authoring and testing environments, not bidirectionally synced peers.
- **Tenant topology** — Day 1 deploy is **one customer deployment with one primary Cruxible instance**. RBAC is required inside that instance.
- **Persistence model** — Deploy v1 uses **Postgres as durable backing** while preserving the current in-memory graph/runtime semantics. We are **not** adopting a graph database in v1.
- **Reference/fork model** — Keep the current reference-world and fork-overlay semantics: upstream and fork-owned state are tracked separately, then assembled into one live execution graph on the deployed instance.
- **Artifact storage** — Use **S3** for uploaded artifacts and cached reference world bundles.
- **Recovery model** — Disaster recovery comes from managed service backups. Operator rollback comes from Cruxible snapshots.

## World Models & Monetization Shape

### Two verticals at launch

| World Model | Reference Config | Feed Sources | Refresh Cadence |
|---|---|---|---|
| **KEV Triage** | `kev-reference.yaml` | CISA KEV, NVD, EPSS | Hourly (KEV/EPSS), daily (NVD) |
| **Case Law Monitoring** | `courtlistener-reference.yaml` | CourtListener (opinions, dockets, filings, judges, courts, statutes) | Daily |

Each world model has a reference layer (public entity types, deterministic relationships, canonical build workflow) and a demo fork (internal entity types, governed relationships, sample seed data, proposal workflows).

### Free vs paid tiers

| | Free demo | Production subscription |
|---|---|---|
| **Reference world** | Static demo bundle, sample data, all workflows runnable locally | Maintained live feed, versioned releases, SLA on freshness |
| **Fork** | Demo fork with synthetic seed data — full schema, all governed relationships, sample proposals | Customer's own fork with their real data, composed against the live reference world |
| **Deploy** | N/A (local only) | Postgres, RBAC, multi-user, managed compute |

The demo instances serve the adoption funnel. Anyone can initialize a local demo instance from the checked-in demo config, run the workflows, see proposals resolve, and understand the shape. No gate.

The production reference world is a subscription. The firm's fork `extends:` the maintained reference and gets versioned updates. Deployed forks pin a reference release and use the same preview/apply recomposition model as release-backed world pulls — the fork's internal types, governed relationships, and accumulated Loop 2 calibration are preserved.

### Pricing (day 1)

| Line item | Price | Notes |
|---|---|---|
| Production reference world | $500-1,000/mo per world | Feed maintenance, schema versioning, freshness SLA |
| Cruxible Deploy | $1,500-3,000/mo | Postgres, compute, RBAC, backups, uptime SLA |
| Integration & setup | $15-30k one-time | Wire data sources, load initial state, calibration period |

### What the fork accumulates (why switching cost is high)

The reference world is commodity — public data, deterministic relationships. The fork's value compounds over time:

- **KEV:** Asset inventory mappings, exposure judgments with receipts, compensating control validations, patch exception history, Loop 2 trust calibration from resolved vulnerabilities
- **Case Law:** Matter/client/position graph, opinion impact judgments with receipts, case outcome history, position track records, Loop 2 constraints derived from which predictions were right/wrong

This accumulated state — especially the Loop 2 calibration — is what makes the fork progressively more valuable and difficult to replicate elsewhere.

## What Deploy v1 Does

A firm runs `cruxible deploy` from their local fork instance. Cruxible provisions:

- **Postgres on RDS** as the durable backing store for graph state, receipts, feedback, groups, and snapshots
- **Cruxible server on ECS/Fargate** running the HTTP API
- **API keys with roles** (admin / editor / viewer) for multi-user access
- **One primary deployed instance** for the firm, composed against a Cruxible-hosted reference world
- **S3-backed artifact and reference bundle storage**

After deploy, the firm's agents and users hit the remote server via `CruxibleClient` with their API keys. Local MCP points at the deployed server (already supported via `CRUXIBLE_SERVER_URL`). The remote instance is the system of record after cutover.

## Architecture

```
Firm's local machine                    AWS (Cruxible-managed)
┌──────────────┐                       ┌─────────────────────────┐
│ cruxible deploy ──── provisions ────>│ ECS Fargate             │
│              │                       │  └─ cruxible-server     │
│ cruxible-mcp │── CRUXIBLE_SERVER_URL─>│     ├─ FastAPI + routes │
│ (server mode)│                       │     └─ API key auth     │
└──────────────┘                       │                         │
                                       │ RDS Postgres            │
                                       │  ├─ graph (entities,    │
                                       │  │   relationships)     │
                                       │  ├─ receipts.db tables  │
                                       │  ├─ feedback.db tables  │
                                       │  └─ api_keys table      │
                                       │                         │
                                       │ S3                      │
                                       │  └─ reference world     │
                                       │     bundles (cached)    │
                                       └─────────────────────────┘
```

## Workstreams

### 1. Postgres Store Backends

The receipt, feedback, and group store protocols are useful starting points, but Deploy v1 also needs a graph persistence refactor. The goal is to keep the current runtime semantics while swapping the durable backing.

- **Execution model stays the same** — The server still works against one in-memory execution graph. Queries, workflows, and governed resolution continue to operate on that graph. Postgres is the durable backing store, not a graph-native query engine.
- **Graph state in Postgres** — Replace `graph.json` with relational tables such as `entities(entity_id, entity_type, properties jsonb)` and `relationships(rel_id, rel_type, source_id, target_id, properties jsonb, confidence)`. Load/rebuild the execution graph from these tables on startup, then write through on mutations.
- **Snapshots and upstream metadata** — Persist snapshot metadata, rollback state, and reference-world tracking in Postgres alongside the live graph so the deployed instance retains the current snapshot and pull/recompose model.
- **Receipt store** — Port the current receipt and execution-trace schema to Postgres.
- **Feedback + group store** — Port feedback, outcomes, groups, and resolutions to Postgres.
- **Connection management** — Use `psycopg` (sync) to match the current sync architecture. Connection pool per deployment. Config via `DATABASE_URL`.
- **Store / instance factory** — Add a backend-aware factory so a deployment can create the right persistence adapters while preserving the current instance/service APIs.

### 2. API Key Auth & RBAC

Extend the existing bearer-token auth to support multiple keys with roles.

- **`api_keys` table in Postgres** — `key_id, key_hash (sha256), role (admin|editor|viewer), label, instance_id, created_at, last_used_at, revoked_at`
- **Key issuance** — `cruxible deploy keys create --role editor --label "attorney-jones"` prints the key once. Stored hashed.
- **Auth middleware change** — Current middleware does single-token comparison. Change to: hash incoming bearer token, look up in `api_keys` table, extract role. Set role in request state.
- **Role to PermissionMode mapping** — `viewer = READ_ONLY`, `editor = GOVERNED_WRITE`, `admin = ADMIN`. This maps directly onto the existing 4-tier permission system. The `request_permission_scope()` context var already supports per-request overrides.
- **Audit enhancement** — Add `key_id` to structlog mutation events (already logging mutations, just need to include who).

### 3. AWS Infrastructure (Terraform)

Minimal Terraform module for a single-tenant deployment:

- **ECS Fargate service** — Runs `cruxible-server` container. Single task, auto-restart. No ALB needed initially — direct task IP or use an NLB for TLS termination.
- **RDS Postgres** — `db.t4g.micro` for firm #1. Private subnet, security group allows only ECS task.
- **ECR** — Container registry for the cruxible-server image.
- **S3 bucket** — Cached reference world bundles plus uploaded workflow artifacts and seed inputs.
- **Secrets Manager** — Database credentials, API key encryption key.
- **VPC** — Standard 2-AZ setup with public/private subnets.

One Terraform workspace per customer deployment. Day 1 is one customer deployment with one primary Cruxible instance. Variables: `customer_id`, `instance_size`, `reference_worlds`.

### 4. `cruxible deploy` CLI Command

New subcommand group in `cli/commands/`:

```
cruxible deploy init          # Provision AWS infra from local instance
cruxible deploy status        # Check deployment health
cruxible deploy keys create   # Issue API key with role
cruxible deploy keys list     # List active keys
cruxible deploy keys revoke   # Revoke a key
cruxible deploy artifacts sync # Upload declared artifacts / seed inputs to S3
cruxible deploy logs          # Tail server logs
cruxible deploy destroy       # Tear down (with confirmation)
```

`deploy init` workflow:
1. Read local fork config plus declared artifact references
2. Run Terraform apply (or call AWS APIs directly) to provision infra
3. Upload reference world bundle and fork artifacts / seed inputs to S3
4. Start ECS task
5. Initialize the remote instance from config and object-storage-backed inputs
6. Run the canonical workflows needed to build the deployed reference + fork state
7. Issue admin API key, print connection config
8. Print: "Add to your MCP config: `CRUXIBLE_SERVER_URL=https://... CRUXIBLE_SERVER_TOKEN=crux_...`"

### 5. Reference World Hosting

For the subscription product:

- **CI workflow** — `publish-reference-world.yml` builds reference world bundles on schedule (daily for CourtListener, hourly for KEV/NVD/EPSS). Publishes to S3 + OCI.
- **Version pinning** — Deployed instances pin to a reference world version. Reference updates use the existing preview/apply recomposition model: preview the incoming release, then apply it to the fork while preserving fork-owned state.
- **Subscription gating** — Reference world S3 bucket uses presigned URLs or IAM-scoped access. Deployed instances get credentials; demo instances use the public demo bundle.

## Build Order

| Phase | What | Why first |
|---|---|---|
| **1** | Postgres durable backing + instance persistence refactor | Everything else depends on this. Can test locally with a Postgres container before any AWS work. |
| **2** | API key auth + RBAC | Needed before multi-user access. Can test against local Postgres server. |
| **3** | Artifact/reference storage on S3 + reference hosting flow | Needed to bootstrap remote instances from declarative inputs instead of local `.cruxible` copies. |
| **4** | Terraform module + Docker image | Infrastructure provisioning. Can test with `terraform apply` manually. |
| **5** | `cruxible deploy` CLI | Automates the manual bootstrap flow after the backend pieces exist. |

## Out of Scope

- SSO/OAuth — API keys are enough for firm #1. Add when a firm requires it.
- Multi-tenant shared infrastructure — One deployment per customer. Revisit at 5+ customers.
- Auto-scaling — Single Fargate task. Scale up the task size, not the count.
- Web dashboard — Firms interact via MCP (Claude Code) and CLI. No UI.
- Billing/metering automation — Invoice manually. Build metering when you have enough customers to justify it.
- Self-hosted / bring-your-own-cloud — You manage all deployments.

## Verification

- **Phase 1:** Run full test suite against Postgres (spin up Postgres in Docker, set `DATABASE_URL`, run `uv run pytest`). All existing tests should pass with both SQLite and Postgres backends.
- **Phase 2:** Issue keys with different roles, verify permission enforcement matches existing `CRUXIBLE_MODE` behavior. Verify audit logs include `key_id`.
- **Phase 3:** `terraform apply` creates working infra. `cruxible-server` starts, health check passes, can connect with `CruxibleClient`.
- **Phase 4:** `cruxible deploy init` from a local fork config provisions everything, bootstraps the remote instance from config + S3-backed inputs, and returns working connection config. MCP in server-mode can run all tools against the deployed instance.
- **Rollback:** Create a snapshot, apply a world update or canonical workflow, and verify rollback to the prior snapshot works as expected.
