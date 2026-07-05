# Sprint Notes

Rolling notes from sprint planning + standups. Newest sprint at the top.
Older entries are probably stale; nobody prunes this page.

## Sprint 47 (current)

- `redis_idempotency` flag: holding at 5% while we watch lock-contention
  metrics. Bump to 25% next week if the fencing-token alarm stays quiet.
- Console: delivery-log pagination (the 10k cap) picked up by Dana.
- On-call handoff doc rewrite — carried over for the third sprint. Someone
  please just do it.
- Spike: cost model for region-pinned dispatch workers (see open questions).
  Timeboxed to 2 days. Outcome feeds the enterprise pricing discussion.

## Sprint 46

- Shipped: endpoint-level rate limit overrides (v2 only).
- `events` table hit 1.4 TB; ops added disk. This buys ~2 quarters. The real
  fix is still undecided (open questions page).
- Redis idempotency rollout started: flag live at 5% in prod.
- Decided in standup (needs a proper writeup): 408s from customer endpoints
  will count toward endpoint health scores after all. Contradicts what the
  conventions page implies about 408 retries being "free" — nobody has
  reconciled the two.

## Sprint 44

- v1 API: sent the deprecation email to the three enterprise holdouts.
  Waiting on responses. Target removal end of Q3.
- Kafka bill review moved to next month (again).
- Fixed the Colima/testcontainers hang in CI images; local devs still need
  the env var.

## Sprint 41

- Prototype: Redis Streams event bus. Results written up for the March
  architecture review.
- Hired: two backend, one on console team starting Sprint 43.
- TODO: delete this page's Sprint 30-39 entries. (Deleted in Sprint 42 — this
  TODO itself is now stale. Left as a monument.)
