# Architecture Decisions

Running log, newest first. If a decision changed, we add a new entry rather
than editing the old one. (We have not always followed this rule.)

## 2026-05: Move delivery idempotency to Redis-based locks

Postgres advisory locks are hitting connection-pool pressure at peak fan-out
(pgbouncer in transaction mode can't hold session-level locks, so delivery
workers bypass the pooler and eat direct connections). Decision: move
idempotency to Redis `SET NX PX` locks with a fencing token check on write.

Status: accepted in the May architecture review. Rollout was supposed to be
Q2 but the flag `redis_idempotency` is still at 5% as of the last sprint
notes. Somebody should confirm whether this is actually done before we tell
anyone the advisory-lock guidance is obsolete.

## 2026-03: Kafka stays; Redis Streams rejected for the event bus

We prototyped Redis Streams for the ingest → dispatch bus to cut infra. Under
sustained 40k events/s the consumer-group rebalance behavior was worse for us
than Kafka's, and we'd be trading a known operational surface for an unknown
one. Decision: keep Kafka, revisit only if the managed-Kafka bill doubles.

## 2026-02: Deprecate the v1 API

v1 predates endpoint-level auth scopes. Decision: freeze v1 (security fixes
only), route new work to v2, remove v1 when the last three enterprise
customers migrate. Support has the customer list. Target was "by end of Q3"
but no one owns the migration nudges yet.

## 2025-11: One database, schemas per service

We decided against database-per-service. Single Postgres cluster, one schema
per service, cross-schema reads forbidden except through published views.
This is load-bearing for the analytics team's replica queries.
