# CLI reference

The public CLI has four top-level command groups.

## Global options

~~~text
--server-url TEXT
--server-socket TEXT
--instance-id TEXT
--no-workspace
--json-compact
--version
~~~

Target resolution is component-wise and deterministic: explicit flags, then
`CRUXIBLE_SERVER_URL` / `CRUXIBLE_SERVER_SOCKET` / `CRUXIBLE_INSTANCE_ID`, then
the attached workspace discovered from `CRUXIBLE_PLAYBILL_WORKSPACE` or by
walking up from the current directory to `.playbill/coverage.json`, then the
remembered global context. Automatic walk-up stops after the home directory and
never crosses a filesystem boundary. `--no-workspace` or
`CRUXIBLE_NO_WORKSPACE=1` disables workspace discovery for recovery from a bad
ancestor binding. A workspace is attached when that file names an instance and
exactly one of `server_url` or `server_socket`; its root must agree with the root
of `.playbill/sources.yaml` when both exist. The global context is only a
fallback, its remembered instance remains bound to the transport on which it
was selected, and entering one workspace never retargets another.

Two environment variables bound how long a client waits for a daemon that has
accepted a request. `CRUXIBLE_CLIENT_TIMEOUT_S` (default 180) is the read budget
for an ordinary call. `CRUXIBLE_CLIENT_CONNECT_TIMEOUT_S` (default 900) is the
separate, larger budget for the single orientation an SDK `Playbill.connect()`
runs when it opens a session: orientation folds the whole accepted world, so its
cost tracks the size of the instance rather than the size of the call, and a
large healthy instance must not read as an unreachable server. Raising
`CRUXIBLE_CLIENT_TIMEOUT_S` above the connect budget raises the connect budget
with it. Either variable is refused, typed, unless it names a positive number of
seconds. A timeout never means the request failed: the daemon may still be
running it, so verify state before retrying.

## context

Manage remembered daemon and instance context. `context show` reports the
resolved target, workspace, and the source selected for each target component.
It reports workspace-config attachment separately from daemon host registration;
for local sockets, a mismatch is a typed attachment-disagreement row rather than
silently treating those two notions as equivalent:

~~~text
cruxible context connect
cruxible context use
cruxible context show
cruxible context clear
~~~

## credential

Manage runtime bearer credentials:

~~~text
cruxible credential claim-bootstrap
cruxible credential mint
cruxible credential list
cruxible credential rotate
cruxible credential revoke
cruxible credential recover-admin
~~~

These credentials authorize transport operations. They are distinct from
Playbill signing principals.

## server

~~~text
cruxible server start [--state-root DIR] [--socket PATH | --host HOST --port PORT]
cruxible server install-service [SERVER-START FLAGS] [--print] [--replace]
cruxible server status
cruxible server info
cruxible server restart
cruxible server stop [--timeout SECONDS] [--json]
~~~

server start is the long-running daemon process and does not connect to an
existing server. State defaults to `~/.cruxible`; `--state-root` overrides
`CRUXIBLE_STATE_ROOT`. The obsolete `CRUXIBLE_SERVER_STATE_DIR` name is
refused. See [Canonical repository and daemon layout](canonical-repository-layout.md)
for the exact directory contract.

`server start` takes an exclusive lock on `<state-root>/daemon/lock` before it
opens any store, so a second daemon over the same state root refuses with a
typed `cruxible.server.state_root_locked` error naming the holder's pid and
transport rather than sharing its SQLite files and ledger. The lock is an
`flock`, so the kernel frees it however the holder died and a stale file from a
killed daemon never blocks the next start.

`server stop` is the way to stop a daemon: it asks the running daemon over the
configured transport to shut down gracefully, then waits for the release and
says what it actually observed. Reaching for `kill` or a terminal multiplexer's
quit instead kills the launching shell and orphans the daemon, which is how one
state root ends up served by several live processes.

The release is observed, never assumed. The daemon must stop answering over the
configured transport; when its state root is a directory on the machine running
the command, its `flock` must also be free. `--timeout` (default 30s) bounds
that wait. The command exits NON-ZERO with the typed
`cruxible.server.stop_not_confirmed` when the root was not released, so
`cruxible server stop && cruxible server start` cannot walk into the lock
refusal the stop existed to clear. Against a daemon bound to TCP on another
host, the state root is not a path this machine has: the command then prints
`Stop requested; lock release not observable from this client.` and exits zero
rather than claiming a release it cannot see. `--json` reports the same two
observations as `daemon_exited` and `state_root_released` (`null` when the lock
is not observable from here).

`server install-service` renders a per-user launchd agent on macOS or systemd
user unit on Linux. It records the resolved `cruxible` executable and explicit
state-root, transport, capability-ceiling, and auth settings under the daemon
state root; a later `--print` with that state root revalidates and renders the
record without writing. Installation refuses an existing unit unless
`--replace`, loads/enables the unit, and does not start it. Start it separately
with `launchctl start ai.cruxible.daemon` on macOS,
`systemctl --user start cruxible.service` on Linux, or run
`cruxible server start`. Auth defaults to the state root's durable auth latch;
an explicit `--auth`/`--no-auth` disagreement is refused. Service files contain
no bearer or bootstrap secret, and auth-on installation requires an active
durable runtime credential first.

`server status` lists the daemon's exact current compiler coordinate and each
governed host as `uninitialized`, `writable`, or `reseed_required`, retaining a
typed reason for malformed or retired state. Its `Instances` count is the number
of governed daemon hosts shown, excluding unrelated local registry entries.
`server status` and `server info`
also render `Provider lane:` and, when degraded,
`Provider lane reason:`. Provider-lane degradation never prevents the daemon's
non-Provider surfaces from starting, so these lines are the operator's recovery
signal rather than a daemon-startup failure. When transient process-table reads
fail without degrading the lane, `Provider lane detail:` reports the bounded
observation-diagnostic count, retained ring occupancy, and last typed message;
JSON clients read the same text in `provider_lane.detail`. The lane's
`isolated_executors` field lists the backend ids of the isolated Provider
executors this daemon discovered at start, sorted; it is empty in core, which
ships none. A daemon that could not load an executor an installed distribution
advertises does not start at all, so an empty list means nothing advertised
itself, never that something advertised itself and failed.

### Provider runtime operational configuration

The local operator may write `<state-root>/daemon/provider-runtime.json` while
the daemon is stopped. This file is daemon-local operational configuration, not
governed state; agents and Provider children never write it. Its closed v1 shape
has tag `cruxible-provider-runtime-operational-config-v1` and these entries:

| Entry | Default | Purpose |
|---|---:|---|
| `lease_acquisition_timeout_seconds` | `5.0` | Child lease/echo acquisition deadline. |
| `lease_recovery_timeout_seconds` | `5.0` | Per-record process-fence recovery deadline. |
| `recovery_aggregate_timeout_seconds` | `30.0` | Aggregate deadline for one recovery scan; later records remain for the next scan. |
| `rearm_backoff_seconds` | `5.0` | Minimum delay before another lazy recovery attempt; calls inside the window refuse immediately with the retained reason. |
| `secret_writer_join_timeout_seconds` | `5.0` | Secret-pipe writer join deadline. |
| `stdin_writer_join_timeout_seconds` | `5.0` | Provider-input writer join deadline. |
| `descendant_tracker_join_timeout_seconds` | `5.0` | Descendant-observer join deadline. |
| `descendant_tracker_poll_interval_seconds` | `0.1` | Cross-session descendant observation interval while a child is alive; each poll reads the host process table, so shorter intervals trade CPU and process-spawn cost for a smaller best-effort observation window. Transient failures appear as bounded observation diagnostics in Provider-lane detail. |
| `process_group_termination_timeout_seconds` | `5.0` | Child group termination and verification deadline. |
| `deployments` | `[]` | Digest-keyed local Provider deployment records. |
| `provider_repository` | `null` | Operator-configured provider repository used by `provider list` and name-based installs. |
| `provider_index_urls` | `[]` | Explicit allowed dependency indexes/download origins. Without these, supply locked dependency wheels. |
| `workspace_allowed_roots` | `[]` | Canonical absolute roots that widen `workspace.file` beyond an attached workspace; these are daemon-local authority and never come from an environment variable. The daemon state root, its trust, custody, Provider-secret, and instance substrate stay refused inside any allowed root. |

Unknown entries, non-positive timing values, malformed JSON, unsafe deployment
paths, and an unreadable file degrade only the Provider lane with a typed cause.
Provider installation requires the first-party `cruxible-provider-runtime` toolchain
and `uv` in the daemon environment. The 0.2.0 runtime may be installed from a locally
built wheel before publication. No provider-specific Python is imported into Core.
An exhausted aggregate recovery scan reports untouched records as
`not_attempted`; a later lazy re-arm resumes from the retained records after the
configured backoff. A lazy re-arm also retries exactly the construction stages
that failed. Repairing the named filesystem cause therefore restores the cached
operator without a restart when re-initialization succeeds; restart the daemon
only when the repaired stage continues to fail re-initialization.

As a last-resort process-fence repair, stop the daemon, independently prove the
recorded process group and descendants are no longer live, and remove only the
exact stale JSON record under `<state-root>/daemon/provider-process-leases/`.
Removing a record while its process may still be live abandons the recovery
identity and is unsafe; prefer repairing the typed cause and allowing re-arm.

`<state-root>/daemon/proposal-receive.json` is a second daemon-local operational
file, tag `cruxible-proposal-receive-operational-config-v1`, with one entry:

| Entry | Default | Purpose |
|---|---:|---|
| `max_changed_members` | `5000` | How many changed members one submission may carry. An ADMISSION ceiling on receive, never a product ceiling on authoring: one intent is one changeset and may carry any mix of members. Derivative cards are not counted. |

An absent file is the default. A file that exists and cannot be read as this
shape refuses loudly rather than silently restoring the default bound.

The admitted limits a caller reads back carry two further numbers, which are
ADVERTISEMENTS rather than receive gates: `max_change_set_record_bytes`
(`67108864`, the per-blob ceiling the ledger's own record of a change set is
written under) and `change_set_record_bytes_per_member` (`11264`, the largest
measured cost of one ENTRY in that record, over every member kind). Their
quotient -- 5,957 record entries at the defaults -- is the largest change set
that can be settled, and `max_changed_members` is 5,000, so the member budget a
caller is told is the member budget that settles: 5,000 entries project to
56,320,000 bytes, about 53.7 MiB, under the ceiling. They were two different
numbers until the ledger's per-blob read ceiling rose from 4 MiB to 64 MiB, and
the disagreement arrived at activation, after the compile had been paid for.

The same raise lifts a ceiling on evidence: receive admits a single member of up
to 8 MiB, and a captured source over 4 MiB used to be admissible and then
unreadable, so a source that size may now back a Claim. Operators who mirror a
ledger to GitHub should note that GitHub warns on any pushed file over 50 MB and
rejects one over 100 MB, so a change-set record approaching the new ceiling can
trip that warning on a mirrored remote even though the daemon accepts it. That
is a deployment concern rather than a second product ceiling.

The record holds one entry per changed PATH, not one per authored member, and
two member kinds lower to more than one: a ClaimType succession writes the
successor plus an entry per dependent it dispositions, and a Claim retirement
writes the retired Claim plus its live closure. So 5,957 is the largest set of
1:1 members the record can hold, `max_changed_members` sits under it, and a set
carrying either of those kinds settles at however many entries it lowers to. An intent over the bound is refused at
preflight, typed `playbill.authoring.change_set_record_too_large`, naming the
entry count that fits: on the projected count before anything is lowered when
that already exceeds the bound, and on the exact lowered count -- still before
the compile -- when it does not.

## playbill host

~~~text
cruxible playbill host create [--instance-id ID] [--workspace DIR] [--replace]
cruxible playbill host show INSTANCE [--json]
cruxible playbill workspace attach [--instance-id ID] [--replace]
cruxible playbill workspace detach [--instance-id ID] [--json]
~~~

Allocates an empty daemon-owned host and remembers it. When the selected daemon
is reached through `--server-socket` or `CRUXIBLE_SERVER_SOCKET`, the command
also registers the selected Git worktree with the daemon. Every selected
workspace gets an atomic `.playbill/coverage.json` v2 write containing exactly
one transport, the instance ID, and the fixed floor profile; bearer credentials
and secrets are never inputs to that writer. A differing config is refused
unless `--replace` is explicit. Because the binding may carry a local socket,
the writer adds `.playbill/coverage.json` to this repository's machine-local
`.git/info/exclude` rather than changing a shared ignore file.

A TCP client never sends its local path to the daemon. Implicit attachment from
inside a TCP worktree remains refused; explicit `--workspace DIR` instead writes
a client-local `server_url` binding without claiming daemon registration. Use a
local socket when the daemon must advertise ledger refs into that worktree.

With auth on, `host create` is authorized by the daemon's runtime bootstrap
secret, which is its unscoped operator credential. That authorization is
repeatable, exactly as it is for `server info`, `server restart` and
`server stop`: a daemon hosting several instances allocates each of them with
the same secret, and `credential claim-bootstrap` -- which stays one-shot --
does not revoke it. An instance-scoped credential cannot allocate a host on the
daemon that hosts it, and the refusal names the bootstrap secret as the
credential to present.

`host show` is a zero-authority inspection of workspace registration, exact
compiler coordinate/revision, and write compatibility; the CLI adds the selected
transport. The daemon-local managed root is visible only to an unscoped operator,
not an instance-scoped credential. `workspace attach` is client-local and requires a Unix
socket: it writes `.playbill/coverage.json` for an existing host only after the
daemon proves that it registered the exact current Git worktree. A missing or
different registration is a typed refusal and no config is written.

`workspace detach` releases a host from the worktree it registers. The registry
holds one host per worktree, so moving a worktree to a second host needs the
first one released; nothing governed changes, the host keeps its ledger and
every read it has ever served, and it stops being the host of this directory.
It requires the same local socket for the same reason attaching does. It refuses
while the host still registers published blocks in that worktree, because
detaching under them leaves a page carrying markers no host owns: depublish
those blocks (`playbill block depublish`) or retire their backing Claims first.

## playbill init

~~~text
cruxible playbill init --key-dir DIR
  [--principal-id ID]
  [--reviewer-key-dir DIR]
  [--require-independent-approval]
  [--recovery-key-dir DIR]
  [--recovery-principal-id ID]
  [--profile local|cloud]
  [--workspace DIR] [--replace]
  [--object-format sha1|sha256]
  [--mirror-url URL]
~~~

Generates a client-held ordinary key outside the workspace and bootstraps the
ledger with its public principal record. An optional `--reviewer-key-dir` adds a
second ordinary principal; pair it with `--require-independent-approval` to make
one non-creator approval mandatory. Local key directories provide attribution
and repository hygiene, not a security boundary. Organization review normally
rides branch protection/CODEOWNERS on the state repository; real custody
separation belongs at the parked Cloud broker/leasing seam. The optional
recovery key remains lifecycle-only.

All target, topology, principal, and custody-path checks run before key
generation. If the server response is lost after generation, each custody pair
has a transport- and instance-bound local retry marker; the exact retry adopts
that pair and clears the marker after success. This is not general key import:
existing keys without the matching marker are refused. Re-seed with fresh
owner, reviewer, and recovery custody by default; see
[Canonical repository and daemon layout](canonical-repository-layout.md).

Successful initialization remembers the initialized instance and atomically
writes the selected workspace config before rendering either JSON or human
output. For a daemon-registered local worktree, the advisory `playbill` remote
fetches accepted state as `playbill/accepted` and open proposals as
`playbill/proposals/<proposal-digest>`. These are remote-tracking refs only:
compare them, never check them out or merge them to admit governed state.

Explicit `--workspace DIR` over TCP writes only the client-local URL binding.
An instance initialized without daemon registration cannot acquire one later;
archive and rebuild an attached host through the local socket when ledger-ref
advertisement is required.

The ledger's Git object format follows `--object-format`: with no flag it
inherits an attached workspace's format, and with no workspace it is `sha1`.
SHA-1 is the default because common Git viewers do not recognize a SHA-256
repository, and a ledger nobody can open is not evidence anyone can read
(maintainer ruling, 2026-09-03). An explicit `--object-format` that contradicts
the attached workspace refuses with the typed
`playbill.init.object_format_conflict` before any state is written; instances
already initialized keep their pinned format forever. The
equivalent request field is `git_object_format` on the HTTP/SDK init body and on
MCP `cruxible_playbill_init`.

`--mirror-url` binds the ledger mirror during bootstrap, before subsequent
proposals. An instance can publish nowhere initially; `playbill ledger set-mirror`
adds a destination later. See [playbill ledger](#playbill-ledger) for URL syntax.

Initialization creates governed state only. Install provider packages separately
with `playbill provider install`; initialization needs no provider checkout or
executable environment.

## playbill capture

```bash
cruxible playbill capture read CAPTURE_DIGEST [--max-bytes BYTES]
```

Verify a retained Capture and return its evidence metadata and bounded material as JSON.
Uses body-read permission and never refetches the external source. The SDK equivalent
is `pb.capture(digest)`; its `.ref` can be passed to Claim authoring as `supported_by`.

## playbill body

~~~text
cruxible playbill body store PATH
~~~

Stores exact bytes in inert CAS and prints their digest.

## playbill instance

~~~text
cruxible playbill instance decommission --reason TEXT --yes
~~~

Decommissioning is the terminal lifecycle state of one governed instance. It
stamps the reason, instant, and actor on the instance descriptor, so a daemon
restart replays the same state. Every further governed write refuses with the
typed `playbill.instance.decommissioned` error naming the reason and the repair;
reads keep serving at the accepted coordinate, `next` reports the terminal state,
and `search --mode orient` marks the orientation decommissioned.

Nothing is deleted. Every accepted generation, receipt, and body stays exactly
where it is, and archiving or erasing the directory afterwards is the operator's
own step — no verb performs it, and the state cannot be reversed, so `--yes` is
required.

## playbill ledger

~~~text
cruxible playbill ledger set-mirror URL
cruxible playbill ledger clone-url
cruxible playbill ledger publish [--timeout 0..60] [--json]
~~~

The ledger is Git, so review is Git — but only for a reviewer who can reach the
ledger, and the daemon's copy is a bare repository under the instance root that
nobody else can open. A mirror is how the review flow leaves the host. Bind one
and the daemon schedules background publication after proposal submission,
approval, activation and withdrawal. Local writes return after durable local
completion; the publisher combines pending work into exact ref snapshots.

What travels: `refs/heads/main`, whichever of `refs/notes/playbill-gen`,
`refs/notes/playbill-eval` and `refs/notes/playbill-approval` exist, one branch
per OPEN proposal under `refs/heads/proposals/`, and `refs/settled/archive` once
there is retained closed work. Activated, withdrawn and stale review branches
are removed; their commits and refused admission commits stay reachable through
that single archive ref. Historical review, readmission and curation keep the
candidate bytes after Git collection. Old `refs/settled/<digest>` links no longer
resolve; retained commits can be opened by their exact OID. To retain this history
in a reviewer clone, fetch the custom archive ref explicitly:

~~~bash
git fetch origin refs/settled/archive:refs/settled/archive
~~~

Local legacy archive refs are folded into the single ref before deletion.
An old mirror or saved mirror state with per-proposal archive refs is refused.
Bind a freshly created mirror at a new URL to start a clean publication snapshot;
this batch does not migrate remote state.
The archive has fixed-size publication metadata, while its retained objects and
ancestry grow with history. It is retention metadata, not accepted-state authority.

**The mirror branch is named by the PROPOSAL DIGEST, not by actor and name.**
Two ref namespaces exist and they are keyed differently on purpose. The
daemon's own transport ref is `refs/proposals/<actor>/<name>` — an actor writes
to a ref they own, and each evaluated snapshot is parented on its accepted base. The branch a
reviewer sees, locally as `playbill/proposals/<proposal-digest>` and on the
mirror as `refs/heads/proposals/<proposal-digest>`, is the projection of ONE
evaluated candidate, which is what a digest names and what a name does not: the
same `<actor>/<name>` ref carries a different candidate after every
resubmission. So `git diff playbill/accepted...playbill/proposals/<proposal-id>`
takes the digest that `proposal list` and `proposal review` print.

Re-keying that branch to `<actor>/<name>` is a deprecate-then-remove candidate,
not a rename: `review open` resolves those ref names and the workspace
advertisement fetches that refspec, so both are shipped surfaces. It would also
have to answer what a resubmitted proposal's branch means, which the digest
answers by construction. Nothing schedules it today.

The publisher captures exact object IDs and pushes them atomically. `main`
always fast-forwards. Updates and deletions of known review refs use explicit
expected-old-ID leases; unexpected remote changes report divergence. A stale
push cannot update notes while its main update is rejected. Remote hosts must
support atomic Git pushes. Snapshots exceeding the current 64 KiB argument
budget report a publication failure rather than splitting the atomic update.

A push that fails never refuses the write that preceded it. The ledger on disk
is the record and the remote is a copy, so a network that is down, a credential
that expired or a remote that was deleted becomes the `ledger_mirror_behind`
warning row in `playbill next`, carrying the URL and Git's own reason.

Remote discovery and push each have a 30-second deadline; one attempt may
therefore take up to roughly 60 seconds. These commands run in a background
worker. Failed attempts retry at most three times for a request; newer work,
explicit publication or reopening can start another attempt. Reopening repairs
missed scheduling from durable ledger and evidence records.

Use `ledger publish` before relying on a remote review view. It requests
publication and waits at most `--timeout` seconds (default 60); zero only queues
work. The JSON receipt includes `wait_sequence`, `published_sequence`, and the
exact `published_refs` acknowledged by the remote. The barrier succeeded when
`wait_sequence` is non-null and `published_sequence >= wait_sequence`. Newer work
may still be `pending` or `publishing`. Failure is `behind`; timeout returns the
actual pending/publishing status. A destination change interrupts the old wait.
A success acknowledges that snapshot at that time, not permanent remote durability.
`clone-url` keeps stdout as the URL and reports publication status on stderr;
`--json` returns both. Missing local status is rebuilt; it is not ledger authority.

The URL never carries a credential. `https://user:token@host/...` is refused,
as is plain `http://`, `ext::` and anything whose host or user begins with a
dash (`ssh://-oProxyCommand@host/x` puts it where the transport reads its own
arguments); the four
accepted shapes are `https://`, `ssh://`, `user@host:path` and an absolute local
path (or its `file:///` spelling). The daemon reads its own token from
`CRUXIBLE_PLAYBILL_MIRROR_TOKEN` in its environment and sends it as an HTTP
Basic `Authorization` header built through Git's environment-config protocol, so
it appears in no command line, no config file and no error message. SSH and
local remotes use no token at all: SSH authenticates as the daemon itself.

Create the remote in the ledger's own object format — `git init --bare
--object-format=sha1` or `sha256`, matching `playbill init --object-format` —
because Git refuses a push between repositories with different hash algorithms.

`set-mirror` publishes immediately, so a wrong credential or an unreachable host
is reported at once rather than at the next governed write. It stays bound
either way: a remote that is temporarily unreachable is not a wrong remote.
`clone-url` prints the URL a reviewer clones and refuses with the typed
`playbill.ledger.mirror_unset` when the instance publishes nowhere; the same
value rides `playbill orient --json` as `orientation.mirror_url`, so an agent
that has just oriented already has it. The equivalent surfaces are
`POST`/`GET /{instance}/playbill/ledger/mirror` and the `mirror_url` field on
the init body.

## playbill provider

~~~text
cruxible playbill provider list [--json]
cruxible playbill provider install PACKAGE_OR_WHEEL [--lock FILE]
  [--dependency WHEEL]... [--extra NAME]... [--reverify] [--json]
~~~

Installation requires **ADMIN**. A package name resolves through the daemon's
configured repository. A local wheel requires `--lock`; `--dependency` supplies
local or offline locked dependency wheels. Local paths are read by the client
and transferred through CAS, so this also works against a remote daemon.

The shared installer prepares an exact Python environment, verifies it once,
checks package classifiers in supervised children, and proposes the package's
node-type interfaces and Provider definition through ordinary acceptance.
It returns `ready`, `awaiting_approval`, or `blocked`, with per-operation missing
requirements. Missing browser resources remain explicit; Python extras do not
install browsers. Credentials, grants, and invocation remain separate.

Retries reuse the prepared installation and an open registration proposal.
Package updates create a new environment and preserve earlier deployments.
Runs reuse the retained verification record without hashing the environment.
Treat installed environments as immutable; `--reverify` detects manual changes
and refuses drift instead of silently resealing or repairing it.

SDK: `install_provider_package(client, instance_id, wheel=..., lock=...,
dependency_wheels=(...))`, or `client.install_playbill_provider` with a typed
request. MCP: `cruxible_playbill_provider_catalog` and
`cruxible_playbill_provider_install`. HTTP: `GET /{instance}/playbill/providers`
and `POST /{instance}/playbill/providers/install`.

## playbill document

~~~text
cruxible playbill document propose --envelope FILE --name NAME
cruxible playbill document list
cruxible playbill document get IDENTITY
cruxible playbill document body IDENTITY [--output FILE]
cruxible playbill document history IDENTITY
~~~

## playbill subject

~~~text
cruxible playbill subject propose --envelope FILE --name NAME
cruxible playbill subject list
cruxible playbill subject get KIND/ID
cruxible playbill subject history KIND/ID
~~~

A Subject is an identity-only referent named by its canonical `kind/name`
address — the spelling the SDK, claim objects, floor profiles, and `explain` all
use. The two-argument `KIND ID` form is deprecated and still accepted; it emits
the structured deprecation warning on stderr and is removed in 0.6.0.

`subject get` renders the Subject's own facts and an `incoming` section: every
live Claim whose subject-valued object is this Subject, grouped by predicate and
naming the asserting Subject and the Claim id. A relation is stored once, on the
asserting Subject, so without this section nothing answers "what touches this
package" from the object side.

## playbill claim-type

~~~text
cruxible playbill claim-type propose --template
cruxible playbill claim-type propose --input FILE --name NAME
cruxible playbill claim-type migrate REQUEST_FILE
cruxible playbill claim-type list
cruxible playbill claim-type get PREDICATE
~~~

A ClaimType is the governed interface a predicate must satisfy before any Claim
may state it. `propose --input` accepts a complete `ClaimTypeInputV1`; ClaimType
is not part of the authoring coordinator's example vocabulary. `propose
--template` prints a complete literal `project.work_item.status` input with a
`repo.replace-me` foreign-source evidence rule and does not contact the daemon.
Replace `anticipated_source_ids` with the logical source used by `authoring bind`;
the source-intent lint then names the deterministic foreign-source
CaptureContract digest to place in the rule. Flow-A binding carries that exact
contract into the governed Claim candidate, so accepting the bound Claim accepts
the contract and gives the rule a shipped evidence producer. The dormant
direct-self-asserted constant has no production producer or acceptance surface
and is not a template prerequisite. The input command lowers the tagless form
and returns nonblocking policy/source lint beside the proposal. The optional,
sorted `anticipated_source_ids` input supplies
source-specific repair suggestions without entering the governed ClaimType.
Expert proposals, migration preflight and submission, and SDK cold-dependency
preflight deliver the same typed lint; advisories never enter candidate identity,
the approval frontier, or its certificate. `migrate` atomically succeeds the
ClaimType and disposes every dependent the request names; it never authors
retirement decisions from diagnostics.

`migrate` is the operator form of a ClaimType succession: one vocabulary change,
built from a request file, with nothing else in the generation. The agent form is
the `claim_type_succession` change-set member under `playbill authoring`, which
lands the succession in the same generation as the Claims that speak the new
vocabulary and adds one disposition the operator form has no use for --
`re_author`, whose successor is a sibling Claim member of the same set. Both
roads build their candidate with the same function, so neither can drift from
the other's law.

## playbill claim

~~~text
cruxible playbill claim retire IDENTITY REQUEST_FILE
cruxible playbill claim attest IDENTITY --support|--contradict|--unsure [--note TEXT]
cruxible playbill claim list [--subject PATH] [--predicate P] [--include-retired]
cruxible playbill claim get IDENTITY [--brief]
cruxible playbill claim history IDENTITY
cruxible playbill claim explain IDENTITY [--evaluation-time TS]
~~~

Claims are authored through `playbill authoring create`/`compile`; the retired
direct v1 proposal commands are not a second writer. `retire` preflights or submits one
attributed retirement over the complete dependent Claim closure; the request
must name every dependent reason and never receives a daemon-synthesized end
time. explain returns the verdict together with the law evidence and source
handles it was computed from.
`claim get --brief` renders the typed subject, predicate, object, role,
qualifier, flat lifecycle state, and predecessor digest. JSON returns the same
shape in the top-level `statement` field alongside the canonical envelope.

## playbill claim-attestation

~~~text
cruxible playbill claim-attestation recover
~~~

Recovery is an admin-only repair for an interrupted evidence-ledger append. It
rolls the sole durable unpublished event forward and refuses rather than choosing
between ambiguous histories.

## playbill authoring

~~~text
cruxible playbill authoring create PAYLOAD
cruxible playbill authoring create --example claim-flow-a|claim-self-source|claim-subject-relation|procedure|change-set|claim-type-succession
cruxible playbill authoring get INTENT_ID
cruxible playbill authoring resume INTENT_ID
cruxible playbill authoring list
cruxible playbill authoring compile PAYLOAD [--intent-id INTENT_ID]
cruxible playbill authoring bind --file PATH --anchor TEXT [--occurrence N]
  [--window-lines N]
  --payload-file CLAIM_STUB
cruxible playbill authoring preflight INTENT_ID
cruxible playbill authoring rebase INTENT_ID
cruxible playbill authoring submit INTENT_ID
cruxible playbill authoring status INTENT_ID
cruxible playbill authoring abandon-insertion INTENT_ID [--expectation-id ID]
~~~

One authoring intent is one changeset. The tagless `change_set` input carries
any mix of members -- `claim`, `claim_type`, `claim_retirement`, `subject`,
`query_definition`, `procedure`, `procedure_mandate` -- and the whole intent
lowers once, proposes once and admits or refuses together, typed to the member
index that offends. `approval_policy` and `procedure_runtime_policy` are the
two exceptions: the member union parses either, but a change set carrying one
refuses whatever else it holds, so author each as its own singleton input. A
Claim member may define the Subject and ClaimType it needs in the same set, and
may retire a Claim the set does not otherwise touch. Two sibling Claims contending for
one cardinality-one slot are un-authorable in a single set by construction, not
merely unrepaired: dispositioning one needs the other's Claim ID, which the
daemon mints only at create from the already-frozen payload, so that refusal's
repair is to merge the two decisions or split the set. `--example change-set`
prints a mixed set to start from, and `--example claim-type-succession` prints a
vocabulary evolution.

A `claim_type_succession` member succeeds an accepted ClaimType and settles its
whole reverse-pin closure in the same generation. Members lower in dependency
order -- definitions, then successions, then Claims, then retirements -- so a
Claim member after a succession is lowered under the successor vocabulary and is
never one of its dependents. A set cannot define a ClaimType and succeed it:
both members author the same artifact path, and the set refuses
`playbill.authoring.change_set_member_path_collision`. The
`successor` is a whole ClaimType naming its predecessor by identity and pinning
that predecessor's exact digest; `dependents` is the exact closure computed over
the staged tree. Each dependent takes `successor` (carry it, re-pinned),
`retire` (a tombstone, with `claim_retirement_reason` `was-rescinded` for a
rescission, `was-wrong` for a statement that was false, or `superseded` for the
ordinary case of a statement that stood under a shape a later ruling replaced)
or `re_author` (a sibling Claim member of the same set, naming that
Claim again under the successor, named by `successor_claim_id`). The standalone
route's fourth, deprecated word `invalidation` parses here and refuses typed,
naming `cruxible playbill claim-type migrate` as the road that still tolerates
it. A successor that changes `object_kind` refuses `successor`
for any live Claim dependent. `cruxible playbill claim-type migrate` is the
operator form of the same law and builds its candidate with the same code.

There is no semantic member ceiling; how many
changed members one daemon will receive in a single submission is the operator's
`max_changed_members` bound in `daemon/proposal-receive.json`. Settling a change
set additionally requires it to fit under the advertised change-set record
ceiling described there, which preflight checks before lowering anything.

No intent publishes a Claim into a page any more, and none mints a publication
expectation. `abandon-insertion` releases one an instance already holds, and
because a change set that published several Claims owns one expectation per
publishing member it takes an `expectation_id` naming the one it is about; a
singular Claim intent owns exactly one and may omit it. `playbill block
depublish SOURCE_ID BLOCK_ID` is the same release addressed the way a page names
it, and is the verb to reach for.

The authoring coordinator owns stable identities, timestamps, bases, and proposal
references. `compile` creates or updates an intent and performs a binding preflight;
`rebase` advances an unsubmitted refused intent to the current accepted coordinate;
`submit` is idempotent and never supplies approvals. `status` reports the remaining
approval or activation conditions without impersonating the actors who own them.
`bind --occurrence N` counts matching anchors in ascending byte-offset order and
selects the 1-based `N`th match. The resulting selector records the total number
observed while its start/end bytes name the selected occurrence; multiple matches
are therefore truthful input metadata, not an unresolved selection.
Use `--example claim-subject-relation` for a subject-valued Claim such as
`sec.vulnerability/<cve> → sec.vuln.affects_package → sec.package/<package>`.
Both endpoint Subjects must already be accepted and admitted by the ClaimType.
A projection block's marker grammar is a page-level shape rather than an
authoring one, and it is documented under `playbill block`. Nothing composes it
for a caller any more: the marker must start in column zero, blocks cannot
overlap, nest, or repeat an id, and marker-looking text inside a Markdown fence
is not a declaration.

## playbill policy

~~~text
cruxible playbill policy list [--json]
~~~

Lists the live standalone and embedded governed policies at the accepted coordinate.

## playbill query

~~~text
cruxible playbill query list
cruxible playbill query get NAME
cruxible playbill query run NAME [--parameters FILE] [--evaluation-time TS]
~~~

run executes one accepted QueryDefinition and prints its
`playbill-query-execution-receipt-v1`: the definition digest, the resolved
parameter digest, and the result digest that replays it.

Author named queries through `playbill authoring compile`, then submit the intent
and review/accept its proposal. `cruxible playbill query propose` is deprecated.
The SDK equivalent is `pb.query_definition(definition=QueryDefinitionInput(...)).prepare()`, followed
by the normal intent submission and approval flow. `pb.changes().query_definition(...)`
includes a query in a changeset. Omitted ClaimType pins resolve against the intent
base or sibling definitions; explicit pins remain assertions. SDK `vocabulary=`
accepts World ClaimType references and retains their stale-reference checks.
Use `pb.run_query(name)` to read the accepted result and receipt.

`authoring create --example query-claims-by-type` provides a Claim query without
placeholder digests. `--example query-ontology` and `--example query-procedures`
provide artifact queries; MCP's authoring-example and authoring-compile tools use
these same typed inputs. Both are `query_definition` inputs.

Artifact queries use `playbill-query-definition-v2` and
`playbill-query-artifacts-entry-v2`. Select `ClaimType` or `Procedure`:

- `selection: all` selects all live definitions of that kind.
- ClaimTypes support `selection: namespaces` with a sorted, nonempty `namespaces`
  list. Membership is exact: `security.asset.owner` is in `security.asset`,
  while `security.asset.deep.value` is in `security.asset.deep`.
- Procedures support `selection: name_prefixes` with sorted prefixes ending in
  `.`. `security.` selects `security.observe`, but not `security_other.observe`.
  This is an explicit naming convention, not inferred domain membership.

The result shape is `artifact_definition`, cardinality `many`, and dedupe
`artifact`. Results are ordered by identity and include the typed definition,
path, and artifact digest at the requested accepted coordinate. They do not
traverse Claim edges. Only result budgets apply; traversal depth must be zero.
An initially empty result still tracks future membership. Out-of-scope changes
do not alter the semantic result. Truncation remains explicit; the SDK's
`result.artifact_definitions` accessor refuses truncated or refused listings.

Query-backed blocks detect selected definitions being added, retired, removed,
or changed. Their backing proves currency, not that handwritten prose lists
every result. Generate a complete list from an untruncated result and keep that
separate from freeform prose; sync does not render Markdown or HTML.

## playbill procedure

~~~text
cruxible playbill procedure readiness NAME --evaluation-time TS
cruxible playbill procedure bind NAME REQUEST_FILE
cruxible playbill procedure run NAME INPUT_FILE --evaluation-time TS
cruxible playbill procedure status RUN_ID
cruxible playbill procedure measure NAME [--run-id RUN_ID] [--measurement NAME]...
  [--evaluation-time TS] [--at FILE] [--json]
cruxible playbill procedure readings NAME [--run-id RUN_ID] [--measurement NAME]...
  [--limit N] [--cursor C] [--json]
~~~

The served lanes run deterministic `state_tap`, `transform`, `project`, `guard`,
`repeat` and `halt` graphs, plus `source` on a graph-v4 definition: a Procedure
may READ an external source through an accepted Provider. On the DIRECT lane
the effectful terminals -- `emit_capture`, `post_inbox`, `propose_change_set`,
`settle_change_set` -- are not served, and `readiness` lists them as
unsupported nodes before execution: a direct invocation carries no requested
rung, no occurrence, and no mandate coordinate, and none is fabricated for it.
The Line lane serves `propose_change_set`, and `settle_change_set` under a live
settle ProcedureMandate; see `playbill line`.

A Source run needs accepted state to authorize it: a live
SourceAcquisitionPolicy, the CaptureContract each Source node pins, and the
Provider closure it names. A Procedure names its policy on its own envelope,
under the pin role `acquisition-policy` -- authored by naming the policy, the
way a Line names its own -- and a pinned Procedure reads only that policy, so
what anyone accepts afterwards cannot change what it does. A Procedure with no
such pin falls back to accepted state: exactly one live SourceAcquisitionPolicy
whose declared inputs are exactly the Procedure's Source aliases. The direct
lane refuses `source_acquisition_policy_required` when the pinned policy does
not declare this Procedure's Source inputs, or when no single policy applies to
an unpinned one, and `source_acquisition_refused` when the policy's own rule
denies a declared input; neither leaves run history behind. A read outside an
authorized workspace root, over the CaptureContract's selection budget, or with
no daemon-local reader refuses `workspace_file_read_refused` and names its path
class.

Source acquisition currently serves independent coherence. Bounded-window and
declared-snapshot-group policies refuse before provider invocation. Actual
captures are checked against the pinned replayability and maximum-age rules
before selection, including the rule’s omission/default/refusal behavior.

A completed Source run reports, per occurrence, the `SourceReadReceiptV1` the
daemon minted for the exact bytes it read and the digest of the Capture those
bytes became; `--json` carries both in `source_observations`. A direct Source
run is identified by its evaluation instant, so re-running at the same instant
replays the retained observation rather than reading the source again.

`readiness` names open slots or unsupported nodes before execution. `bind`
proposes a same-identity Procedure successor with exact accepted pins; it never
mutates the accepted Procedure in place. Runs append replay-verifiable journal
records and `status` reconstructs the one-read run state from those records.

### Measurements and readings

A Procedure declares measurements -- an accepted query, a Claim statement's
evidence-relative verdict, or the ClaimAttestations on a statement -- and the
generation that accepts the Procedure revision ACTIVATES them: `activated_at`
is that generation's signed acceptance instant, `check_at` and `expires_at`
are that instant plus the declaration's `check_after` and `expires_after`.
Nothing restarts the window per run or per poll.

`measure` is the due/pending/resume door. It evaluates at an explicit
OBSERVATION instant (`--evaluation-time`, default now) and coordinate (`--at`,
default the current head), which are distinct from the activation coordinate
and from a run's admission coordinate. Before `check_at` a measurement reports
`pending`; at or after `expires_at` with no standing answer it reports
`expired`; neither writes anything. Inside the window the door gathers real
evidence -- the exact accepted QueryDefinition run with the declared
parameters and budgets and its receipt retained, the statement's verdict at
that instant with the observation retained as its own journal record (which
accepted coordinate, which Claim artifact, which verdict inputs) and cited as a
`journal_record` proof, or the verified attestations with a declared stance,
one credit per independent principal -- evaluates the frozen resolution law,
and appends one resolution per activation. Attestation evidence is selected at
the observation instant over the complete attestation history, in this order:
only events that had occurred by the observation instant count; the latest of
those per principal is that principal's standing word; validity and stance are
judged on that word alone. A later word does not erase the word that stood
when observed, an expired standing word contributes no proof and does not
revive the word it superseded, and no attestation at all resolves
`indeterminate`, even against a `max_count` of 0, because the law demands
proof for every verdict. The resolution append is a compare-and-set on the
contract partition's head: two evaluations racing to answer first retain one
resolution, and the loser reports the winner's answer. A refused or truncated query
resolves `indeterminate`; it never establishes complete or satisfied evidence.
The latest non-overturned resolution governs: calling again returns the
STANDING answer rather than re-deriving it (the observation instant on a later
call is reported, not re-evaluated), and only an overturned answer reopens the
contract for a fresh evaluation.

With `--run-id`, the standing resolution is bound to the grain that run really
reached as one contract-grade reading: a unit reading needs a succeeded run, a
node reading a node that fired and succeeded, an arm reading a guard that
selected that arm and a target that succeeded. A run that did not reach the
grain reports `grain_not_occurred` and earns nothing; a run that has not
finalized reports `run_not_final`. Readings are keyed on the activation, the
grain, and the run -- or the Line occurrence, so a second attempt of the same
occurrence replays the first attempt's reading -- and a retry with the same
key returns the same record (`replayed`). A retry is any later request by the
same principal: the request attribution a fresh authenticated call re-mints
(timestamp, operation id, request id) is not part of the reading's meaning,
and the retained record keeps its original attribution. The same key with a
different meaning (another resolution, verdict, or value) refuses
`measurement_reading_conflict`. Two requests crediting the same grain at once
land exactly one reading: lookup and append are one compare-and-set on the
reading partition's head, so the loser replays the winner's record. A crash
between the resolution append and the reading append resumes at the reading.
Execution outcome and measurement verdict stay distinct: a completed run does
not satisfy a measurement, and a failed run does not contradict one.

`readings` is read-only: it reports each activation's standing (pending,
expired, or resolved with its resolution id, verdict, value, and retrievable
journal record) and pages through retained readings, at most 200 per page. A
page's `--cursor` continues that page's selection: the observation instant and
coordinate the first page was answered at travel inside the cursor, so a later
page with a fresh clock (every SDK call stamps one) pages the same selection;
changing the run, measurement, or grain filter refuses the cursor. Every
reading a page serves, and every reading a retry replays, is re-read through
its content address, so a warm daemon refuses a missing or corrupt body
exactly as a cold one does. Resolutions and readings are operational exhaust
in the Procedure journal. They are not accepted state and grant no authority.

A complete loop, from a run through a delayed evaluation to inspection and a
retry:

~~~text
$ cruxible playbill procedure run release-guard input.json --evaluation-time 2026-09-06T10:00:00Z
RUN-3f…: succeeded
$ cruxible playbill procedure measure release-guard --run-id RUN-3f…
rollout-healthy: pending reading=no_resolution
  the measurement window has not opened
$ cruxible playbill procedure measure release-guard --run-id RUN-3f… \
    --evaluation-time 2026-09-06T11:00:00Z
rollout-healthy: resolved (satisfied, RSR-9a…) reading=recorded PRD-c1…
$ cruxible playbill procedure measure release-guard --run-id RUN-3f… \
    --evaluation-time 2026-09-06T11:05:00Z
rollout-healthy: resolved (satisfied, RSR-9a…) reading=replayed PRD-c1…
$ cruxible playbill procedure readings release-guard --run-id RUN-3f…
rollout-healthy: resolved readings=1 (satisfied, RSR-9a…)
PRD-c1… rollout-healthy procedure_unit satisfied run=RUN-3f…
~~~

## playbill line

~~~text
cruxible playbill line check LINE [--since TS] [--until TS] [--limit 100] [--cursor CURSOR] [--json]
cruxible playbill line listen LINE [--stop] [--json]
cruxible playbill line evaluate LINE --since TS --until TS [--limit 100] [--cursor CURSOR] [--json]
cruxible playbill line dispatch LINE [--occurrence-id DIGEST] [--retry] [--limit 1] [--json]
cruxible playbill line run LINE --evaluation-time TS
  [--occurrence-id ID] [--json]
~~~

`check` is read-only: it returns `met`, `not_met`, or `incomplete`, exact
matching events/windows, and the dispatch status of each occurrence (pending,
admitted, rejected, or superseded). `listen` enables matching into durable pending work; it never runs a
Procedure. Idle coverage is checkpointed at one-minute intervals; event progress
and partial scans are retained immediately. `listen --stop` ends coverage at the
last durable checkpoint.
`evaluate` explicitly checks a historical `[since, until)` range and records
its matches as pending. Follow its cursor to finish a bounded page.
`dispatch` admits pending occurrences using the caller's current permissions
and the ordinary Line admission checks. Permanent input failures close as
`rejected`; changed Line bindings close as `superseded`. Both leave the runnable
queue, retaining their evidence and a typed refusal with repair instructions.
Invalid event bindings, unavailable event material, and Captures that exceed
their fixed read budget close as rejected. Budget refusals name the limiting
Line or CaptureContract cap. Accepting a successor Line can raise its own limit;
it cannot override the exact CaptureContract's cap. Transient authority/provider
failures and events whose recorded time has not arrived remain blocked.
Historical evaluation does not reopen closed work.

`dispatch --occurrence-id DIGEST --retry` explicitly retries one occurrence,
binding the current accepted Line version only within the same occurrence epoch.
It preserves the exact event/window and rechecks present authority and freshness;
it cannot substitute a newer Capture. An existing admission is always reused.

Restart resumes pending work and opens a new forward listening range. Time
not covered by completed listening ranges requires explicit `evaluate`; it is
never replayed automatically. Rebuilding the disposable event index similarly
opens a new forward range, while retained pending work survives. A changed
occurrence epoch needs an explicit new subscription. Rebinding within the same
epoch preserves listening progress; pending work bound to an older Line version
is closed as superseded rather than silently rebound. A Line v4 or v5 can bind its
trigger Capture to a named Source input. Its `max_age` is checked at admission
time, not backdated to when the trigger occurred.

`run` triggers one daemon-derived due occurrence. The occurrence's evaluation
instant is the daemon's; `--evaluation-time` only asserts the instant the
caller believes it is running at, and an assertion outside the daemon's skew
bound is refused. That bound is operational, not wire: the daemon reads
`evaluation_instant_skew_seconds` from `daemon/procedure-runs.json` in its own
state root, defaulting to the 300-second ProcedureMandate skew the bound
protects, and refuses the run if that file exists but cannot be read as one.
The accepted Line's governed mandate authorizes execution; without one the
operation returns a typed no-mandate refusal.

A Line whose Procedure ends in a `propose_change_set` terminal produces a
proposal. Each resolved candidate template must be one Claim proposal item --
a statement, a rationale, and optionally the Claim lineage it revises. The
daemon supplies the evidence: the produced Capture in that item's own
dependency closure is cited, so a computed interpretation is a Claim under its
ClaimType's evidence admission policy, never an attested observation and never
a self-asserted one. The items are lowered through the same change-set
authoring every surface uses, the exact live ProcedureMandate is evaluated
against the paths lowering actually changed, and the proposal door is called
once. The run reports, per terminal, the proposal id, the exact candidate
digest, the operation key, the mandate bound, and the Claim path each item
lowered into; `--json` carries them in `terminal_egress`. Producing the
proposal activates nothing: retrieve it with `playbill proposal show`, review
it in the ledger, and activate it with the existing proposal verbs.

The proposal ref is keyed on the admitted operation. A retry of the same
operation recovers the same proposal and publishes the same receipt; other
member bytes under the same key refuse `effectful_operation_payload_mismatch`.
A run that dies between preparing its egress and receiving the door's receipt
is resolved at daemon startup through the same door and finalized as
`terminal_egress_recovered` with the receipt on its run state. An item that is
not a Claim proposal item refuses `proposal_item_invalid`; one whose closure
reached no Capture, or more than one, refuses `proposal_item_evidence_missing`
or `proposal_item_evidence_ambiguous`; a ClaimType that does not admit the
Capture refuses `proposal_lowering_refused` naming the lowering diagnostic; a
mandate that does not cover the request refuses `procedure_mandate_*` naming
the failed law. None of these creates a proposal ref.

A Line whose Procedure ends in a `settle_change_set` terminal lands the change
without candidate approvals when, and only when, exactly one live settle
ProcedureMandate for that Procedure covers every Claim the change touches: its
namespace, its ClaimType scope and change kinds (`create`, `revise`,
`retire`), and its Subject scope. No covering mandate refuses
`settle_mandate_missing`; more than one refuses `settle_mandate_ambiguous`.
The mandate's pinned condition query is then evaluated at the accepted parent
for each changed Claim's target Subject; it must return exactly that Subject,
with every required field present, unconflicted and untruncated. A false or
incomplete condition follows the mandate's declared fallback: `propose`
produces an ordinary proposal, reported with `settle_outcome: proposed` and a
`fallback_reason`, while `refuse` refuses `settle_condition_refused` and
creates no proposal ref. A holding condition publishes the change, reported
with `settle_outcome: settled` and the `accepted_git_oid` it produced. The
accepted record names the mandate digest, and replay re-derives the same
authority from the parent state alone; the change carries no approvals.

The settle mandate is the authority: any caller permitted to run the Line
triggers the settlement, whatever its own tier, and no caller settles without
one. A caller's tier can raise the run's reported authority above what its
mandate grants, but the terminal still settles only under a covering settle
mandate. A mandate that expired or was suspended before publication
refuses `settle_publication_refused`, as does a delegated candidate that no
longer reproduces under its mandate at publication.

Each terminal is reported with the authority it needs (`required_authority`:
`observe`, `propose` or `settle`) and the authority the run held
(`effective_authority`, or `none`). A terminal the run's authority does not
reach is reported `refused_effective_authority` with the `limiting_term` that
capped it -- the Procedure's own terminals, the Line's `max_authority`,
propagated sensitivity, the mandate grant, or calibration -- and the run
refuses `terminal_authority_capped_by_<term>`.

Known limitation: a settle run submits its delegated proposal against the
accepted head and then activates it. If another generation is accepted between
the two, activation refuses `settle_publication_refused`, and that proposal
stays open but can never be activated, because its candidate no longer
reproduces against the new head. Retrying the same operation recovers the
same proposal and refuses the same way. Withdraw it with `playbill proposal
withdraw`; the Line's next due occurrence settles against the current head
under a new operation key.

## playbill predictions

~~~text
cruxible playbill resolution-contracts REQUEST_FILE [--json]
cruxible playbill predict REQUEST_FILE [--json]
cruxible playbill settle PREDICTION_ID REQUEST_FILE [--json]
~~~

`predict` submits a governed ResolutionContract for an already accepted, exact
hypothesis Claim version and returns the proposal ID and authoring intent. The
contract must be accepted before it can bind an investigation or settlement.
`resolution-contracts` finds accepted contracts for an exact hypothesis version.

`settle` names that contract by ID and exact accepted reference. It checks later
accepted observation evidence against the contract's selector, mechanical rule,
and bound window. Terminal-backed settlement additionally requires one delivered
`settle_change_set` receipt from the same investigation whose outcome is
`settled`; one that fell back to a proposal does not qualify. It records
the activation and resolution in operational exhaust; it does not create or
mutate Claims. A failed attempt or an unevaluable
observation does not settle the hypothesis as false. Effectful terminal nodes
remain disabled in the public Procedure runner.

## playbill block

~~~text
cruxible playbill block repin SOURCE_ID BLOCK_ID [--claim ID]... [--query ID]...
  [--backing SHA256] [--params CANONICAL_JSON]... [--workspace-root DIR]
  [--evaluation-time TS] [--artifact ID]... [--currency-policy warn|require_current]
  [--clear-claims] [--clear-queries] [--clear-artifacts]
cruxible playbill block sync [PATH]... [--all] [--check]
  [--detach PATH]... [--workspace-root DIR]
cruxible playbill block depublish SOURCE_ID BLOCK_ID [--json]
~~~

### The two roads a governed passage takes

A page is a **source**. Its bytes are captured, its capture is evidence, and a
passage of it can be cited like any other evidence. What a page holds is one of
exactly two kinds of governed block, and they never overlap.

A **source block** is ordinary prose the author wrote, made governed by a Claim
that cites its span. There is no marker: write the passage, capture the page
through `playbill sources compile` / `propose`, and author the Claim citing the
span it states -- `copied_from` when the passage states the value verbatim,
`supported_by` when it rests on the passage as evidence. This is the road for
"the page says this, and here is the Claim that stands behind it".

A **projection block** is agent-authored prose HELD TO an explicit list of
accepted Claims and artifacts, marked in the file by a marker pair and declared
with `block repin`. **Nothing renders it.** The body is git-tracked text the
agent writes; what the marker commits to is which accepted state the passage is
accountable for, and `playbill next` and `block sync` prove that state has not
moved under it. This is the road for "this table reflects these Claims".

Evidence never comes from a projection window. A citation of any role and any
origin whose span lies inside a stamped block refuses --
`playbill.projection.evidence_from_projection`, at the daemon, not only in the
SDK -- because a block that was both kinds would let a page attest itself into
concrete. Prose outside every window is the author's own and stays citable.

### Declaring a projection block

`block repin --claim ID --claim ID ...` is how a projection block is created.
Write the marker pair by hand around the prose you want governed, then repin it
naming every backing: the daemon re-reads and re-proves each Claim at the
accepted coordinate, stamps the marker, and registers the block with the
instance. Up to 512 backings fit in one block
(`MAX_PROJECTION_BACKINGS_PER_BLOCK`, inside a 128 KiB stamp), and a block that
would need more refuses rather than truncating. **`repin` mints no Claim.** It
declares that this passage reflects Claims that already exist, which is exactly
what a projection block is.

A block binds explicit Claims, Subject/ClaimType artifacts, and optionally one
query. All are dependencies: Claim statement changes, retirement or overturn;
artifact digest changes; and query definition or semantic result changes require
review. A query keeps selecting current membership, including additions and
removals. It is not converted to a static Claim list. Missing, refused, or
truncated query results are reported as unchecked, never current.

`--currency-policy warn` (the default) makes drift advisory. `require_current`
makes drift or an incomplete dependency check fail workspace checks and produces
a blocking `playbill next` finding. Invalid markers and integrity failures always
fail. A zero exit means the configured policy passed, not that every block is
current: advisory stale, dirty, retired-backing, and unchecked findings remain
in the result for the author to review. Repin follows that review, whether the
prose was revised or reaffirmed. This policy never blocks acceptance of underlying
state. Cruxible does not serve Markdown or HTML, and reading or exporting a local
package is not a freshness check.

Repin preserves omitted categories and policy. Supplying `--claim`, `--query`, or
`--artifact` replaces only that category; `--clear-claims`, `--clear-queries`, and
`--clear-artifacts` remove it explicitly. At least one dependency must remain.
SDK callers use `None` for omission and an empty sequence for removal. Repin is
the author's declaration after reviewing the prose, not proof that arbitrary
prose follows logically from its backings. Ordinary repin preserves the body;
the SDK can install explicitly supplied reviewed body bytes.

There is no SDK option that publishes a Claim as its own page text. `publish_to`
was that road and it is gone: it minted a block whose one backing was the
publishing Claim itself, which is a source block projected as its own
projection -- the overlap the two-block-kinds law refuses. An intent carrying
`insertion_target` refuses typed as
`playbill.authoring.insertion_target_removed`, naming both roads above as the
repair.

### Checking and detaching

`block sync` and `playbill next` use the same currency evaluator. A sync checks
all blocks at one accepted revision and evaluation time, sharing query facts and
lineage reads. Claim-backed checks currently build accepted query facts once
per invocation; each distinct query is evaluated once. Artifact-definition
queries use their indexed reader without building Claim facts. Lineage reads
still consult accepted history, with batched and cached source reads. The batch
request currently supports at most 4096 stamps, without client chunking.
It reports `unchanged`, `stale`, `dirty`, or an incomplete/refused
check with diagnostic details. A dirty body does not suppress dependency checks,
and one failed dependency does not hide the others. Repin acknowledges a reviewed
body and refreshes its dependencies; there is no separate accept-local bypass.

`--check` suppresses explicit detach edits. Advisory findings remain visible
without a nonzero exit; `require_current` findings and integrity errors fail the
check. An unreadable or ambiguous lineage remains an incomplete check, with
exact successor candidates where available. `repin --backing DIGEST` selects a
live successor explicitly.

`--detach PATH` removes markers from a retired block or a declaration belonging
to a different instance, preserving its prose and all bytes outside the block.
It uses a whole-file compare-and-swap. It does not rewrite or approve prose.

### Depublishing

`block depublish SOURCE_ID BLOCK_ID` releases the registration that demands a
block's frame, whichever road declared it. Every projection block is registered
with the instance -- a `block repin` records a declaration, and an instance that
published under the retired road folds its bound publications -- and `playbill
next` reports a registered block whose marker is no longer in the file as
blocking, correctly, until the block is meant to be gone. Depublishing is what
says so, and the blocking row names this verb.

The registration is protocol state and says nothing about what a block CONTAINS:
it records that this instance stands behind this marker. It is also the identity
`workspace detach` refuses on, so a worktree cannot move out from under markers
a host still owns.

It edits no page and retires no Claim. Strip the markers (`block sync --detach`,
or by hand), retire the backing Claim through the ordinary retirement road if
the statement is also being withdrawn, and depublish when the block itself is
not coming back. A registration whose backing Claim is already retired no longer
demands its frame, so a retirement alone clears the row without this verb.

**Depublishing is one of two steps: the marker still has to leave the page.**
The verb touches the registration and nothing in the workspace. There is no
`--strip` -- removing bytes from a file is the operator's explicit act, not a
side effect of a ledger release -- so between the two steps `playbill next`
reports the marker as `unregistered_projection_block` with the repair
`remove_or_register_projection_block`. That is a warning rather than a blocking
row, and it is the opposite instruction to the row it replaces, which asked for
the frame to be restored. Remove the marker pair with `block sync --detach PATH`
or by hand and it clears.

## playbill next

~~~text
cruxible playbill next [--evaluation-time TS] [--access-profile FILE]
  [--expiring-within P7D] [--workspace-root DIR]
~~~

Returns the deterministic repair queue at one accepted coordinate. The client
stamps the current UTC evaluation time when `--evaluation-time` is omitted,
parses `--expiring-within` ISO-8601 durations client-side without changing the
integer-microsecond daemon wire, and observes its configured floor locally. If
`.playbill/sources.yaml` or root-level `sources.yaml` exists, the client also
observes readable source-file digests, including paths explicitly authorized by
a local overlay. Unreadable or unresolved sources are omitted individually; the
daemon compares observed sources with accepted whole-source snapshots and names
drifted or unobserved cited sources. The daemon reads no clock or workspace.
Without actual source or drift observations, `workspace_sources` remains explicitly
unobserved. Procedure-catalog coverage is accounted for separately as
`workspace_projections` and cannot imply that workspace sources were scanned.
An entry with `kind: procedure`, a `Procedure` identity, and a workspace-relative
`locator` declares projection intent for that accepted Procedure. A complete,
coordinate-bound catalog observation produces one nonblocking warning listing all
live Procedures without such entries; the repair carries their exact hand-edit
entry shapes until a projection-authoring command exists.
Empty output means only that no work exists in the explicitly observed domains.
Conflicting values in the same claim slot require revisions into distinct
qualifiers; when a shared value field such as `topic` separates the contenders,
the repair identifies that field.

## playbill curation

~~~text
cruxible playbill curation list [--workspace-root PATH] [--json]
cruxible playbill curation overrule ITEM_ID
  --expected-latest-event-digest DIGEST --reason TEXT [--json]
cruxible playbill curation accept-fixed ITEM_ID
  --expected-latest-event-digest DIGEST --reason TEXT
  --proposal-id DIGEST --changeset-digest DIGEST [--json]
cruxible playbill curation suppress ITEM_ID
  --expected-latest-event-digest DIGEST --reason TEXT
  --scope item|pattern|instance [--until-generation N] [--json]
~~~

Lists the mechanical curation queue and explicitly submits the declared-block
observation produced by the client-side workspace scanner. The daemon does not
read workspace files. The lifecycle commands append attributed operational
events; they do not create governed proposals or mutate accepted knowledge.

## playbill audit

~~~text
cruxible playbill audit [--claim-type ID]... [--subject-kind KIND]...
  [--max-rows N] [--max-bytes N] [--access-profile FILE]
  [--cursor FILE] [--json]
~~~

Returns a deterministic Claim verification patrol ranked by the exact integer
product of stake, weakness, and verification recency. Every row includes all
factor values and mechanical evidence references; it never includes a repair
recommendation and never executes a Procedure. A successful read appends one
idempotent completed-run record to the daemon-local operational store so
`audited_through_generation` means completed coverage rather than silence.
Audit reads do not create qualifying consumption touches or change governed
state. Follow `next_cursor` only while its accepted coordinate, evaluation time,
scope, and operational input head remain unchanged.

## playbill discover

~~~text
cruxible playbill discover [--query TEXT] [--entrypoint NAME]
  [--profile interfaces|subjects|all]
  [--evaluation-time TS]
~~~

Exactly one of --query or --entrypoint selects the page. Matching is exact and
lexical over the accepted naming layer; it is never a similarity score.

## playbill search, list, and orient

~~~text
cruxible playbill search QUERY [--kind KIND]... [--status STATUS]...
  [--subject-path PATH] [--cursor JSON] [--evaluation-time TS]
cruxible playbill list [--kind KIND]... [--status STATUS]...
  [--subject-path PATH] [--cursor JSON] [--evaluation-time TS]
cruxible playbill orient [--kind KIND]... [--status STATUS]...
  [--subject-path PATH] [--evaluation-time TS]
~~~

These are the generic headless discovery surface for Claims, Procedures, and
installed demand policies. `orient` returns counts and exact follow-up filters,
never arbitrary top rows. Until demand policy is installed it explicitly reports
`demand: not_installed`.

## playbill world

~~~text
cruxible playbill world stub [--out PATH]
~~~

Writes a `.pyi` typing this instance's accepted world -- its Subject kinds, the
Subject IDs that can be spelled as Python attributes, every predicate, and each
enum member a literal schema names -- so an editor and a model complete the real
vocabulary instead of `Any`. Without `--out` the stub goes to standard output.

The stub names in a header comment the exact accepted coordinate it was read at,
and is byte-identical for that coordinate, so regenerating after an activation
shows the vocabulary movement as an ordinary diff. It types one coordinate and
carries no authority over the next: the SDK still refuses a world whose
orientation has moved.

## playbill since

~~~text
cruxible playbill since GENERATION [--max-rows N] [--max-bytes N]
  [--access-profile FILE] [--cursor FILE] [--json]
~~~

Returns signed accepted ChangeSet members in `(GENERATION, pinned head]` order.
Follow `next_cursor` to continue against the same historical head even if main
advances; the cursor binds the lower bound, access profile, and page budgets.

## playbill expand

~~~text
cruxible playbill expand ARTIFACT_PATH [--facet NAME]... [--evaluation-time TS]
~~~

Returns one bounded context capsule for an accepted address. Repeat --facet to
narrow what the capsule carries.

## playbill floor

~~~text
cruxible playbill floor export [--force]
~~~

Writes the deterministic greppable floor of accepted state to the fixed derived
cache `.playbill/floor/` under the current workspace. The daemon returns bytes
keyed by floor path and never writes a client path; export refuses a non-empty
floor unless `--force` is given. The export carries its own coverage boundary
in `coverage-manifest.json`, enumerated in the root manifest like every other
floor file. `floor_output.path` is obsolete and refused; a v2 coverage config
enables refresh with only the fixed profile. `floor export` records that profile
when the config lacks it, so the following `next` observation no longer reports
`floor_missing` after a successful export:

~~~json
{
  "tag": "playbill-coverage-workspace-config-v2",
  "floor_output": {
    "tag": "playbill-floor-output-v1",
    "format": "playbill-floor-export-v2"
  }
}
~~~

Floor export v2 pretty-prints every JSON card with stable key ordering for grep
quality. `manifest.json` inventories and digests those exact rendered bytes, so
repeated exports at one accepted coordinate remain byte-identical. Historical v1
manifests and compact JSON spelling remain readable without reinterpretation.

## playbill coverage

~~~text
cruxible playbill coverage resolve
  [--bind PATH=PLANE:IDENTITY]... [--bindings FILE] [--root DIR]
  [--file PATH]... [--range PATH:START-END]...
  [--grep-results FILE] [--all]
cruxible playbill coverage status
  [--bind PATH=PLANE:IDENTITY]... [--bindings FILE] [--root DIR]
~~~

resolve answers what the working files you just read or changed have to do with
accepted state. Every working path is bound to a logical source by an explicit
declaration -- coverage never infers a binding from a filename, because
identical bytes in another file are precisely not the same source. The CLI reads
and hashes the bytes locally; the daemon reads no client filesystem.

Governed spans are annotated inline in card order. Ungoverned results are
summarized once per operation, never one line per result:

~~~text
Playbill coverage: 2 exact, 1 drifted, 3 candidates, 41 none
coverage complete for 47 returned spans at generation gen-sha256:...
omitted cards: 0, truncated spans: 0
~~~

A `none` is factual only inside a complete boundary, so a span whose health is
`partial`, `stale`, `denied`, or `unavailable` prints that health and its reason
codes rather than reading as an absence.

status renders the coverage manifest over the whole declared scope: epoch,
health, completeness, and the sources a `none` would have been factual inside.

Resolving coverage changes no accepted state and appends no receipt.

## playbill hook

~~~text
cruxible playbill hook post-tool-use [--root DIR]
~~~

Reads one Claude Code PostToolUse payload on stdin and writes the hook response
on stdout, binding working paths through `.playbill/coverage.json` at the
workspace root. Wire it with the settings fragment in
`integrations/claude-code/`.

This vendor-specific hook is deprecated and parked: it remains compatible, but
new harnesses should use the client coverage middleware rather than extend it.

Grep content-mode results are annotated in place: the cards are appended to the
result's own text and every other field is passed through unchanged. Read, Edit,
and Write are observed only -- their paths are resolved, which refreshes the
local freshness manifest so the next Grep answers against a current snapshot --
and their output is returned unmodified, because those tools' result shapes
carry no field that can hold an annotation without fabricating file content.
`additionalContext` is never used: it would arrive as a system reminder, which
is the instruction channel rather than the data channel.

The command always exits 0 and always emits one JSON object. A coverage failure
degrades to the original output plus, where a channel exists, one
`Playbill coverage: unavailable` line; it never breaks the agent's tool call.
The parked hook writes one actionable code to stderr only when its own adapter
input is malformed:

- `playbill.coverage_hook.instance_id_missing`: add `instance_id` to
  `.playbill/coverage.json`, or select one with the CLI context/environment.
- `playbill.coverage_hook.rule_tag_invalid`: use the exact-path or path-prefix
  rule tags shown in the integration README.
- `playbill.coverage_hook.tool_response_invalid`: the Grep hook must receive its
  structured response object; fix the harness envelope rather than parsing text.

The workspace config's `instance_id` is the hook's selected instance. General
CLI and SDK target selection also reads `server_url` or `server_socket` from an
attached workspace after explicit flags and environment and before remembered
global context.

For a harness that owns its tool executor, the vendor-neutral middleware in
`cruxible_core.coverage.middleware` is the full-fidelity path and
covers all four tool kinds, including same-turn edit drift.

## playbill proposal

~~~text
cruxible playbill proposal inspect PROPOSAL_ID
cruxible playbill proposal list [--status open|settled]
cruxible playbill proposal readmit PROPOSAL_ID
cruxible playbill proposal withdraw PROPOSAL_ID --reason TEXT
cruxible playbill proposal refusal PROPOSAL_ID
cruxible playbill proposal review PROPOSAL_ID [--include-body|--redacted]
  [--workspace-root DIR]
cruxible playbill proposal approve PROPOSAL_ID
  --signer-id ID --key FILE [--yes]
cruxible playbill proposal activate PROPOSAL_ID [--workspace-root DIR]
  [--no-sync]
cruxible playbill review open PROPOSAL_ID [--workspace-root DIR]    # deprecated
cruxible playbill review close PROPOSAL_ID [--workspace-root DIR]   # deprecated
~~~

`cruxible playbill whoami` names the credential-derived actor, its effective
permission mode, accepted principal-registration status, and current coordinate.
`proposal list` prints a labeled `COORDINATE_TIME` column and deterministically
separates current open candidates from accepted, refused, and stale terminal
evidence so retries do not depend on remembered IDs. Proposal actions accept a
full digest, a unique digest prefix, or a target ref whose current Git target
names exactly one admission; unknown and historical ambiguous selectors are
typed refusals that point back to `proposal list`.
`proposal readmit` replays a stale proposal's authored content through the current
governed rebase and returns a fresh, idempotent proposal without changing the old
proposal evidence. A stale generated ClaimType dependency-closure migration is not
byte-rebased because its dependent inventory may have changed; rerun ClaimType
migration preflight and submit at the current head instead.

`proposal withdraw` is the terminal statement for the opposite case: an open (or
stale) proposal that will never be activated, because a hard limit refuses its
activation or its author changed their mind. It writes one immutable withdrawal
record beside the admission, touches no accepted state, leaves every byte of the
candidate readable, and moves the proposal out of `proposal list --status open`
with terminal reason `withdrawn`. Only the actor who submitted a proposal may
withdraw it; withdrawing an already-withdrawn proposal repeats the first answer
rather than rewriting its reason, and a settled proposal refuses, because its
outcome is not an intention to overwrite.

approve signs locally. The private-key path is not sent to the daemon.
When `.playbill/coverage.json` at `--workspace-root` declares `floor_output`,
activate refreshes floor-v2 as a verified exact directory replacement. An
accepted activation followed by a failed local refresh reports both truths and
exits nonzero; the daemon never receives the workspace path.

After an accepted activation, the client runs block sync last unless
`--no-sync` is explicit. An unattached workspace retains a typed `skipped`
`workspace_not_attached` row and exits zero; a sync refusal in an attached
workspace reports the already-accepted truth, names `cruxible playbill block sync
--all`, and exits nonzero.

### Reviewing a proposal

The ledger is Git, so review is Git. The daemon fetches its own refs into the
attached workspace on every proposal, so a reviewer compares the candidate
against accepted state with standard tooling and no bespoke rendering:

~~~text
git diff playbill/accepted...playbill/proposals/<proposal-id>
~~~

The daemon's records are notes on that same projected commit, fetched into the
workspace under their own names (`git notes --ref=` prefixes anything that is
not already under `refs/notes/`, so a note parked elsewhere reads back as "no
note found"). From a clone of the ledger mirror, fetch them once:

~~~text
git fetch origin '+refs/notes/*:refs/notes/*'
git notes --ref=refs/notes/playbill-eval show origin/proposals/<proposal-id>
~~~

The candidate commit carries the change set's own summary as its message: a
subject naming what the set does, then one line per member as `<disposition>
<kind> <address> [qualifier]`. It is prose for a reader; nothing parses it. The
daemon's records travel beside it as Git notes on that commit:
`refs/notes/playbill-eval` holds the admission and the evaluation verdict with
every diagnostic behind a refusal, and `refs/notes/playbill-approval` holds the
canonical approval list, each entry carrying its signer's own attestation. Both
are byte-identical projections of the proposal evidence store, which stays the
source of record: activation refuses to settle a candidate whose note disagrees
with it.

A commit shared by several admissions carries one admission/evaluation pair per
proposal, ordered by proposal ID. The approval list retains signatures for each
distinct candidate digest. Match both IDs when inspecting the group. Activation
checks original and materialized advisory aliases; recovery restores valid
incomplete projections without treating modified records as accepted evidence.

`proposal review` prints that pointer and those ref names; `--json` remains the
structured read. `proposal approve` still renders the whole candidate before
asking for a signature, because that rendering is what the signature covers.

A reviewer without a workspace attachment clones the ledger mirror instead and
runs the same diff against `origin/main`; see [playbill
ledger](#playbill-ledger) for what the mirror carries and how to get its URL.

`playbill review open` and `playbill review close`, which materialized a
detached, gitignored worktree at `.playbill/review/<proposal-digest>/`, are
DEPRECATED and are removed in 0.6.0; both emit the structured deprecation
warning naming the diff above. They still work for the deprecation window. A
`review_workspace_not_attached` refusal from `review open` names the
local-socket `playbill host create --workspace` command needed when creating a
host that supports review worktrees.

## playbill principal

~~~text
cruxible playbill principal list
cruxible playbill principal add PRINCIPAL_ID --kind ordinary --key-dir DIR --name NAME
cruxible playbill principal rotate ...
cruxible playbill principal revoke ...
cruxible playbill principal recover ...
~~~

Registration, rotation, revocation, and recovery are governed principal-change
proposals. `principal add` generates the Ed25519 private key exclusively in the
client-held `--key-dir` outside the current workspace and sends only its public
principal record. Every principal-lifecycle proposal requires the PROPOSING
actor's own cryptographic approval before it can settle — the identity shown
by `playbill whoami`, which coincides with the affected principal only for
self-rotation: run `playbill proposal approve PID --signer-id <the proposing
actor> --key <that actor's current private key> --yes`, then
`playbill proposal activate`. For `principal add` and `principal recover`,
the signer is the actor performing the operation, never the new or
locked-out principal. Registration neither
grants authority immediately nor sends a private key to the daemon. Other
non-creator principals may record additional voluntary approvals. `--kind`
is explicit and may be `ordinary` or `recovery`; the daemon kind is
instance-owned. Recovery principals cannot approve ordinary Document candidates.

## playbill sources

~~~text
cruxible playbill sources check ...
cruxible playbill sources compile ...
cruxible playbill sources propose ...
~~~

Compilation reads declared local files client-side and emits a path-free bundle.
The daemon never reads a submitted client path.

## playbill explain

~~~text
cruxible playbill explain IDENTITY
  [--detail summary|evidence|proof]
  [--include-body]
~~~

IDENTITY is one accepted Document identity (`document:fleet.policy-note`) or one
Subject address (`sec.package/click`, or the `Subject:`-prefixed spelling), which
resolves to that Subject rather than refusing.

summary and evidence are implemented. proof is reserved and returns a typed
unsupported-detail result.

Use --json on operation commands for machine-readable output. Run any command
with --help for its exact options.

### Compiler upgrade

`cruxible playbill compiler upgrade --to DIGEST --name NAME` creates an admin-only
proposal bound to the exact accepted head and target compiler. Review it, sign
through `cruxible playbill proposal approve`, then use
`cruxible playbill proposal activate`. Activation validates the full target
projection before advancing the signed ledger. Installing or restarting a daemon
does not upgrade an instance. Historical generations keep their original compiler.
The instance descriptor retains its genesis compiler; inspection reports the
active compiler from accepted history. Unsupported transitions and downgrades are
refused. Artifact format migrations are separate work.
