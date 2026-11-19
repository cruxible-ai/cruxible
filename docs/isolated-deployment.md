# Isolated Cruxible deployment

Run the daemon with a durable state root, explicit network boundary, and
least-capable credentials.

## Local Unix socket

A Unix socket avoids exposing a TCP port:

~~~bash
uv run cruxible server start \
  --socket /run/user/$UID/cruxible.sock \
  --state-root /srv/cruxible
~~~

Clients select it with --server-socket or CRUXIBLE_SERVER_SOCKET.

A socket daemon defaults to auth off and says so when it starts: every process
of your OS user is equally trusted, and a principal ID is a claim, not
authentication. Add `--auth` when processes of the same user must be told
apart by bearer credential.

The daemon binds the socket with mode 0600. It creates a missing socket
directory with mode 0700. It refuses to start unless that directory is a real
directory you own with no group or other access, and every directory the path
passes through, including where each symlink on the way leads, is owned by you
or root and not writable by others (except a sticky root-owned directory such
as `/tmp`): anyone who could replace the socket would receive the bearer
tokens clients send. After binding it re-checks that the path still names the
directory it validated, and removes the socket and refuses otherwise. Protect the state root with operating-system ownership and
permissions. The daemon state includes bearer credential records, Cruxible Git
ledgers, CAS objects, projections, and daemon signing keys.

## Bound TCP service

For TCP, bind loopback unless a trusted reverse proxy or private network
provides the boundary:

~~~bash
uv run cruxible server start \
  --host 127.0.0.1 \
  --port 8100 \
  --state-root /srv/cruxible \
  --capability-ceiling admin \
  --auth \
  --bootstrap-secret-file /secure/cruxible-bootstrap
~~~

A TCP daemon, loopback included, refuses to start without `--auth`
(or `CRUXIBLE_SERVER_AUTH=true`). The bootstrap secret is never printed; the
daemon writes it owner-only to `<state-root>/daemon/bootstrap-secret` (and to
`--bootstrap-secret-file` when given), and local `server status|restart|stop`
read it from there.

Use TLS at the proxy for any non-loopback deployment. Never send bearer tokens
over plaintext untrusted networks. TCP-created hosts are intentionally
unattached: run `cruxible host create` and `cruxible init` outside a Git
worktree. If the daemon and workspace are local and the ledger should advertise
into that workspace, attach it over the Unix socket (`cruxible workspace
attach`), before or after initialization.

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

`cruxible instance decommission --reason "<why>"` previews; committing it
with `--commit --at OID` (the preview's coordinate) is terminal: it
ends one instance's governed writes and cannot be undone. It is ADMIN-tiered and
deletes NOTHING. Afterwards:

- every governed write -- proposals, approvals, activation, Claim
  attestations, predictions and settlements, and
  Procedure/Line runs -- refuses with the typed
  `cruxible.instance.decommissioned` error carrying the recorded reason;
- reads keep serving at the accepted coordinate, and `orient` and `next` report
  the terminal state so an agent is told why the instance is closed;
- every byte stays where it is, and the record lives in the canonical descriptor
  so it survives a daemon restart.

Archiving or erasing the directory afterwards is your own step. No verb here
performs it, and nothing restores the instance once the state is stamped: the
successor is a fresh instance from `cruxible host create`.

## Capability ceiling

The daemon capability ceiling is fixed for the process lifetime. Set it to the
highest operation the deployment should ever perform. Individual bearer
credentials may be narrower but cannot exceed the ceiling.

A read-only witness can run with a read_only ceiling and replicated accepted
ledger/CAS material. It can independently verify and serve reads without holding
owner/reviewer private keys. Under that ceiling every Line and Procedure run and
every Line dispatch refuses, because each retains a run journal and Captures, even
for an observe-only Line. It still writes some records of its own: audit-run
events, consumption receipts when they are enabled, and the floor files that
`floor deliver-now` writes into an attached workspace.

## Key custody

Daemon keys stay under the daemon-managed state root. Owner, reviewer, and
recovery private keys must stay outside that directory and outside source
workspaces.

For cloud deployments:

- keep ordinary signing keys in client custody or an external signing service;
- keep recovery keys offline or under stronger custody;
- back up each instance's `ledger.git/`, `cas/` and `exhaust/` together;
- treat SQLite projections as rebuildable;
- never bake bearer tokens or private keys into images.

## External sources

Do not mount an entire enterprise source estate into the daemon merely so it can
compile files. Compile file catalogs client-side and submit path-free bundles.
For APIs and databases, record stable source coordinates and evidence rather
than copying whole tables into Cruxible.

## Providers without index access

A kit ships its own providers inside it, and names a general-purpose provider
(such as `cruxible-provider-web`) as a published dependency at an exact
version, which `kit add` installs from the provider index: PyPI unless the
operator configured another. A daemon that cannot reach PyPI has two ways to
supply that wheel:

- **An internal index.** List it in `provider_index_urls` in
  `<state-root>/daemon/provider-runtime.json` (written while the daemon is
  stopped; see [Provider runtime operational
  configuration](cli-reference.md#provider-runtime-operational-configuration)).
  It must serve the same build: the kit records the wheel's hash, and another
  build under the same name and version is refused.
- **The files themselves.** Carry the wheel, its lock and its dependency
  wheels to the machine and install them before adding the kit: `cruxible
  provider install WHEEL --lock LOCK --dependency WHEEL` (once per dependency
  wheel). `kit add` then finds operations that serve the kit's slots and
  installs nothing by name (the preview says `satisfied`).

Either way the registry dependencies a lock pins must also be reachable, from
the configured indexes or as `--dependency` wheels.

## Recovery

Runtime admin recovery is a local filesystem-ownership operation. Cruxible
principal recovery is a governed ledger operation. They are distinct and should
have different custody and audit procedures.

Test restoration by rebuilding projections from accepted Git/CAS state, not by
assuming a copied SQLite file is sufficient.

The `exhaust/` directory is part of the instance, not a cache: it holds the
run journals, observation history, Line enablement and inbox decisions that
the ledger cites or that exist nowhere else. Back it up with the ledger and
the CAS, and restore the three together. No command prunes or clears a
journal; a long journal is the intended use.
