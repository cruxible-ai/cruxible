# Onboarding Map

Where things live and who to ask. Written for new humans; agents use it too.

## Services

| Service | Path | Language | Owner |
| --- | --- | --- | --- |
| Ingest | `ingest/` | Go | Priya |
| Dispatch | `dispatch/` | Go | Marcus |
| Console | `console/` | TypeScript | Dana |
| Infra | `infra/` | Terraform | Marcus (interim) |

## Data stores

- **Postgres** (RDS): one cluster, schema per service. Analytics reads the
  replica. Migrations via `migrate/` per service.
- **Kafka** (managed): ingest → dispatch bus. Topics prefixed `relay-`.
- **Redis**: rate limiting, and (rolling out) delivery idempotency locks.

## Environments

- `staging` — auto-deployed from main. Shared Kafka with the data team.
- `prod` — manual deploy from tags. Two regions (us-east-1, eu-west-1), but
  dispatch workers are NOT region-pinned; EU events can be delivered by US
  workers today.

## Who to ask

- Delivery semantics / retries: Marcus
- Postgres capacity, pgbouncer: Priya
- Anything console: Dana
- Billing questions about Kafka: whoever lost the coin toss
