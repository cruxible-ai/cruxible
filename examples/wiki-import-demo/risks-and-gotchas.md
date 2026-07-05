# Risks And Gotchas

Standing concerns and sharp edges. If it bit someone twice, it goes here.

## Risks

- **Single Kafka consumer-group for dispatch.** If the group gets wedged
  (it has, twice), all delivery stops globally — there is no per-tenant
  isolation. Mitigation half-built: `dispatch --shard` exists but has never
  been run in production.
- **pgbouncer + advisory locks.** Delivery workers bypass the pooler to hold
  session locks (see the May idempotency decision). Every new worker replica
  eats direct Postgres connections; we are ~30 replicas away from
  `max_connections`. This is the quiet capacity ceiling nobody watches.
- **Flag debt in `dispatch/flags.yaml`.** 40+ flags, at least a dozen past
  the 90-day rule. Two past incidents started with "that flag was still on?"
- **The v1 API is frozen but not gone.** Every month it stays, the enterprise
  migration gets politically harder. Security-fix-only also means nobody
  looks at it, which is its own risk.

## Gotchas

- Staging secrets rotate weekly on Sunday night. If staging breaks Monday
  morning, check secret mounts before debugging anything else.
- `make test` in `dispatch/` spins up testcontainers; on machines with Colima
  the Kafka container needs `TESTCONTAINERS_RYUK_DISABLED=true` or it hangs.
- The console's delivery-log view silently caps at 10k rows. Support has been
  told; they forget quarterly.
- `deploy.sh` reads the git tag from the CURRENT checkout, not origin. Deploying
  from a stale clone has shipped an old release once already.
- Grafana's "Delivery success %" panel excludes 429s. Arguing with a customer
  about their success rate? Check whether they're being rate-limited first.
