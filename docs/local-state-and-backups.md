# Local State And Backups

SQLite is an acceptable starting point for OSS, local daemon, and single-droplet
deployments. The important rule is that all state changes still go through
Cruxible surfaces: workflows, canonical apply, proposal groups, feedback,
queries, and receipts.

Do not treat SQLite as an application API. Treat it as the local persistence
backend.

## State Directory

In daemon mode, state lives under:

```text
${CRUXIBLE_SERVER_STATE_DIR:-~/.cruxible/server}
```

The daemon materializes governed instances under `instances/inst_*` and returns
opaque instance IDs to clients. Keep this directory outside the agent workspace
when the agent should not have direct state access.

Do not use `/tmp`, `/var/tmp`, or macOS private temp directories for long-lived
daemon state. Those paths may be cleaned by the operating system while the
daemon is still running. Cruxible emits a startup warning when the server state
directory or a registered instance location resolves under a known volatile temp
path.

Direct local runtime still creates a `.cruxible/` directory under the workspace.
That mode is convenient for development, but it is not the recommended agent
boundary.

## Daemon Logs

In daemon mode, structured server request logs are JSON lines written under the
server state directory by default:

```text
${CRUXIBLE_SERVER_STATE_DIR:-~/.cruxible/server}/logs/server.log
```

Set `CRUXIBLE_SERVER_LOG_PATH` to place that log somewhere else, for example
under a supervisor-managed log directory. The daemon rotates this file locally
and treats log sink failures as nonfatal; on the first write failure it emits a
best-effort warning to stderr so operators know request logs may be dropping.

## What Lives There

A Cruxible instance can include:

- active config and workflow lock
- kit metadata and materialized kit runtime files
- graph snapshots and current graph state
- query and workflow receipts
- provider execution traces
- candidate groups and group resolutions
- decision records and decision events
- feedback and outcomes
- generated local artifacts such as wiki output when requested

Exact file names may change across releases. Back up the instance directory as
a unit instead of cherry-picking one SQLite database.

## Backup Guidance

For a droplet or single VM:

1. Stop or quiesce the daemon before taking a filesystem-level backup when
   possible.
2. Back up the whole server state directory.
3. Back up the source kit or record the kit alias/ref and version used to
   materialize the instance.
4. Store any customer source artifacts that feed canonical workflows.
5. Store the Cruxible package version and command used to start the daemon.

If you need online backups, use SQLite-aware backup tooling or snapshot the
volume in a way that gives a consistent filesystem view.

## Snapshots Versus Backups

Cruxible snapshots are state snapshots. They are useful for cloning,
preview identity checks, and comparing graph state over time.

Backups are operational recovery artifacts. They should include the graph plus
the surrounding evidence stores: receipts, traces, groups, resolutions,
decision records, feedback, outcomes, locks, configs, and kit metadata.

Use snapshots to reason about state. Use backups to recover the deployment.

### Edge receipts across snapshots and clones

Each relationship records a `receipt_id` in its provenance pointing at the
receipt that authored it. The invariant is that a non-null `receipt_id` always
resolves to a receipt present in the same instance; a null `receipt_id` is
accepted, immutable history. Two cases produce a null `receipt_id`, and neither
is backfilled:

- **Legacy edges** created before per-edge receipts existed carry a null
  `receipt_id` because no receipt was ever written for them.
- **Cloned, snapshot-restored, and pulled-overlay edges** have their `receipt_id`
  cleared on materialization. A snapshot/clone/state-pull bundle is
  graph+config+lock with no receipts, so the original `receipt_id` would point at
  a receipt that lives only in the source instance. On materialization Cruxible
  nulls that dangling pointer and stamps `clone_origin: upstream-snapshot` on the
  provenance (preserving the original id under `cloned_receipt_id` for
  traceability), so the edge is honestly labeled as clone-origin rather than
  referencing a phantom receipt.

`cruxible instance snapshot` writes a portable same-identity backup artifact for
the active instance. The artifact includes the SQLite state database, active
config, instance metadata, optional workflow lock, and a manifest with content
digests. The service uses SQLite's backup API for the database copy instead of
copying a live database file byte-for-byte.

`cruxible instance restore` restores that artifact into a clean target and keeps
the original `instance_id`. This is different from `cruxible clone`, which
creates a new local instance from a graph snapshot. Restore is an admin
lifecycle operation and should only be used when the old instance is stopped or
unregistered, because the result is the same logical instance identity.

## Portability

State is not meant to be trapped in SQLite forever. The durable product
contract is the Cruxible state model and audited mutation surfaces, not direct
SQLite access.

When moving to a future managed backend, the data that must be portable is:

- accepted graph state and snapshots
- config and lock state
- receipts and provider traces
- candidate groups and resolutions
- decision records and events
- feedback and outcomes
- kit metadata and source artifact provenance

Until managed Postgres or cloud migration tooling exists, the practical
portability story is:

- keep source artifacts and kits versioned
- keep daemon state backed up as a unit
- use Cruxible export/query/wiki surfaces for inspection
- avoid customer code that depends on raw SQLite schemas

## Agent Isolation Notes

Local OSS isolation is a practical boundary, not a hard sandbox:

- run `cruxible-server` as the runtime owner
- keep `CRUXIBLE_SERVER_STATE_DIR` outside the repo and agent workspace
- install `cruxible-client` in the agent environment
- expose MCP or HTTP, not the state directory
- use `CRUXIBLE_MODE=governed_write` for normal agent workflows

If the agent can read the daemon state path, control the daemon process, or
import the runtime package with filesystem access, it can bypass the intended
state surfaces. Use a separate VM, host, or managed service when that boundary
must be strong.
