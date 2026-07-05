# Relay — Agent Instructions

Relay is a webhook delivery platform: ingest events, fan out to customer
endpoints, retry with backoff, surface delivery receipts. Monorepo: `ingest/`
(Go), `dispatch/` (Go), `console/` (TypeScript/React), `infra/` (Terraform).

## Conventions

- Go services: run `make lint test` before pushing. CI is authoritative but
  slow (~9 min); don't use it as your only feedback loop.
- All new endpoints go through `ingest/api/v2`. v1 is deprecated — do not add
  routes there, even "just one small one."
- Feature flags live in `dispatch/flags.yaml`. Flags older than 90 days should
  be deleted, not commented out. (Nobody actually does this. See gotchas page.)
- Database migrations: one migration per PR, forward-only. If you need a
  rollback, write a new forward migration.
- Idempotency for delivery workers uses Postgres advisory locks keyed on
  `(endpoint_id, event_id)`. Do NOT add a second locking layer on top.
- Never retry a 4xx from a customer endpoint except 408 and 429.
- Console: no new Redux. New state goes in TanStack Query. Old Redux slices
  get migrated opportunistically when touched.

## Things Agents Keep Getting Wrong

- `dispatch/internal/backoff` is NOT a general-purpose retry library. It
  assumes delivery semantics (max 72h horizon, jitter tuned for webhook
  storms). Use `pkg/retry` for anything else.
- The staging Kafka cluster is shared with the data team. Do not create
  topics without a `relay-` prefix.
- `EndpointHealth` scores are recomputed nightly, not live. Treating them as
  live status has caused two support escalations.

## Deploy

- `main` auto-deploys to staging on green CI.
- Production deploys are manual: `./scripts/deploy.sh prod` from a tagged
  release. Fridays are fine, we are not superstitious, but check the on-call
  calendar first.
