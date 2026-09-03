# Isolated Playbill deployment

Run the daemon with a durable state root, explicit network boundary, and
least-capable credentials.

## Local Unix socket

A Unix socket avoids exposing a TCP port:

~~~bash
uv run cruxible server start \
  --socket /run/user/$UID/cruxible.sock \
  --state-root /srv/cruxible/playbill \
  --bootstrap-secret-file /secure/cruxible-bootstrap
~~~

Clients select it with --server-socket or CRUXIBLE_SERVER_SOCKET.

Protect the socket and state root with operating-system ownership and
permissions. The daemon state includes bearer credential records, Playbill Git
ledgers, CAS objects, projections, and daemon signing keys.

## Bound TCP service

For TCP, bind loopback unless a trusted reverse proxy or private network
provides the boundary:

~~~bash
uv run cruxible server start \
  --host 127.0.0.1 \
  --port 8100 \
  --state-root /srv/cruxible/playbill \
  --capability-ceiling admin \
  --bootstrap-secret-file /secure/cruxible-bootstrap
~~~

Use TLS at the proxy for any non-loopback deployment. Never send bearer tokens
over plaintext untrusted networks. TCP-created hosts are intentionally
unattached: run `playbill host create` and `playbill init` outside a Git
worktree. If the daemon and workspace are local and the ledger should advertise
into that workspace, use the Unix socket and attach before initialization;
attachment cannot be retrofitted afterward.

## One daemon per state root

`server start` takes an exclusive `flock` on `<state-root>/daemon/lock` before
it opens any store. The lock body names the holder's pid and its transport, so a
second daemon over the same state root refuses with the typed
`cruxible.server.state_root_locked` error naming that holder, having touched no
SQLite file, no ledger, and no credential record. The `flock` is the authority,
not the recorded pid: the kernel frees it however the holder died, so a stale
lock file left by a killed daemon is reclaimed by the next start rather than
blocking it. The file is created (and, if an earlier daemon left it wider,
narrowed) to mode `0600`.

Two daemons over one state root is the failure this prevents: they hold the same
SQLite files and the same ledger, they race the accepted tree, and a `/version`
probe after a restart can be answered by either image.

## Stopping a deployed daemon

Stop it with the verb, not with a signal:

~~~bash
uv run cruxible --server-socket /run/user/$UID/cruxible.sock server stop
~~~

`kill` and a terminal multiplexer's quit both kill the launching shell and
orphan the daemon, which is exactly how one state root ends up served by several
live processes. `server stop` asks the running daemon over the configured
transport to shut down gracefully, and then reports what it OBSERVED rather than
what it assumed: the daemon must stop answering over that transport, and when
its state root is a directory on the machine running the command, the lock must
also be free. `--timeout` (default 30s) bounds the wait.

The command exits NON-ZERO with the typed `cruxible.server.stop_not_confirmed`
when the root was not released, so `server stop && server start` cannot walk
into the lock refusal the stop existed to clear.

For the bound-TCP topology above, the daemon's state root is a path on the
DAEMON's host, not on the machine running the client. Release is therefore not
observable from there: the command says
`Stop requested; lock release not observable from this client.` and exits zero,
rather than claiming a release it cannot see. Confirm the release on the
daemon's own host, or through whatever supervises the process.

## Ending an instance

`cruxible playbill instance decommission --reason "<why>" --yes` is terminal: it
ends one instance's governed writes and cannot be undone. It is ADMIN-tiered and
deletes NOTHING. Afterwards:

- every governed write door -- proposals, approvals, activation, curation
  rulings, Claim attestations, predictions and settlements, Procedure binds and
  Procedure/Line runs -- refuses with the typed
  `playbill.instance.decommissioned` error carrying the recorded reason;
- reads keep serving at the accepted coordinate, and `orient` and `next` report
  the terminal state so an agent is told why the instance is closed;
- every byte stays where it is, and the record lives in the canonical descriptor
  so it survives a daemon restart.

Archiving or erasing the directory afterwards is your own step. No verb here
performs it, and nothing restores the instance once the state is stamped: the
successor is a fresh instance from `playbill host create`.

## Capability ceiling

The daemon capability ceiling is fixed for the process lifetime. Set it to the
highest operation the deployment should ever perform. Individual bearer
credentials may be narrower but cannot exceed the ceiling.

A read-only witness can run with a read_only ceiling and replicated accepted
ledger/CAS material. It can independently verify and serve reads without holding
owner/reviewer private keys.

## Key custody

Daemon keys stay under the daemon-managed state root. Owner, reviewer, and
recovery private keys must stay outside that directory and outside source
workspaces.

For cloud deployments:

- keep ordinary signing keys in client custody or an external signing service;
- keep recovery keys offline or under stronger custody;
- back up accepted ledgers and CAS objects;
- treat SQLite projections as rebuildable;
- never bake bearer tokens or private keys into images.

## External sources

Do not mount an entire enterprise source estate into the daemon merely so it can
compile files. Compile source catalogs client-side and submit path-free bundles.
For APIs and databases, record stable source coordinates and evidence rather
than copying whole tables into Playbill.

## Recovery

Runtime admin recovery is a local filesystem-ownership operation. Playbill
principal recovery is a governed ledger operation. They are distinct and should
have different custody and audit procedures.

Test restoration by rebuilding projections from accepted Git/CAS state, not by
assuming a copied SQLite file is sufficient.
