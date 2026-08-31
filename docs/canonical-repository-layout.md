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
ledger artifacts use the current compiler's pretty canonical JSON codec and
`.json` paths; those are separate domains.

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
Archive and re-seed them; do not transcode signed history. Historical compiler
verifiers remain available for frozen material, but a current mutable instance
must use the current layout and compiler.

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
