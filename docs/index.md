# Cruxible Playbill development documentation

This documentation describes the breaking Playbill development line, not the
retired config-authority and mutable-graph product.

Playbill is a governed state substrate. Exact bytes and external source
coordinates become deterministic candidates; humans or agents review and sign
those candidates; activation advances an accepted Git ledger by compare-and-set.
SQLite and rendered files are rebuildable projections.

## Start here

- [Quickstart](quickstart.md): run a daemon and initialize a Playbill instance.
- [Concepts](concepts.md): CAS, proposals, generations, Claims, Procedures, and
  attestations.
- [Architecture](architecture.md): authority boundaries and the hot/cold split.
- [Family 1](playbill-family-1.md): the implemented Document lifecycle.
- [For AI agents](for-ai-agents.md): efficient discovery and operating rules.

## Current status

Documents, principal governance, source bundles, accepted reads, history, and
explanation are implemented. First-class Claims and Playbill-native Procedures
are the next implementation program.

Legacy graph, config, kit, snapshot, state-distribution, and mutation interfaces
are absent from the served API. Some old internals remain as test-backed donors
until their semantics are transplanted.
