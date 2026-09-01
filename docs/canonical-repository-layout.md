# Canonical repository and daemon layout

Cruxible keeps shareable workspace configuration, derived local views, and
daemon authority in separate locations. No daemon key, bearer credential,
accepted ledger, or trust root belongs in the source repository.

## Workspace

~~~text
<git-worktree>/
  .playbill/
    sources.yaml
    sources.local.yaml
    coverage.json
    presentation-policy.json
    floor/
~~~

`sources.yaml`, `coverage.json`, and `presentation-policy.json` are shareable
configuration. `sources.local.yaml` is an optional machine-local overlay and
must be ignored. `.playbill/floor/` is a derived, exactly replaceable cache and
must also be ignored. A typical ignore fragment is:

~~~gitignore
.playbill/sources.local.yaml
.playbill/floor/
~~~

The floor location is fixed. A v2 `floor_output` declaration enables it but
does not carry a path:

~~~json
{
  "tag": "playbill-coverage-workspace-config-v2",
  "floor_output": {
    "tag": "playbill-floor-output-v1",
    "format": "playbill-floor-export-v2"
  }
}
~~~

The floor is excluded from evidence observation, so reading generated cards
cannot recursively manufacture coverage. Delete and re-export it at any time.

The `.yaml` source-catalog names are intentional workspace formats. Governed
ledger artifacts in the installed PC-HR compiler lineage use the pretty canonical
JSON codec and `.json` paths; those are separate domains. A mutable instance need
not use the executable's newest compiler coordinate, but its compiler must select
that current artifact-codec lineage.

## Daemon state root

The default state root is `~/.cruxible`. `cruxible server start --state-root
DIR` or `CRUXIBLE_STATE_ROOT=DIR` replaces that base:

~~~text
<state-root>/
  instances/<instance-id>/
    instance.json
    ledger.git/
    cas/
    exhaust/
    projections/
    credentials/
    leases/
  trust/<instance-id>.json
  daemon/
    registry.db
    runtime_credentials.db
    logs/server.log
~~~

The instance directory is the managed root itself. The pinned trust root stays
out of band under `trust/`, while registry, runtime credential, and logging
state are daemon-global. The obsolete `CRUXIBLE_SERVER_STATE_DIR` variable is
refused rather than silently assigned precedence.

Pre-PC-HR instances used a nested layout and compact `.yaml` artifact codec.
Archive the managed instance directory together with its matching out-of-band
`trust/<instance-id>.json`, then re-seed under a new instance ID; do not transcode
signed history. Leaving only one half is a typed re-seed-required state, never a
partly initialized state. Historical compiler verifiers remain available for
frozen material, but a mutable instance must use the current layout and an
installed compiler in the PC-HR artifact-codec lineage.

Re-seed creates fresh owner, reviewer, and recovery custody by default.
Arbitrary existing client key directories are not an import surface and are
refused. The CLI's per-principal init retry marker is narrower: it binds an
init-created key pair to one normalized transport, instance ID, principal ID,
kind, and public key solely so the same operation can recover from response
loss. It is cleared after success and does not authorize later key reuse.

## Advisory ledger remote

When a host is created or initialized through a local Unix-socket daemon, the
current exact Git worktree may be attached. The daemon inherits that
repository's SHA-1 or SHA-256 object format for the new ledger and maintains a
remote named `playbill` with these remote-tracking refs:

~~~text
refs/remotes/playbill/main
refs/remotes/playbill/proposals/<actor>/<name>
~~~

Advertisement uses an atomic, scoped fetch. It never checks out a commit,
changes the index or worktree, configures an upstream, or writes a user branch
or tag. A conflicting existing `playbill` remote is left untouched and reported
as a typed advisory failure. Fetch failure never rolls back proposal admission
or activation.

These refs expose ledger state for ordinary Git review. `git merge` is never
admissibility: only the signed approval and activation ceremony can advance
accepted `main`.
