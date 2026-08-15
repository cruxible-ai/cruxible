# Isolated Playbill deployment

Run the daemon with a durable state directory, explicit network boundary, and
least-capable credentials.

## Local Unix socket

A Unix socket avoids exposing a TCP port:

~~~bash
uv run cruxible server start \
  --socket /run/user/$UID/cruxible.sock \
  --state-dir /srv/cruxible/playbill \
  --bootstrap-secret-file /secure/cruxible-bootstrap
~~~

Clients select it with --server-socket or CRUXIBLE_SERVER_SOCKET.

Protect the socket and state directory with operating-system ownership and
permissions. The daemon state includes bearer credential records, Playbill Git
ledgers, CAS objects, projections, and daemon signing keys.

## Bound TCP service

For TCP, bind loopback unless a trusted reverse proxy or private network
provides the boundary:

~~~bash
uv run cruxible server start \
  --host 127.0.0.1 \
  --port 8100 \
  --state-dir /srv/cruxible/playbill \
  --capability-ceiling admin \
  --bootstrap-secret-file /secure/cruxible-bootstrap
~~~

Use TLS at the proxy for any non-loopback deployment. Never send bearer tokens
over plaintext untrusted networks.

## Capability ceiling

The daemon capability ceiling is fixed for the process lifetime. Set it to the
highest operation the deployment should ever perform. Individual bearer
credentials may be narrower but cannot exceed the ceiling.

A read-only witness can run with a read_only ceiling and replicated accepted
ledger/CAS material. It can independently verify and serve reads without holding
owner/reviewer private keys.

## Key custody

Daemon keys stay under the daemon-managed state directory. Owner, reviewer, and
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
