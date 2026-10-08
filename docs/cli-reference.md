# CLI reference

The public CLI has four top-level command groups.

## Global options

~~~text
--server-url TEXT
--server-socket TEXT
--instance-id TEXT
--principal-id TEXT
--no-workspace
--json-compact
--version
~~~

Target resolution is component-wise and deterministic: explicit flags, then
`CRUXIBLE_SERVER_URL` / `CRUXIBLE_SERVER_SOCKET` / `CRUXIBLE_INSTANCE_ID`, then
the attached workspace discovered from `CRUXIBLE_WORKSPACE` or by
walking up from the current directory to `.cruxible/coverage.json`, then the
remembered global context. Automatic walk-up stops after the home directory and
never crosses a filesystem boundary. `--no-workspace` or
`CRUXIBLE_NO_WORKSPACE=1` disables workspace discovery for recovery from a bad
ancestor binding. A workspace is attached when that file names an instance and
exactly one of `server_url` or `server_socket`; its root must agree with the root
of `.cruxible/sources.yaml` when both exist. The global context is only a
fallback, its remembered instance remains bound to the transport on which it
was selected, and entering one workspace never retargets another.

`--principal-id` (or `CRUXIBLE_PRINCIPAL_ID`) names the principal this process
acts as; the CLI, SDK (`Cruxible.connect(principal_id=...)`) and MCP server all
send it with every request. The daemon checks that it names a registered, active
principal on the instance before any write and attributes the work to it; an
unregistered or revoked ID is refused on writes (`cruxible.identity.principal_absent`
/ `principal_revoked`) with the command that repairs it. Reads stay open, so an
agent can read (and `whoami` explains its standing) while its registration
awaits activation. With daemon auth off the principal ID is a
claim of identity, not authentication: every process of the same OS user is
equally trusted and could claim any principal. With auth on the bearer
credential decides who acts, and a principal ID that disagrees with it is refused
(`cruxible.identity.principal_claim_mismatch`). Approvals are unaffected either
way: they are signed with the principal's private key.

`CRUXIBLE_CLIENT_TIMEOUT_S` (default 180) bounds how long a client waits for a
daemon that has accepted a request. An SDK `Cruxible.connect()` reads only the
current head when it opens a session; it does not orient over the whole
accepted world, so no separate connect budget is needed. The variable is
refused, typed, unless it names a positive number of seconds. A timeout never
means the request failed: the daemon may still be running it, so verify state
before retrying.

Every argument that takes a one-off payload also accepts `-` to read it from
stdin, so a heredoc or a pipe works and nothing lands on disk: `write -`,
`authoring compile -`, `authoring bind --payload-file -`, `claim-type propose
--input -`, `claim-type migrate -`, `predict -`, `settle --request -`,
`resolution-contracts --request -`, `query --spec -`, `coverage resolve
--grep-results -`, the Procedure and Line request, input, `--at`,
`--resolution-contract` and `--trigger-event` files, access profiles and
cursors, and `body store -`. One command reads stdin for one argument; a second
`-` is refused. Real artifacts stay files: Procedure source, signed source
bundles, kit and provider lock files, key directories, cited workspace files
and block pages.

## context

Manage remembered daemon and instance context. `context show` reports the
resolved target, workspace, and the source selected for each target component.
It reports workspace-config attachment separately from daemon host registration;
for local sockets, a mismatch is a typed attachment-disagreement row rather than
silently treating those two notions as equivalent. When a remembered instance
is bound to a different transport than the one resolved, `context show` names
it under `remembered_instance_ignored`. `context connect` stores the socket's
realpath:

~~~text
cruxible context connect
cruxible context use [INSTANCE_ID] [--principal ID]
cruxible context show
cruxible context clear
~~~

The context also remembers which principal's settings the CLI uses on each
instance. `cruxible init` remembers the owner's `cruxible.env` and makes it
active; `cruxible principal add` remembers the new principal's file without
switching to it. The CLI loads the active principal's settings itself (its
principal ID, key and, with daemon auth, its bearer credential), so no shell
sourcing is needed; `context use --principal ID` switches, and `context show`
names the principal and where it came from. A process that sets its own
principal (`--principal-id`, `CRUXIBLE_PRINCIPAL_ID`, `CRUXIBLE_PRINCIPAL_KEY`
or `CRUXIBLE_SERVER_BEARER_TOKEN`) keeps it, so an agent launched with its own
`cruxible.env` works unchanged. Settings are remembered for one instance on one
daemon endpoint (URL or socket), and the file must name that endpoint: when a
command targets another daemon, by flag, environment or workspace config, the
CLI loads nothing and acts as no principal, so a credential never reaches a
daemon it was not issued by.

## Previews

Every operation that can change governed state, operational state or anything
outside the daemon takes `--dry-run` (MCP and SDK: `dry_run`). A dry run runs
the change's own checks and evaluation up to the commit and writes nothing
anywhere: no proposal, ref, record, body, credential, registry row, journal
entry or workspace file. It answers in the change's own result shape with a
`would_*` status (`would_propose`, `would_block`, `would_decommission`,
`would_revoke`, `would_publish`, ...), pinned to the coordinate it was
evaluated at: the accepted head for a change to governed state, or, for a
change to operational state (a runtime credential, a host's worktree binding,
a page's block markers), a state digest of exactly the records it changes,
which exists before `init` too.

- A change the server derives across several artifacts previews unless asked
  to commit: `kit add`, `kit remove` and `claim-type upgrade`. Commit with
  `--commit`.
- A change that cannot be undone previews unless asked to commit, and commits
  only with the preview's coordinate: `instance decommission`,
  `credential revoke`, `credential rotate` and `ledger set-mirror`. Commit with
  `--commit --at OID`; without `--at` it refuses
  `cruxible.preview.confirmation_required`.
- Everything else commits unless `--dry-run` is given.

`--at OID` pins any commit to a preview: if that state moved since, the
commit refuses `cruxible.preview.state_moved` and changes nothing; preview
again. The pin is checked where the change commits, under the lock its write
holds, so state that moves after the first check is still refused. The write
verbs (`set`, `retire`, `write`) take `--dry-run` and `--at` the same way,
where `--at` pins each changed slot.

A preview opens its instance behind the same guards. If opening it would first
repair derived files a crash left behind, the preview refuses
`cruxible.preview.recovery_pending` instead of writing them; an ordinary read
(`cruxible orient`) reopens it, and the preview then runs.

Which operations preview follows one principle (the maintainer's ruling):
an operation previews when its effect is derived -- computed by the server
from more than its input -- or when it cannot be undone or reaches outside the
daemon and its effect is not already shown to the caller. An operation whose
full effect is determined by its input and that writes nothing when refused is
exempt. By that principle these are exempt:

- `claim attest`: the statement the caller signs is the whole effect, and a
  refused attestation writes nothing;
- `body store`: an inert content-addressed put whose effect is its input;
- `proposal approve`: records the caller's signed approval of an evaluation
  already shown by `get PROPOSAL_ID`;
- `proposal activate`: its preview is the proposal's evaluation, already
  shown; the commit-time `at` check covers the head moving under it;
- `floor delivery on|off`: its effect is its input;
- floor deliver-now: its result is fully determined by the accepted head (the
  floor is a pure function of the accepted coordinate), it is idempotent, and
  it writes only the derived, regenerable `.cruxible/floor`.

Exempt in v1 as well, by the maintainer's earlier scope ruling:

- the exhaust paths -- `prediction settle`, `prediction propose`, `procedure run` and
  `procedure measure`, and `line evaluate`, `line dispatch` and `line run` --
  append observations to the instance's exhaust and need a separate
  dry-run-execution feature;
- client-local writes -- `context connect`, `context use`, `context clear`,
  `kit build`, `kit pull` and `hook` -- change only the caller's own
  configuration or output files;
- `init` (genesis) and `server stop` / `server restart`, which have no
  coordinate to preview against.

`provider install --dry-run` is a labelled v1 exception: it validates and
writes nothing, but it does not prepare the package, check deployment
readiness or (for a package not yet prepared) evaluate its registration. Its
outcome says so: `preview_scope: validation_only` and `not_run` naming those
steps, with the coordinate it evaluated at.

## credential

Credentials and principals are different things. A credential is a daemon
bearer token: it authorizes transport (which endpoints a request may reach, at
which permission tier) and, when the daemon runs with auth, says which principal
the request acts as. A principal is a governed signing key registered in the
instance's ledger (`cruxible init`, `cruxible principal add`): it is who
authors, approves and is attributed, and its approvals are signed with its
private key, never with a token. With auth off there are no credentials to
speak of and the principal ID is a claim of identity; with auth on each
credential is bound to one principal. Revoking a principal revokes its
credentials; rotating a credential never changes the principal.

Manage runtime bearer credentials:

~~~text
cruxible credential claim-bootstrap [--secret-file PATH] [--dry-run] [--json]
cruxible credential mint --principal-id ID --mode TIER [--key-dir DIR] [--label TEXT]
  [--dry-run|--commit] [--at OID] [--json]
cruxible credential list [--json]
cruxible credential rotate CREDENTIAL_ID [--key-dir DIR] [--dry-run|--commit] [--at OID] [--json]
cruxible credential revoke CREDENTIAL_ID [--dry-run|--commit] [--at OID] [--json]
cruxible credential recover-admin [--state-root DIR] [--instance-id ID] [--dry-run] [--json]
~~~

The bootstrap secret is claimable once per host: each host on a daemon claims
its own first ADMIN credential with it, and no host claims twice
(`runtime_bootstrap.secret_already_claimed`). Revoking or rotating a credential
cannot be undone, so both preview first; see [Previews](#previews).

`recover-admin` is local-only: it opens the state root's credentials DB
directly with the daemon stopped. It ignores a remembered CLI context and
refuses only a transport chosen for that invocation (`--server-url`,
`--server-socket` or their env vars). When the DB holds several instances and
exactly one has a directory under `<state-root>/instances`, that instance is the
target; otherwise pass `--instance-id`.

These credentials authorize transport operations. Each one acts as exactly one
Cruxible principal, stored with the credential; the label is a description and
never decides who acts. `credential mint` refuses unless that principal is
registered and active (`cruxible.identity.principal_absent` /
`principal_revoked`) and ordinary (a recovery principal never holds one:
`runtime_credential.principal_not_ordinary`), and it needs the principal's own authority, not just an
admin credential: either the request already acts as that principal, or
`--key-dir` signs the principal's single-use consent with its registered key
(`runtime_credential.principal_authority_required`,
`principal_proof_invalid`, `principal_proof_replayed`). On a daemon with auth
off, where a bearer credential authenticates nothing, minting is refused
(`runtime_credential.auth_off`, repair: `cruxible server start --auth`) and
nothing is stored, so the state root is never silently latched into requiring
auth. Revoking a principal
revokes every credential that acts as it: the next request with one is refused
with `cruxible.identity.principal_revoked` and the rows are marked revoked.
Rotation keeps the principal, tier and label, so rotating a bound credential
needs the same authority minting it would: the request acts as that principal,
or `credential rotate --key-dir DIR` signs its consent. Any admin may revoke a
credential, but never receives a replacement for someone else's principal.

The bootstrap claim and `recover-admin` mint unbound operator credentials: they
carry transport authority (host, init, credentials, daemon lifecycle) but act as
no principal, so they cannot author or perform any other instance write --
body store, ledger mirror binding and publication, attestation recovery
included (`cruxible.identity.credential_unbound`).
`cruxible init` under such a credential designates the owner it names.
Credentials minted before credentials named a principal are migrated as
unbound, never rebound from their label; repair each by minting a bound one with
`cruxible credential mint --principal-id ID --key-dir DIR --mode TIER`, then
revoking the old one.

`credential mint --mode` picks a cumulative tier. `read_only` reads only.
`governed_write` also proposes and authors, but cannot submit approvals or
activate. `graph_write` also submits approvals and activates. `admin` also
performs operator actions: credentials, host and init, principal changes,
compiler upgrades, provider installs, ledger mirrors, and daemon stop/restart. A
permission refusal names the tier it needs and what that tier allows.

## server

~~~text
cruxible server start [--state-root DIR] [--socket PATH | --host HOST --port PORT] [--auth]
cruxible server install-service [SERVER-START FLAGS] [--print] [--replace]
cruxible server status
cruxible server restart
cruxible server stop [--timeout SECONDS] [--json]
~~~

server start is the long-running daemon process and does not connect to an
existing server.

Auth depends on the transport. A Unix-socket daemon defaults to auth off and
prints one line saying so when it starts: every process that can reach its
owner-only socket directory already runs as your OS user, and bearer tokens
would protect nothing from a process that can read the token files anyway. A
TCP daemon, loopback included, refuses to start without auth
(`cruxible.server.tcp_requires_auth`), because any local user or network peer
that can reach the port could otherwise act as any principal. `--auth` is the
explicit opt-in on either transport; `CRUXIBLE_SERVER_AUTH=true` is its
environment form. Once a state root has run with auth it refuses to start
without it (`cruxible.server.auth_latched`).

With auth on, the daemon's runtime bootstrap secret (its unscoped operator
credential) is never printed to stdout, stderr or the request log. Once the
daemon holds the state-root lock it writes the secret owner-only (0600) to
`<state-root>/daemon/bootstrap-secret`, and prints only that path. An in-place
restart keeps the same secret. `server status`, `server restart` and
`server stop` use that file by default when no `CRUXIBLE_SERVER_BEARER_TOKEN` is
set, so a local restart needs no credential typed in. The secret is never sent:
each such request carries a MAC keyed by the secret over its method, path,
body digest, a fresh nonce, a timestamp and the daemon's unpredictable boot id
(read from the live lock record), and the daemon accepts it only if the boot id
is its own current process image's, the MAC verifies under its own secret, the
timestamp is within 60 seconds of its clock, and the nonce is new
(`runtime_bootstrap.operator_mac_boot_changed`, `operator_mac_invalid`,
`operator_mac_stale`, `operator_mac_replayed`). A request captured before an
in-place restart therefore cannot be replayed after it. Only `server status`, `restart`
and `stop` accept a signed request. As defense in depth the secret is read only
while a live daemon holds the state-root lock and the lock records exactly the
transport the command is about to use (the socket path, or the bound host and
port as written: `localhost`, `127.0.0.1` and `::1` are different endpoints,
since IPv4 and IPv6 loopback can host different listeners on one port). A relay
or a process that took over the endpoint receives nothing it can reuse. An
explicit `CRUXIBLE_SERVER_BEARER_TOKEN` is sent as a bearer token, as before. `--bootstrap-secret-file PATH` also writes a 0600 copy to
PATH; it needs auth and is refused on an auth-off start, which removes any stale
state-root copy. With `--socket`, the socket is bound with mode 0600;
a missing socket directory is created 0700; a socket directory that is not
yours and owner-only, or an ancestor another user could use to replace it, is
refused at startup. State defaults to `~/.cruxible`; `--state-root` overrides
`CRUXIBLE_STATE_ROOT`. The obsolete `CRUXIBLE_SERVER_STATE_DIR` name is
refused.

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
refusal the stop existed to clear. A stopped socket daemon removes its socket
file. `server restart` waits until the probe answers from the NEW process
image (a different `boot_id` on `/version`), not the image it replaced. Against a daemon bound to TCP on another
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
an explicit `--auth`/`--no-auth` disagreement is refused, and a TCP service
without auth is refused (`service_install.tcp_requires_auth`). Service files contain
no bearer or bootstrap secret, and auth-on installation requires an active
durable runtime credential first. The rendered units run the daemon at normal
priority: the launchd agent sets `ProcessType` to `Interactive` (left unset,
launchd throttles the job's CPU and I/O), and the systemd unit sets `Nice=0`.

Run the daemon at normal priority however it is started. Agents wait on its
answers, and a niced daemon on a busy host turns a seconds-long floor export
into a minute-long one. zsh starts `&` background jobs at nice 5 (its
`BG_NICE` option, on by default), so `cruxible server start ... &` from an
interactive zsh runs niced; prefer the installed service, or run
`setopt NO_BG_NICE` first. `ps -o pid,ni,rss,command -p <pid>` shows the
daemon's nice value (`NI`) and resident set (`RSS`, in KiB).

Give the daemon enough memory to keep its working set resident. A floor
export holds several hundred MB per instance at its peak (about 640 MB on a
state of about 3,000 Claims). When the daemon's resident set is far below
that and the host is swapping, the first request after idle pages the heap
back in, and full garbage collections over that heap (from a quarter of a
second to several seconds each on such a state) run at swap speed. Free
memory on the host, or move the daemon to a host with headroom; the daemon
has no memory or garbage-collection settings to tune.

Every request line in `<state-root>/daemon/logs/server.log` carries
`duration_ms`, the wall time from the request's arrival to its log line, so a
slow request can be attributed to its route. Route handlers run in the
daemon's threadpool, so one slow request does not hold up the others.

`server status` answers an instance-scoped credential with its own host and
identity (`"scope": "instance"`) instead of refusing; the daemon-wide view below
needs the bootstrap secret. `server status` lists the daemon's exact current
compiler coordinate and each
governed host as `uninitialized`, `writable`, `reseed_required`, or
`decommissioned`, retaining a typed reason for malformed, retired or
decommissioned state. Its `Instances` count is the number
of governed daemon hosts shown, excluding unrelated local registry entries.
`server status` also lists the daemon's consumers on every instance it holds
open: each enabled Line and each built-in worker, as `running`, `stalled`,
`lagging`, `stopped`, or `disabled`. The built-in `next` worker runs on every
instance by default. It maintains the
current Claim queue, cited evidence availability and prediction windows under
one health entry, with independent cursors for each part. Queue rows carry
evaluation-time bounds; a `next.expire` deadline refreshes them at the next
boundary. Its evidence part re-hashes the Captures live Claims cite whenever a generation
cites one, and sweeps them all on a daily `evidence.sweep` trigger event.
Retention is evaluated at the generation or trigger event's recorded instant. A missing or corrupt Capture envelope, a
corrupt body, or a missing body its contract still requires to be retained is a
finding. A body whose contract lets it go (`optional` or `never_materialize`
retention, or a `required_for_duration` window that has passed) is not.
Its prediction part binds
each accepted ResolutionContract's observation window: a fixed window when the
contract is accepted, and an event window once per landed Capture its selector
matches, each its own contract instance. On first start it reads every live
contract and every retained matching Capture. It reads each bound window's
resolution journal initially and whenever a settlement or overturn lands there.
It stores whether an answer exists; reads decide whether an unanswered window
has closed at the request's evaluation time. Unbindable anchors are retried on
`prediction.anchor_retry` events (hourly by default) and new capture landings. A retired or revised contract withdraws its
windows. `CRUXIBLE_DISABLED_CONSUMERS=next` turns off the whole findings worker.
Unknown names, including the former `evidence` and `prediction` names, refuse
the setting. Enabled Lines remain a separate governed consumer.
`server status` also renders `Provider lane:` and, when degraded,
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
| `provider_index_urls` | `[]` | Explicit allowed package indexes and download origins, in lookup order. Without these, a transferred or repository install must supply locked dependency wheels, and an install by name uses PyPI. |
| `workspace_allowed_roots` | `[]` | Canonical absolute roots that widen `workspace.file` beyond an attached workspace; these are daemon-local authority and never come from an environment variable. The daemon state root, its trust, custody, Provider-secret, and instance substrate stay refused inside any allowed root. |

Unknown entries, non-positive timing values, malformed JSON, unsafe deployment
paths, and an unreadable file degrade only the Provider lane with a typed cause.
Provider installation uses the first-party `cruxible-provider-runtime` toolchain, which
the `cruxible` package installs as a dependency, and needs `uv` in the daemon
environment. Installed providers run in supervised child
processes; the one exception is `workspace.file`, which is built into core (see
[init](#init)) and runs inside the daemon with no provider install. It needs no
deployment and keeps running while this lane is degraded. Its receipts carry
`fence_scope: in_process` and egress observer `core.in-process`; a daemon crash
during one is closed at the next startup as `provider_in_process_interrupted`.
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

### Internal triggers

The daemon's internal schedules are governed Trigger artifacts
(`triggers/<name>.json`) aimed at an internal action, not daemon-local
configuration. Internal actions are registered in code (`evidence.sweep`,
`prediction.anchor_retry`, `floor.refresh`); a Trigger aimed at one takes a
`cadence`, `cron`, or `generation_accepted` schedule. Capture-landing and
window-close schedules for internal actions are
not supported yet (`cruxible.trigger.schedule_unsupported_for_action`), and an
action name that is not registered is refused at acceptance
(`cruxible.trigger.action_unknown`). A new
instance is initialized with `evidence-sweep` (daily) and
`prediction-anchor-retry` (hourly); change a schedule, add a Trigger, or retire
one through an ordinary proposal. `cruxible next` reports unscheduled findings
actions, and an unscheduled floor action when workspace delivery is enabled.

No Trigger fires retroactively. A timer fires each of its instants once, all of
them after the acceptance of its Trigger version: a new cadence first fires one
interval after its acceptance (never on sight), a cron schedule at its first
calendar instant after acceptance, and a changed schedule starts again from the
successor's acceptance. Instants that pass while no daemon is running are
skipped when it restarts, never fired late as a catch-up; a retired Trigger
stops. Fires, which record the Trigger and the action, and pending one-shot
deadlines are retained under each instance's `exhaust/triggers.sqlite3`; this
append-only event log is not disposable worker state. Workers follow fires by
action and resume from its sequences.

A `generation_accepted` schedule has no fields or predicate. It fires once at
latest head when accepted generations advance; a burst coalesces. It skips
accepts before the Trigger version's acceptance and before listening starts,
including accepts made while the daemon was stopped. Lines use the same target
input law, so a Line needing a Capture event refuses this schedule.

New instances seed the ordinary governed `floor-refresh` Trigger with schedule
`generation_accepted` and action `floor.refresh`. Edit or retire it through the
usual Trigger authoring flow; existing instances are unchanged.

`floor.refresh` warms the floor index on every daemon. Workspace delivery is
on by default for an attached workspace. Use `workspace attach --no-floor-delivery`
or `cruxible floor delivery off` through the local Unix socket
to opt out; `on` enables delivery again. Detaching clears it, and a later attachment
defaults on again. Host inspection and daemon status label it "on (default)" or
"off (opted out)". With delivery
on, the daemon is the floor's only writer: it writes only `.cruxible/floor`,
and a `floor export` over the local socket asks it to deliver immediately, while
one over TCP refuses rather than write a second copy (the registration route
answers `delivers_here` for the caller's workspace root without echoing the
daemon's path). Workspaces the daemon does not deliver to (a remote daemon,
delivery off, MCP library mode) pull the floor with `floor export`, MCP
`cruxible_floor_export mode=write`, or SDK `cx.refresh_workspace()`. Both writers create `.cruxible/floor/.gitignore` containing
`*`, which ignores the entire floor, including itself, in Git. It is local metadata
outside the accepted floor manifest and survives delta applies and full repairs.
A failed apply stalls the floor consumer with `cruxible floor export` as its
repair; automatic retries wait for a changed head or workspace registration.

### Proposal receive operational configuration

`<state-root>/daemon/proposal-receive.json` is another daemon-local operational
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
preflight, typed `cruxible.authoring.change_set_record_too_large`, naming the
entry count that fits: on the projected count before anything is lowered when
that already exceeds the bound, and on the exact lowered count -- still before
the compile -- when it does not.

## mcp

```text
cruxible mcp
```

Serves the MCP tools over stdio. It is the same server as the `cruxible-mcp`
script and reads the same environment (see [MCP tools](mcp-tools.md)); it exists
so launchers that run a package by its own name, such as `uvx cruxible mcp`,
reach the server. The root options and remembered CLI context do not configure
it. Every tool runs on a daemon: with no transport configured, the server reuses
the local daemon on `~/.cruxible/run/daemon.sock` or starts one (see
[MCP tools](mcp-tools.md#the-daemon)).

## host and workspace

~~~text
cruxible host create [--instance-id ID] [--workspace DIR] [--replace] [--dry-run]
cruxible host show INSTANCE [--json]
cruxible workspace attach [--instance-id ID] [--replace] [--no-floor-delivery]
  [--dry-run|--commit] [--at DIGEST]
cruxible workspace detach [--instance-id ID] [--dry-run|--commit] [--at DIGEST] [--json]
~~~

`cruxible init` creates the host itself when no instance is selected, so a new
project is one bare `cruxible init`; `host create` stays for allocating a host
without becoming its owner. `host create` allocates an empty daemon-owned host
and remembers it. When the selected daemon
is reached through `--server-socket` or `CRUXIBLE_SERVER_SOCKET`, the command
also registers the selected Git worktree with the daemon. Every selected
workspace gets an atomic `.cruxible/coverage.json` v2 write containing exactly
one transport, the instance ID, and the fixed floor profile; bearer credentials
and secrets are never inputs to that writer. A differing config is refused
unless `--replace` is explicit. Because the binding may carry a local socket,
the writer adds `.cruxible/coverage.json` to this repository's machine-local
`.git/info/exclude` rather than changing a shared ignore file.

Daemon floor delivery is on by default when a local workspace is registered.
`workspace attach` enables it unless `--no-floor-delivery` is supplied;
`floor delivery STATE` takes `on` or `off`: `off` opts out after
attachment, and `on` restores it.

A TCP client never sends its local path to the daemon. Implicit attachment from
inside a TCP worktree remains refused; explicit `--workspace DIR` instead writes
a client-local `server_url` binding without claiming daemon registration. Use a
local socket when the daemon must advertise ledger refs into that worktree.

With auth on, `host create` is authorized by the daemon's runtime bootstrap
secret, which is its unscoped operator credential. That authorization is
repeatable, exactly as it is for `server status`, `server restart` and
`server stop`: a daemon hosting several instances allocates each of them with
the same secret, and `credential claim-bootstrap` -- claimable once per host,
so each host claims its own first ADMIN credential with the same secret and no
restart -- does not revoke it. An instance-scoped credential cannot allocate a host on the
daemon that hosts it, and the refusal names the bootstrap secret as the
credential to present.

`host show` is a zero-authority inspection of workspace registration, exact
compiler coordinate/revision, and write compatibility; the CLI adds the selected
transport. The daemon-local managed root is visible only to an unscoped operator,
not an instance-scoped credential. `workspace attach` requires a Unix socket, so
the daemon can see the path it is asked to register. A host with no worktree
registers this one, whether or not Cruxible is already initialized under it: an
initialized host attaches in place (nothing is rebuilt) when the worktree is in
its ledger's Git object format and holds no part of its managed root, and it
advertises the accepted ref into the worktree at once. A host already
registered to this worktree just gets the client config. Either way
`.cruxible/coverage.json` is written only once the daemon holds the exact
current worktree. A host registered to a different worktree is a typed refusal
naming `workspace detach` as the repair, and no config is written. `host create
--workspace` and `init --workspace` take the same attach path.

`workspace detach` releases a host from the worktree it registers. The registry
holds one host per worktree, so moving a worktree to a second host needs the
first one released; nothing governed changes, the host keeps its ledger and
every read it has ever served, and it stops being the host of this directory.
It requires the same local socket for the same reason attaching does. The
instance's own ADMIN credential or the bootstrap secret may detach it. It refuses
while the host still registers published blocks in that worktree, because
detaching under them leaves a page carrying markers no host owns: depublish
those blocks (`cruxible block depublish`) or retire their backing Claims first.

## init

~~~text
cruxible init [--key-dir DIR]
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

With no instance selected, init first creates a host (as `host create` does),
selects it, and initializes it: a retry after a failure initializes that same
host. Makes you the owner under `--principal-id` (default: the configured
`CRUXIBLE_PRINCIPAL_ID` / global `--principal-id`, which must agree with the
flag, else your OS username lowercased; a username that is no principal ID is
refused with the `--principal-id` repair). `--key-dir` defaults to
`$XDG_CONFIG_HOME/cruxible/keys/INSTANCE/PRINCIPAL` (`~/.config` when unset): a
per-user path outside the workspace and the daemon state root. Every custody
directory (owner, reviewer and recovery, default or explicit, with symlinks
resolved and case ignored) is refused inside either before any host is
allocated or key generated; a refused default is reported after the new host
is selected, so the retry initializes that host. On an auth-off daemon the init request claims that
principal, so no bootstrap secret is needed. Init writes the owner's settings
file (`DIR/cruxible.env`) and remembers it in the CLI context, so later commands
act as the owner without sourcing anything (see [context](#context)). An init
whose caller is not one of the owner principals it names is refused with
`cruxible.identity.init_owner_mismatch`.

Generates a client-held ordinary key outside the workspace and bootstraps the
ledger with its public principal record. A missing `--key-dir` is created with
mode 0700; an existing one must already exclude group and other access. An optional `--reviewer-key-dir` adds a
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
owner, reviewer, and recovery custody by default.

Successful initialization remembers the initialized instance and atomically
writes the selected workspace config before rendering either JSON or human
output. For a daemon-registered local worktree, the advisory `cruxible-ledger` remote
fetches accepted state as `cruxible-ledger/accepted` and open proposals as
`cruxible-ledger/proposals/<proposal-digest>`. These are remote-tracking refs only:
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
`cruxible.init.object_format_conflict` before any state is written; instances
already initialized keep their pinned format forever. The
equivalent request field is `git_object_format` on the HTTP init body.

`--mirror-url` binds the ledger mirror during bootstrap, before subsequent
proposals. An instance can publish nowhere initially; `cruxible ledger set-mirror`
adds a destination later. See [ledger](#ledger) for URL syntax.

Initialization creates governed state only, and needs no provider checkout or
executable environment. A new instance starts with `workspace.file` built in: its
ProviderInterface and the `cruxible-builtin` Provider are part of the first
generation, so a Procedure can pin and run a workspace Source with no install.
(`cruxible-provider-workspace` is not published; instances that installed it
before keep it.) Install other provider packages with `cruxible provider install`.

## capture

```bash
cruxible capture read CAPTURE_DIGEST [--max-bytes BYTES]
```

Verify a retained Capture and return its evidence metadata and bounded material as JSON.
CAPTURE_DIGEST is the full digest, the `CAP-<12 hex>` handle `get --detail
evidence` and Capture cards print, or a `sha256:` prefix of 12+ hex; a handle
or prefix must name one Capture, resolved exactly as the write verbs resolve
`--capture`: one accepted Claims cite, or one the instance holds that verifies
(`cruxible.capture.ref_ambiguous` lists the candidates,
`cruxible.capture.not_found` points at `orient --section captures`, and
`cruxible.capture.ref_scan_exhausted` asks for a longer handle when more share
the prefix than one bounded lookup examines).
Uses body-read permission and never refetches the external source. The SDK equivalent
is `cx.capture(digest)`; its `.ref` can be passed to Claim authoring as `supported_by`.

## body

~~~text
cruxible body store PATH
~~~

Stores exact bytes in inert CAS and prints their digest.

## instance

~~~text
cruxible instance decommission --reason TEXT [--dry-run|--commit] [--at OID]
~~~

Decommissioning is the terminal lifecycle state of one governed instance. It
stamps the reason, instant, and actor on the instance descriptor, so a daemon
restart replays the same state. Every further governed write refuses with the
typed `cruxible.instance.decommissioned` error naming the reason and the repair;
reads keep serving at the accepted coordinate, `next` reports the terminal state,
and `search --mode orient` marks the orientation decommissioned.

Nothing is deleted. Every accepted generation, receipt, and body stays exactly
where it is, and archiving or erasing the directory afterwards is the operator's
own step — no verb performs it, and the state cannot be reversed. So the command
previews first and changes nothing; the confirmation is that preview's
coordinate: `--commit --at OID` (see [Previews](#previews)).

## ledger

~~~text
cruxible ledger set-mirror URL [--dry-run|--commit] [--at OID]
cruxible ledger set-mirror --clear [--dry-run|--commit] [--at OID] [--json]
cruxible ledger publish [--timeout 0..60] [--dry-run|--commit] [--at OID] [--json]
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
reviewer sees, locally as `cruxible-ledger/proposals/<proposal-digest>` and on the
mirror as `refs/heads/proposals/<proposal-digest>`, is the projection of ONE
evaluated candidate, which is what a digest names and what a name does not: the
same `<actor>/<name>` ref carries a different candidate after every
resubmission. So `git diff cruxible-ledger/accepted...cruxible-ledger/proposals/<proposal-id>`
takes the digest that `proposal list` and `proposal review` print.

Re-keying that branch to `<actor>/<name>` is a deprecate-then-remove candidate,
not a rename: the workspace advertisement fetches that refspec, so it is a
shipped surface. It would also
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
that expired or a remote that was deleted puts the `ledger_mirror` facet of
`cruxible next`'s status at `behind`, carrying the URL and Git's own reason.

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
Missing local status is rebuilt; it is not ledger authority.

The URL never carries a credential. `https://user:token@host/...` is refused,
as is plain `http://`, `ext::` and anything whose host or user begins with a
dash (`ssh://-oProxyCommand@host/x` puts it where the transport reads its own
arguments); the four
accepted shapes are `https://`, `ssh://`, `user@host:path` and an absolute local
path (or its `file:///` spelling). The daemon reads its own token from
`CRUXIBLE_MIRROR_TOKEN` in its environment and sends it as an HTTP
Basic `Authorization` header built through Git's environment-config protocol, so
it appears in no command line, no config file and no error message. SSH and
local remotes use no token at all: SSH authenticates as the daemon itself.

Create the remote in the ledger's own object format — `git init --bare
--object-format=sha1` or `sha256`, matching `cruxible init --object-format` —
because Git refuses a push between repositories with different hash algorithms.

`set-mirror` publishes immediately, so a wrong credential or an unreachable host
is reported at once rather than at the next governed write. It stays bound
either way: a remote that is temporarily unreachable is not a wrong remote.
`cruxible orient` prints the URL a reviewer clones (`mirror_url` in
`--json`), and `cruxible next` reports the mirror's health with its repair.
`set-mirror --clear` unbinds the mirror: nothing more is published, and what
was already sent stays on the remote. It commits by default; `--dry-run`
previews it. The equivalent surfaces are `POST /{instance}/ledger/mirror`,
`POST /{instance}/ledger/mirror/clear` and the `mirror_url` field on the init
body.

## provider

~~~text
cruxible provider list [--json]
cruxible provider install NAME[==VERSION] | WHEEL [--lock FILE]
  [--dependency WHEEL]... [--extra NAME]... [--control-domain NAME] [--reverify]
  [--dry-run|--commit] [--at OID] [--json]
~~~

Installation requires **ADMIN**. A package name resolves through the daemon's
configured repository when it has one. Otherwise it installs from the provider
index: the newest final release, or exactly `==VERSION`. The first configured
index that lists the package is the only one consulted, the wheel must match the
hash the index publishes, and the environment is materialized from the lock the
wheel embeds. With no `provider_index_urls` configured, the index is PyPI
(`https://pypi.org/simple/`, files from `https://files.pythonhosted.org/`). A
local wheel requires `--lock`; `--dependency` supplies local or offline locked
dependency wheels. Local paths are read by the client and transferred through
CAS, so this also works against a remote daemon.

The shared installer prepares an exact Python environment, verifies it once,
checks package classifiers in supervised children, and proposes the package's
provider interfaces and Provider definition through ordinary acceptance.
The registration lands at once when the approval policy requires no approval
(`ready`); otherwise it stops at proposed (`awaiting_approval`) for the ordinary
review and activation. It returns `ready`, `awaiting_approval`, or `blocked`,
with per-operation missing requirements. `--control-domain` names the control
domain the Provider definition records (default `operator`); `--at OID` commits
only if accepted state is still the coordinate a preview answered at.
Installation requires the current compiler; an instance on an older one runs
`cruxible compiler upgrade` first. Missing browser resources remain explicit; Python extras do not
install browsers. Credentials, grants, and invocation remain separate.

Retries reuse the prepared installation and an open registration proposal.
Package updates create a new environment and preserve earlier deployments.
Runs reuse the retained verification record without hashing the environment.
Treat installed environments as immutable; `--reverify` detects manual changes
and refuses drift instead of silently resealing or repairing it.

Installing is an operator job, so the SDK has no install method; the transport
`client.install_provider` takes a typed request. MCP: `cruxible_provider_list` and
`cruxible_provider_install`. HTTP: `GET /{instance}/providers`
and `POST /{instance}/providers/install`.

## kit

~~~text
cruxible kit build --id ID --version X.Y.Z --owns PREFIX. [--owns PREFIX.]...
  --out KIT_DIR [--provider PACKAGE_DIR]... [--json]
cruxible kit add KIT [--source TEXT] [--keep IDENTITY]... [--keep-local-edits]
  [--retire-dependents IDENTITY]... [--allow-downgrade]
  [--dry-run|--commit] [--at OID] [--json]
cruxible kit pull REFERENCE --out DIR [--layout] [--json]
cruxible kit status [--offline] [--json]
cruxible kit remove ID [--dry-run|--commit] [--at OID] [--json]
~~~

`KIT` is a kit directory, an OCI image layout directory, or a registry
reference. A bare name such as `project-state:1.0.0` resolves under
`ghcr.io/cruxible-ai/kits`; a full reference names its registry
(`ghcr.io/acme/kits/foo:2`, `localhost:5000/kits/foo@sha256:...`).

Distributed, a kit is an OCI artifact (`application/vnd.cruxible.kit.v1`): the
manifest is its config blob and the artifacts (with any bundled provider files
under `providers/`) are one deterministic, uncompressed tar layer, so rebuilding a
release gives the same manifest digest.
There is no public publishing in v1: official kits are published by internal
release tooling, which never moves an existing version tag to different content.
`pull` fetches and verifies a kit into a kit directory (or, with `--layout`, an OCI image
layout for offline transfer) without installing it. Every blob is checked
against its digest, a digest reference must match the manifest pulled, and blobs
are cached by digest under `CRUXIBLE_ARTIFACT_CACHE` (default
`~/.cache/cruxible/artifacts`) and re-hashed on every use; responses are bounded
while they stream, and layers admit only plain regular files. Registry
credentials come from `CRUXIBLE_REGISTRY_USERNAME` and
`CRUXIBLE_REGISTRY_PASSWORD`, apply only to the host named in
`CRUXIBLE_REGISTRY` (for example `ghcr.io`), and are sent only to a token realm on
that host. Every other registry is contacted anonymously, a registry token is
never sent to another origin (such as an upload location), and nothing is sent
over a downgraded scheme. The client does all fetching: the daemon receives only the verified
bundle, and the receipt records the source pinned to its manifest digest.

A kit is one release of definitions: ClaimTypes, CaptureContracts,
QueryDefinitions, SourceAcquisitionPolicies, Procedures, Blueprints and the
ProviderInterfaces they need, plus the provider packages its Procedures run on.
It never carries authority (governance, principals, mandates), local binding
(Provider artifacts, Lines) or state (Subjects, Claims). A kit directory holds
`cruxible-kit.json`, the artifact bytes under `artifacts/` and the bundled
provider files under `providers/`.

A Procedure or Blueprint moves its pins through its graph (its definition digest
is recomputed) and a SourceAcquisitionPolicy moves only its pins. A
ProviderInterface is carried, never owned: it belongs to the provider package
that registers it, so `remove` never retires one.

Provider packages. A kit ships the provider its own Procedures run on (for
example a domain parser) as the package's built wheel, the uv lock it was built
with, and the wheel of each dependency that lock names by path (such as
`cruxible-provider-runtime` while it is unpublished); registry dependencies are
never bundled and resolve by name, pinned by the lock's hashes, from the daemon's
provider index (PyPI unless the operator configured `provider_index_urls`). The
kit never carries source. `--provider PACKAGE_DIR` (repeatable) names a package
directory in the kit's source tree: its `pyproject.toml`, its `uv.lock`, and in
`dist/` its built wheel plus the dependency wheels (`uv build --wheel --out-dir
dist` for each). The CLI stages the files through the body store and the
manifest records each package (provider id, package, version, interfaces) with
the sha256 of every file. `build` refuses `cruxible.kit.provider_not_bundled` when
a carried Procedure pins a Provider the kit does not bundle,
`cruxible.kit.provider_build_differs` when the bundled wheel, lock or path
dependency wheels are not the build this instance installed (the dependency
closure is compared through the materialization digest, resolved for this host
from the bundled files), and `cruxible.kit.interface_not_bundled` or
`cruxible.kit.interface_differs` when a carried ProviderInterface is not exactly
what a bundled wheel registers (installing that wheel would otherwise propose a
successor over the kit's interface). Blueprints stay for slots the consumer
fills: their slot interfaces come from a bundled package, and the consumer
instantiates them with any installed Provider of that interface. The
compiler-seeded built-ins (`Provider:cruxible-builtin`, `ProviderInterface:
workspace.file`) are never bundled or carried: a kit pins them as they are, and
`add` requires this instance to hold the same seeded ones.

Authoring a kit that ships its provider:

1. Write the provider package in the kit's source tree (start from the provider
   template), `uv lock` it, and build its wheel and the wheel of each dependency
   its lock names by path into its `dist/`.
2. Install that wheel on the authoring instance (`cruxible provider install
   dist/WHEEL --lock uv.lock --dependency dist/RUNTIME_WHEEL`) and author the
   kit's Procedures against it; leave a Blueprint slot where the consumer picks
   the provider.
3. `cruxible kit build --id ID --version X.Y.Z --owns PREFIX. --provider
   PACKAGE_DIR --out KIT_DIR`.
4. Change the wheel only with a new package version: consumers holding another
   build of a bundled provider refuse the kit until it is replaced.

A release is self-contained. `build` exports every live definition whose
identity starts with an `--owns` prefix, plus every definition those pin, as
snapshots with no predecessor that pin only the release's own digests; a pin into
anything a kit cannot carry refuses the build (a Provider pin stays as built and
names a bundled package). The release content digest
therefore names the same definitions wherever the kit is installed, and any
release can be installed on its own. The manifest records where it was built
(the building instance, its accepted coordinate and the building principal),
shown by the install preview and `kit status`: claimed, not proven, and not part
of the release identity.

`add` diffs the release against this instance and always proposes that diff as
one change set: a missing definition is added as released, a changed one is
replaced by a successor naming this instance's current digest, and pins are
remapped to the digests this instance actually holds. A definition this instance
holds differently takes the release's version as a successor of its own, and the
preview says what that does here: `overwrites your edit` (edited since install,
compared by content, so a reverted edit is no edit), `re-adds a definition you
retired`, `takes over a definition you defined outside the kit`, or `replaces a
definition the kit depends on`. `--keep IDENTITY` (such as
`ClaimType:acme.account.seats`, repeatable) and `--keep-local-edits` keep this
instance's version instead; the receipt records each kept divergence, so a later
release that leaves that definition as it was does not ask again. Every live
artifact pinning a replaced definition takes one successor in the same change set,
carried to the kit's final definitions (a Procedure or Blueprint moves its pins
through its graph; a Procedure's Blueprint origin stays as recorded); the preview
counts each definition's dependents rather than listing them.

A definition the kit installed that the release dropped retires when nothing
live depends on it. Dependents include the live ClaimTypes whose evidence rules
admit captures under a dropped CaptureContract (by identity or exact digest),
though no pin names it. One with live dependents is kept until a decision names it:
`--keep IDENTITY` keeps it live, `--retire-dependents IDENTITY` retires it and its
dependents. A kept definition stays the kit's even when a later release narrows
its prefixes. Changing a definition another installed kit owns or holds, or
owning a prefix that overlaps another kit's or takes in a definition it holds,
blocks the change. `add` refuses a release older than
the installed one unless `--allow-downgrade`, and the preview names the transition
(install, upgrade, downgrade, reinstall).

`add` and `remove` land at once when the instance's approval policy requires no
approval, like provider install and value writes; otherwise they stop at
proposed for the ordinary `cruxible proposal approve` and `activate` steps. `add`
records a `kit_receipt` Document, `documents/kit-<id>.json`, with each path's
release digest, installed digest and content digest, and the bundled packages.

A kit that bundles provider packages installs them before its definitions. A
commit stages each file in the daemon's body store (the CLI and MCP adapters do
it; a preview stages and installs nothing and reports `would install`), then
installs each package not yet installed through the ordinary transfer install,
which needs the install permission (`ADMIN`) and lands when the approval policy
requires no approval. The definitions are proposed once every bundled provider
is installed; while an install awaits approval the result is
`awaiting_providers` with that install's proposal, and `kit add` again after it
is activated proposes the definitions. A commit first reads the staged files
(metadata only), refuses `cruxible.kit.provider_manifest_mismatch` unless they
reproduce the manifest, and refuses what the current state already decides
(ownership overlap, a disallowed downgrade, a carried interface held
differently) before installing anything; after installing it refuses
`cruxible.kit.provider_not_installed` unless every bundled Provider is live. A
package already installed from the same wheel, lock and dependency closure is
unchanged (a preview, which has no files, compares wheel and lock). Another installed build of a bundled provider is
refused (`blocked`) rather than replaced: replacing a Provider owes a successor
of every live Procedure pinning it, which an install does not carry, and the
build may serve Procedures outside the kit; replace it deliberately with
`provider install` and add the kit again. A Provider digest names the
environment its package materialized in (platform, machine, Python and markers),
so a release Procedure's Provider pin moves to the build installed here; its
implementation digests (interface, entrypoint, wheel sha256) are the same
everywhere. A carried ProviderInterface this instance holds differently blocks
the change.

`status` lists installed kits, the kit paths edited locally, the divergences kept
on purpose, where each release was built, and each bundled provider package with
its install state here (`installed`, `differs` with the installed version, or
`missing`). For a kit installed from a registry
the client lists the repository's tags (MAJOR.MINOR.PATCH, short timeout) and
shows the latest available version; `--offline` skips the check, and a kit from a
directory or layout shows its local source. Installing an update stays explicit:
`kit add REFERENCE:VERSION`. `remove` retires what a kit owns (never what it only
carries, and never a bundled provider, which stays installed); a path edited
since install, or the dependency closure while live Claims depend on those
definitions, blocks it. Removing a kit that is not installed
refuses with `cruxible.kit.not_installed`, naming the installed kits.

MCP: `cruxible_kit_build` (`providers` names packages staged with
`cruxible_body_store`), `cruxible_kit_status` (with the same update check,
`offline`), `cruxible_kit_add` (by registry `reference`, which the adapter pulls,
or inline `bundle`; a commit stages its provider files) and
`cruxible_kit_remove`. HTTP:
`POST /{instance}/kits/build`, `GET /{instance}/kits`,
`POST /{instance}/kits` and `POST /{instance}/kits/remove`.
SDK: `read_kit_directory`, `write_kit_directory` and `stage_kit_providers` in
`cruxible_client.kits`, with the matching `CruxibleClient` methods.

## document

~~~text
cruxible document propose --envelope FILE --name NAME [--dry-run|--commit] [--at OID]
~~~

`document propose` is the only Document subcommand; Documents are read through
the general reads:

| To | Run |
|---|---|
| list accepted Documents | `cruxible orient --section documents` |
| read one Document's envelope | `cruxible get Document:NAME` |
| read its body | `cruxible get Document:NAME --detail body [--range a:b] [--output FILE]` |
| explain it | `cruxible get Document:NAME --detail why` |
| read its revisions | `cruxible get Document:NAME --detail history` |

`--detail body` answers one page of at most 64 KiB; `--range start:end` reads a
byte range, and `--output FILE` writes the body's exact bytes to a new file,
paging past the cap.

A Subject is an identity-only referent named by its canonical `kind/name`
address, the spelling the SDK, claim objects, floor profiles, and `get` all
use. `cruxible get KIND/ID` renders the Subject's own facts and an
`incoming` section: every live Claim whose subject-valued object is this
Subject, grouped by predicate and naming the asserting Subject and the Claim
id. A relation is stored once, on the asserting Subject, so without this
section nothing answers "what touches this package" from the object side.
`--detail history` reads the Subject's revisions.

## claim-type

~~~text
cruxible claim-type propose --template
cruxible claim-type propose --input FILE --name NAME [--dry-run|--commit] [--at OID]
cruxible claim-type migrate REQUEST_FILE
cruxible claim-type upgrade [--claim-type P]... [--revision-evidence replace|accumulate]
  [--dry-run|--commit] [--at OID]
~~~

Read the accepted ClaimTypes with `cruxible orient --section
claim_types`, one ClaimType with `cruxible get ClaimType:PREDICATE`.

A ClaimType is the governed interface a predicate must satisfy before any Claim
may state it. `propose --input` accepts a complete `ClaimTypeInputRecord`; ClaimType
is not part of the authoring coordinator's example vocabulary. `propose
--template` prints a complete literal `project.work_item.status` input with a
`repo.replace-me` foreign-source evidence rule and does not contact the daemon.
An evidence rule names the CaptureContracts it admits by identity, in
`capture_contracts` (`CaptureContract:<name>` or just the name); it admits
evidence captured under every accepted version of those contracts, so improving
a contract through a compatible successor needs no ClaimType change. Replace
`anticipated_source_ids` with the logical source used by `authoring bind`;
the source-intent lint then names the deterministic foreign-source
CaptureContract to place in the rule. Flow-A binding carries that exact
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
the `claim_type_succession` change-set member under `cruxible authoring`, which
lands the succession in the same generation as the Claims that speak the new
vocabulary and adds one disposition the operator form has no use for --
`re_author`, whose successor is a sibling Claim member of the same set. Both
roads build their candidate with the same function, so neither can drift from
the other's law.

Every ClaimType authoring path writes ClaimType v7 with identity evidence rules:
`propose --input`, the SDK draft, the authoring examples and change-set members.
A rule that names an exact contract digest no accepted contract version carries
is refused, never authored as an exact-digest rule. On an instance whose
accepted compiler predates revision 31 (which admits neither v7 nor identity
rules), authoring refuses `cruxible.claim_type.compiler_upgrade_required`, whose
repair is `cruxible compiler upgrade --to <current compiler>`.

`upgrade` is the one move for an older ClaimType. A v1-v5 ClaimType whose rules
still name contracts by exact digest first takes the identity-rule conversion,
then the v7 move, in the same change set, carrying its Claims. A rule converts
only when it keeps its meaning: every version of a named contract must be
compatible with its predecessor, and two rules that did not overlap may not start
matching the same evidence. It lists, per ClaimType, the accepted contract
versions a converted rule newly admits, and leaves the rest unchanged with the
reason. The change set carries every dependent Claim, so it previews by default
(see [Previews](#previews)); commit the preview with `--commit --at OID`, then
approve and activate the proposal as usual.

ClaimType v7 adds a `description`, `member_descriptions` for a literal enum, a
`default_role` a write takes when it names none, an `evidence_requirement`
(`none`: the Claim's own origin supports it; `self`: the evidence rules decide;
`captured`: a Capture under a declared contract is required) and a
`revision_evidence` rule (`replace`: a revision that changes its statement keeps
exactly the evidence it cites; `accumulate`: it keeps everything its
predecessors cited). A revision that states the same thing again keeps its
evidence either way, and a carry never loses backing. `propose --input` lowers
to v7: a new ClaimType takes `self` and `replace`. An edit follows JSON merge-patch
against its predecessor: a v7 field left out keeps the predecessor's value (over
a ClaimType before v7: no descriptions, no default role, `self` and
`accumulate`), and an explicit `null` clears `description`,
`member_descriptions` or `default_role`. Inherited member descriptions of
values the edited enum dropped, or an inherited default role the edit no longer
permits, are refused by name rather than dropped. ClaimTypes before v7
never switch on their own: `upgrade` proposes one change set moving the named
live ClaimTypes (default: all of them) to v7 with `evidence_requirement` kept at
`self` and `--revision-evidence` (default `replace`), carrying their Claims with
their backing intact. It lists each ClaimType's revision-evidence change;
`--dry-run` evaluates the change set and proposes nothing.

A CaptureContract successor must be compatible with its predecessor: it may
widen the sources, modes, identities and evidence kinds it accepts and raise its
budgets, and nothing else. A breaking change is a new contract identity. A
contract cannot be retired while live evidence rules name it, and cannot move
while exact-digest rules or ResolutionContract windows still name its previous
version; the refusal names them.

## claim

~~~text
cruxible claim attest IDENTITY --support|--contradict|--unsure [--note TEXT]
  [--valid-until TS]
cruxible claim recover-attestation
~~~

Claims are read through `cruxible query`, `cruxible get` and `cruxible orient`.
`cruxible query KIND --select P --claims` is the status table: each
cell names its Claims with their ID, value, verdict, resolution status and
role, for every Subject of `KIND` (narrow with `--where 'subject_id in a,b'`);
`--status overturned --status refused --status retired` adds the Claims
resolution set aside or that were retired. A query lists a kind's live Subjects;
`--status retired` also lists its retired Subjects, and every row then states its
Subject's `lifecycle` (`live` or `retired`). `cruxible get CLM-...`
reads one Claim's card; `--detail why` its verdict with the law evidence and
source handles it was computed from, `--detail history` its revisions, and
`--detail proof` its full envelope and facts. `orient` counts Claims by status
under `artifacts.claims`.

Claims are written through `cruxible set`, `add`, `retire` and `write`, or authored
through `cruxible authoring submit`/`compile`; the retired direct v1 proposal
commands are not a second writer. `cruxible retire` retires a Claim with its
complete dependent Claim closure in one change set. When a Claim shares anything
with a retired Claim, `get CLM-... --detail why` also carries a
`retirement_context` section. It is review context, not queue work, so `next` does not
report it. It lists each retired Claim the Claim shares a capture, an exact
external source, or a same-version cited span with, with the relation kind, the
shared capture, and the retired Claim and citation witnesses. It is read from
accepted state alone, like the rest of the explanation.
A real dependency on a retired Claim stays a `claim_dependency_stale` row in
`next`.

`attest` signs that the caller examined the current exact Claim. `--unsure` is
how an agent leaves contested state contested instead of forcing a judgment it
is not confident in: it holds the Claim's rows in `next` (see below) until what
the agent examined changes. `--valid-until` ends the attestation, and with it
the hold.
`get CLM-... --detail proof --json` carries the typed statement (subject,
predicate, object, role, qualifier, lifecycle, predecessor digest) in its
top-level `statement` field alongside the canonical envelope.

`recover-attestation` is an admin-only repair for an interrupted
Claim-attestation evidence-ledger append. It rolls the sole durable unpublished
event forward and refuses rather than choosing between ambiguous histories.

## authoring

~~~text
cruxible authoring submit PAYLOAD [--dry-run] [--and-activate]
cruxible authoring submit --intent-id INTENT_ID [PAYLOAD] [--and-activate]
cruxible authoring example [NAME]
cruxible authoring example claim-cite-supporting-evidence
  --attestation-claim-id CLAIM_ID --capture-digest DIGEST
cruxible authoring compile PAYLOAD [--intent-id INTENT_ID]
cruxible authoring bind --file PATH --anchor TEXT [--occurrence N]
  [--window-lines N] [--workspace-root DIR]
  --payload-file CLAIM_STUB
cruxible authoring preflight INTENT_ID
cruxible authoring rebase INTENT_ID
cruxible authoring status INTENT_ID
cruxible authoring get INTENT_ID
cruxible authoring list
~~~

PAYLOAD is a tagless authoring input file, or `-` for stdin. `authoring submit
PAYLOAD` compiles, checks and submits in one call; `--dry-run` returns the
preflight the submit would run, with every refusal, and saves no intent. Durable
intents are optional, for staged work on large definitions: `compile PAYLOAD`
creates one (its ID is `certificate.intent_id`), `compile PAYLOAD --intent-id ID`
revises it (the daemon keeps every revision), `rebase ID` advances a refused one
to the accepted head, and `submit --intent-id ID` submits it. `get` and `list`
find staged work again after the context that started it is gone.
`authoring example` prints a model-generated template for NAME, or lists the
names without one.

`authoring bind` reads `--file` through the workspace's source catalog (the
current worktree's, or `--workspace-root`'s): the stub's `source_id` must be the
name the catalog gives that file, so a mistyped name is refused instead of
minting a citation `next` and coverage never match. An evidence-only catalog
entry (`name` and `locator`) is enough.

A Claim input names the Claim it revises with `revises`, a Claim ID; omit it
to state a new Claim. `authoring example claim-revision` prints one. The three
attestation-door examples (`claim-cite-supporting-evidence`,
`claim-adjudicate-contradicting-evidence`, `claim-adjudicate-unreviewed-evidence`)
revise the Claim named by `--attestation-claim-id` to cite the Capture named by
`--capture-digest`, and require both.

**Intent retention.** A local daemon keeps an authoring intent only while it is
in progress. Only unsubmitted drafts expire, after a day untouched; submitted work
stays available until it finishes. A finished intent (accepted, superseded or
terminal), including one accepted through its proposal, is reduced to a receipt of
its final state, and the 16 most recent receipts are kept, so a retried `submit`
or `status` still answers. Read older results from accepted state. Set
`CRUXIBLE_AUTHORING_INTENTS=durable` on a managed daemon to retain every intent's
full event stream.

**Read receipts.** A local daemon records no read-touch (consumption) receipts:
`CRUXIBLE_CONSUMPTION_RECEIPTS` defaults to `off`, so reads write no receipts. Set it
to `on` on a managed daemon to record one receipt per served artifact. Receipts
feed curation only: with them off, dead-vocabulary detection stands down
(`consumption_receipts_off`). If a daemon that was recording is later run with
receipts off, it marks the instance unobserved once, and when recording resumes
the detector stays silent (`consumption_observation_gap`) until a receipt is
written, then counts zero use only from that point.

One authoring intent is one changeset. The tagless `change_set` input carries
any mix of members -- `claim`, `claim_type`, `claim_retirement`, `subject`,
`query_definition`, `procedure`, `procedure_mandate`, `acquisition_policy`,
`line` -- and the whole intent
lowers once, proposes once and admits or refuses together, typed to the member
index that offends. `approval_policy` and `procedure_runtime_policy` are the
two exceptions: the member union parses either, but a change set carrying one
refuses whatever else it holds, so author each as its own singleton input. A
Claim member may define the Subject and ClaimType it needs in the same set, and
may retire a Claim the set does not otherwise touch. Two sibling Claims contending for
one cardinality-one slot are un-authorable in a single set by construction, not
merely unrepaired: dispositioning one needs the other's Claim ID, which the
daemon mints only at create from the already-frozen payload, so that refusal's
repair is to merge the two decisions or split the set. `authoring example change-set`
prints a mixed set to start from, and `authoring example claim-type-succession`
prints a vocabulary evolution.

A `claim_type_succession` member succeeds an accepted ClaimType and settles its
whole reverse-pin closure in the same generation. Members lower in dependency
order -- definitions, then successions, then Claims, then retirements -- so a
Claim member after a succession is lowered under the successor vocabulary and is
never one of its dependents. A set cannot define a ClaimType and succeed it:
both members author the same artifact path, and the set refuses
`cruxible.authoring.change_set_member_path_collision`. The
`successor` is a whole ClaimType naming its predecessor by identity and pinning
that predecessor's exact digest; `dependents` is the exact closure computed over
the staged tree. Each dependent takes `successor` (carry it, re-pinned),
`retire` (a tombstone, with `claim_retirement_reason` `was-rescinded` for a
rescission, `was-wrong` for a statement that was false, or `superseded` for the
ordinary case of a statement that stood under a shape a later ruling replaced)
or `re_author` (a sibling Claim member of the same set, naming that
Claim again under the successor, named by `successor_claim_id`). The standalone
route's fourth, deprecated word `invalidation` parses here and refuses typed,
naming `cruxible claim-type migrate` as the road that still tolerates
it. A successor that changes `object_kind` refuses `successor`
for any live Claim dependent. `cruxible claim-type migrate` is the
operator form of the same law and builds its candidate with the same code.

There is no semantic member ceiling; how many
changed members one daemon will receive in a single submission is the operator's
`max_changed_members` bound in `daemon/proposal-receive.json`. Settling a change
set additionally requires it to fit under the advertised change-set record
ceiling described there, which preflight checks before lowering anything.

The authoring coordinator owns stable identities, timestamps, bases, and proposal
references. `compile` creates or updates an intent and performs a binding preflight;
`rebase` advances an unsubmitted refused intent to the current accepted coordinate;
`submit` is idempotent and never supplies approvals. `status` reports the remaining
approval or activation conditions without impersonating the actors who own them.
`bind --occurrence N` counts matching anchors in ascending byte-offset order and
selects the 1-based `N`th match. The resulting selector records the total number
observed while its start/end bytes name the selected occurrence; multiple matches
are therefore truthful input metadata, not an unresolved selection.
Use `authoring example claim-subject-relation` for a subject-valued Claim such as
`sec.vulnerability/<cve> → sec.vuln.affects_package → sec.package/<package>`.
Both endpoint Subjects must already be accepted and admitted by the ClaimType.
A projection block's marker grammar is a page-level shape rather than an
authoring one, and it is documented under `cruxible block`. Nothing composes it
for a caller any more: the marker must start in column zero, blocks cannot
overlap, nest, or repeat an id, and marker-looking text inside a Markdown fence
is not a declaration.

## query

~~~text
cruxible query [KIND] [--where 'f=v'|'f!=v'|'f<v'|'f<=v'|'f>v'|'f>=v'|'f in a,b'|'f exists'|'f !exists'|'f~text']...
    [--contains TEXT] [--select a,b] [--follow field:alias]... [--follow-in field:alias]...
    [--order-by f|-f] [--status live|overturned|refused|retired]... [--claims]
    [--limit N] [--cursor C]
    [--spec FILE | --name N --param k=v ... [--budgets JSON] [--receipt compact|full]]
    [--at GIT_OID] [--evaluation-time TS] [--json]
~~~

`query` has no subcommands: it answers any question over accepted state in one
call, the same read as MCP `cruxible_query` and SDK `cx.query`. KIND is
a Subject kind, `ClaimType` / `Procedure` for definitions, or `Trigger` / `Line`;
`--contains` alone searches every live Claim value across kinds. `query Trigger`
lists Triggers by `name`, `schedule` (`cadence`, `cron`, `capture_landing`,
`window_close`, `generation_accepted`), `target_kind` (`line` or `action`),
`target` and `lifecycle`; `--select` adds `cron`, `cadence`, `capture_contract`
and `version`. `query Line` lists Lines with their `procedure`, `authority`,
`lifecycle`, `enabled` (whether the Line's automation is admitting work), its
first 25 live `triggers` and `triggers_total` (a `triggers` filter or a
`--contains` search reads every live Trigger aimed at the Line, not only the
names shown). Both filter on those fields,
list only live rows unless a `lifecycle` filter is given, and page like any
compact query; an answer past 2000 rows is `capped` and says so. `--order-by`
sorts by each column's type (numbers as numbers), nulls last, ties by name. `--where` filters combine as
all-of; a field is a predicate's full name, its name after the `KIND.` prefix,
`subject_id`, or `alias.field` after `--follow`. `f!=v` also matches a Subject without the value.
`--follow field:alias` hops forward along one of KIND's Subject-valued predicates;
`--follow-in field:alias` hops backwards along another kind's predicate whose values
name KIND's Subjects (for example `query dev.roadmap_item --follow-in
dev.batch.delivers:batch` lists which batches deliver each item). Both repeat and
mix, in command-line order. A reverse field is the predicate's full name, or its
name after the pointing kind's prefix;
`orient --kind KIND` lists the predicates that point at KIND as `incoming`. Either
way there is one row per (Subject, followed Subject) pair, and without
`--order-by` rows sort by the queried Subject, then each follow alias.
Names and values are checked first: a wrong kind, field or enum member, or an
operator that does not apply, refuses with its code, the nearest valid names and
a repair. Text output is an aligned table of values and flags (`stale`,
`contested`, `contradicted`, `uncovered`, `unsure_hold`) followed by the next
command when the page is truncated; `--json` gives the full answer with its
receipt. Cells show each slot's answer as `get` shows it; `--status` adds Claims
resolution overturned or refused, or retired ones, and `--claims` names each
cell's Claims (ID, status, verdict, role) beneath the table. `--spec` runs a
`QueryDefinitionSpec` file inline; `--name` with `--param` runs an accepted
named query (`orient --section queries` lists them, `get query:NAME --detail
proof` reads one), `--budgets` sets its budgets up to the definition's maximum,
and `--receipt full` adds its replay receipt (`receipt.replay`: the Claims each
row read, traversal paths, bound parameters, verdict, and the
`playbill-query-execution-receipt-v1` whose digests replay it).

Author named queries through `cruxible authoring compile`, then submit the intent
and review/accept its proposal.
The SDK equivalent is `cx.query_definition(definition=QueryDefinitionInput(...)).prepare()`, followed
by the normal intent submission and approval flow. `cx.changes().query_definition(...)`
includes a query in a changeset. Omitted ClaimType pins resolve against the intent
base or sibling definitions; explicit pins remain assertions. SDK `vocabulary=`
accepts World ClaimType references and retains their stale-reference checks.
Use `cx.query(name=NAME, receipt="full")` to read the accepted result and receipt.

`authoring example query-claims-by-type` provides a Claim query without
placeholder digests. `authoring example query-ontology` and `authoring example query-procedures`
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

## procedure

~~~text
cruxible procedure run NAME INPUT_FILE --evaluation-time TS
cruxible procedure measure NAME [--run-id RUN_ID] [--measurement NAME]...
  [--evaluation-time TS] [--at FILE] [--json]
cruxible procedure readings NAME [--run-id RUN_ID] [--measurement NAME]... [--subject-grain G]
  [--evaluation-time TS] [--at FILE]
  [--limit N] [--cursor C] [--json]
~~~

Read a Procedure with `cruxible get Procedure:NAME`: its card says how it runs
(`runnable`): `direct` (`procedure run`), `line` (only as a Line), or
`unsupported` (no run path admits it, e.g. an `exhaust_tap` node), with the
nodes behind that answer. Procedures are graph format 6 and pin every Provider
exactly; open slots belong only to a Blueprint. The direct lane runs `state_tap`,
`state_claim`, `transform`, `project`, `guard`, `repeat`, `select`, `constant`,
`return`, `invoke`, `halt`, `source` and `call` nodes. The effectful terminals --
`emit_capture`, `post_inbox`, `propose_change_set`, `settle_change_set` -- run
only as a Line: a direct invocation carries no requested authority, no
occurrence, and no mandate coordinate, and none is fabricated for it.

A **Blueprint** is the same definition with interface-typed Provider slots left
open (`cruxible authoring example blueprint`); it is accepted but never runs.
`cruxible get Blueprint:NAME` lists each slot's interface and the installed
Providers that fit it. Instantiate it with one Provider per slot
(`cruxible authoring example blueprint-instance`, kind `blueprint_instance`):
the result is an ordinary Procedure that records its Blueprint and bindings.
The Line lane serves `propose_change_set`, and `settle_change_set` under a live
settle ProcedureMandate; see `cruxible line`.

A Source run needs accepted state to authorize it: a live
SourceAcquisitionPolicy, the CaptureContract each Source node pins, and the
Provider closure it names. A Procedure names its policy on its own envelope,
under the pin role `acquisition-policy` -- authored by naming the policy, the
way a Line names its own -- and a pinned Procedure reads only that policy, so
what anyone accepts afterwards cannot change what it does. A pinned policy
must cover the Procedure: a rule for every Source alias, extra rules allowed,
so one policy can serve several Procedures. A Procedure with no such pin falls
back to accepted state: exactly one live SourceAcquisitionPolicy whose declared
inputs are exactly the Procedure's Source aliases. Both lanes refuse
`source_acquisition_policy_required` when the pinned policy (the Procedure's
on a direct run, the Line's on a Line run) has no rule for a Source alias,
naming it in `uncovered_input_names`; the direct lane also refuses it when no
single policy applies to an unpinned Procedure. `source_acquisition_refused`
is the refusal when the policy's own rule denies a declared input; none of these
leaves run history behind. A read outside an
authorized workspace root, over the CaptureContract's selection budget, or with
no daemon-local reader refuses `workspace_file_read_refused` and names its path
class.

Source acquisition currently serves independent coherence. Bounded-window and
declared-snapshot-group policies refuse before provider invocation. Actual
captures are checked against the pinned replayability and maximum-age rules
before selection, including the rule’s omission/default/refusal behavior.

A completed Source run reports, per occurrence, the `SourceReadReceipt` the
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
$ cruxible procedure run release-guard input.json --evaluation-time 2026-09-06T10:00:00Z
RUN-3f…: succeeded
$ cruxible procedure measure release-guard --run-id RUN-3f…
rollout-healthy: pending reading=no_resolution
  the measurement window has not opened
$ cruxible procedure measure release-guard --run-id RUN-3f… \
    --evaluation-time 2026-09-06T11:00:00Z
rollout-healthy: resolved (satisfied, RSR-9a…) reading=recorded PRD-c1…
$ cruxible procedure measure release-guard --run-id RUN-3f… \
    --evaluation-time 2026-09-06T11:05:00Z
rollout-healthy: resolved (satisfied, RSR-9a…) reading=replayed PRD-c1…
$ cruxible procedure readings release-guard --run-id RUN-3f…
rollout-healthy: resolved readings=1 (satisfied, RSR-9a…)
PRD-c1… rollout-healthy procedure_unit satisfied run=RUN-3f…
~~~

## line

~~~text
cruxible line enable LINE [--dry-run|--commit] [--at OID] [--json]
cruxible line disable LINE [--dry-run|--commit] [--at OID] [--json]
cruxible line run LINE [--event FILE|-] [--repeat] [--resolution-contract FILE|-]
  [--occurrence-id ID] [--evaluation-time TS] [--json]
cruxible line evaluate LINE [--since TS --until TS] [--dry-run] [--limit 100] [--cursor CURSOR] [--json]
cruxible line dispatch LINE [--occurrence-id DIGEST] [--retry] [--limit N] [--json]
~~~

`enable`, `disable` and `run` are the normal flow; `evaluate` and `dispatch`
recover what automation missed, and `cruxible next` points to them. `cruxible
get Line:NAME` reads a Line: its Triggers, its enablements (state, who enabled
it, the pinned Line version and Trigger versions, how far its daemon has
matched), its pending occurrences and its recent runs.

A Line is authored like any other definition: a `line` input (alone or as a
change-set member) names its Procedure and `parameters` -- the Procedure's
input record, which lowering checks against the Procedure's input contract,
refusing `cruxible.authoring.line_parameters_refused` with the expected
fields. It names an `acquisition_policy` (an `acquisition_policy` input) only
when the Procedure has Source nodes. A Procedure with an `exhaust_tap` node
cannot be a Line's (`cruxible.authoring.line_exhaust_tap_unsupported`): no run
path admits one. `authoring example line` prints a Line over the `authoring
example procedure` Procedure, and `authoring example acquisition-policy` a
policy for a Source Procedure's Line.

When a Line runs on its own is not the Line's own: a `trigger` input authors a
Trigger (`triggers/<name>.json`) whose `schedule` is a `cadence`, a `cron`,
`generation_accepted`, `capture_landing` on one exact CaptureContract, or a
`window_close`, and whose
target is one Line (`line_name`) or one registered internal action (`action`,
which takes a `cadence`, `cron`, or `generation_accepted` schedule).
A `cron` schedule is a standard five-field expression (`minute hour
day-of-month month day-of-week`; numbers, `*`, ranges, steps and lists, with
day-of-week 0-7 and Sunday both 0 and 7; no names or `@` macros) evaluated in
UTC, always: a schedule names no timezone, so an instant never depends on a
host's timezone database. Convert local times first (09:00 New York in winter
is 14:00 UTC); a schedule that supplies a `timezone` is refused with that
reason. The Trigger law refuses an expression outside this grammar
(`cruxible.trigger.cron_invalid`). A Line can have several Triggers. **A
Trigger aimed at a Line does nothing until the Line is enabled**: `get` shows
`triggers_inactive: not enabled` on the Line card and on its `orient --section
lines` row until then. Internal-action Triggers need no enablement. Retiring
a Line with live Triggers aimed at it refuses unless they are retired or
retargeted in the same change set. `authoring example trigger` prints
an hourly cron Trigger for the `authoring example line` Line, and `get Trigger:NAME` reads
one. A Trigger never fires retroactively: it matches only Captures recorded
strictly after its version was accepted and fixed windows that close strictly
after it, and admission refuses an earlier one supplied or queued anyway
(`trigger_event_precedes_acceptance`).

`enable` makes the daemon match the Line's Triggers forward from now and admit
what they match, with no explicit call. Enabling needs governed write, even for
an observe-only Line, and keeps only the credential's identifier, never a
token. Runs use the enabling caller's credential, which the daemon rechecks
before every admission: a revoked credential, one moved to another instance, or
one no longer permitted to dispatch stops the enablement with that reason
(`credential_revoked`, `credential_scope_changed`, `permission_insufficient`,
`credential_unbound`). The accepted standing of the principal the enablement
acts as is rechecked too: a credential's bound principal, or on an auth-off
daemon the principal the enabling request claimed, that is no longer active
stops it (`principal_inactive`) and revokes that principal's credentials; an
enablement made with no principal claimed runs as the implicit local operator.
A Line that can propose or settle needs a current ProcedureMandate covering its
Procedure, and refuses to enable while none does. Enablement is per Line, not
per Trigger: it is pinned to the Line version and the exact Trigger versions
aimed at the Line when it was enabled, so any accepted change to the Line stops
it (`line_changed`, or `epoch_changed`), adding, changing or retiring a Trigger
aimed at it stops it (`trigger_changed`), and retiring the Line stops it
(`line_retired`) and closes its pending occurrences, until it is enabled again.
Because a settle mandate, not the caller's tier, authorizes settling, an
enabled Line whose Procedure settles does so on its own under its mandate.

An enablement never catches up. It admits only what it matched itself since it
was enabled or since the daemon last restarted; anything pending before that,
or recorded by `evaluate`, waits for explicit `dispatch`. A cadence or cron
tick is the exception: it is not an event but "the Trigger is due", so when a
Line is enabled or its enablement resumes, a tick still pending from before
closes as `lapsed` -- retained, never run implicitly, and still runnable as
exactly that tick with `dispatch --occurrence-id DIGEST --retry`, even after
newer ticks ran -- and the enablement ticks on from its own start rather than
catching up on ticks it missed. Each cadence or cron Trigger keeps its own
chain: it is due one interval, or at the next calendar instant, after the last
occurrence it fired, whatever other Triggers aimed at the Line fired, and never
before the first instant after its Trigger version's acceptance: a new cadence
ticks first one interval after it was accepted, a successor schedule from its
own acceptance. `disable` stops further admissions; a run already admitted
keeps going, and a retired Line can be disabled too. Both are idempotent:
enabling a Line already enabled by the same credential at the same versions
returns it unchanged with `outcome: already_enabled`, and disabling a stopped
enablement returns it with `outcome: already_disabled`. Enabling under a
different credential or after the Line changed rebinds it from now (`outcome:
reenabled`). `get Line:NAME` shows each enablement's state, how many pending
occurrences it will admit on its own (`pending_automatic`) and how many await
explicit dispatch (`pending_explicit`), and why it stopped; another
principal's enabling credential is withheld. Idle coverage is checkpointed at
one-minute intervals; event progress and partial scans are retained
immediately. Each enabled Line is drained by at most one worker at a time, so a
slow Procedure never delays matching or another Line. An enabled Line is one
kind of daemon consumer. `cruxible next` reports `consumer_stalled` (with
`detail.kind: line`) for an enablement that stopped by itself (its repair
enables it again) or an enabled Line whose own due work has waited more than 15
minutes (its repair dispatches it, which shows the refusal). A deliberate
disable is not reported.

A restart keeps each enablement and opens a new forward range from the
restart: the downtime is not matched, what the previous range matched but did
not admit stays pending, and timed ticks lapse. For each enabled Line `next`
then shows one `line_coverage_gap` row per range its daemon never matched,
naming the exact `cruxible line evaluate LINE --since S --until U` that covers
it (the row leaves once an evaluation covers the range), and one
`line_work_pending` row while work it matched before the restart, or that was
evaluated explicitly, waits for `line dispatch`. Nothing evaluated or matched
before a restart is dispatched automatically. Rebuilding the disposable event
index similarly opens a new forward range, while retained pending work
survives. Pending work bound to an older Line version is closed as superseded
rather than silently rebound.

`evaluate` checks a historical `[since, until)` range against every live
Trigger aimed at the Line and records its matches as pending; it never runs
anything. `--dry-run` only reports what the range makes eligible -- `met`,
`not_met`, or `incomplete`, the exact matching events/windows (each naming its
Trigger), and each occurrence's dispatch status (pending, admitted, rejected,
superseded or lapsed) -- enqueues nothing, needs no range, and is a read.
Without `--dry-run`, `--since` and `--until` are required and it needs governed
write. Follow its cursor to finish a bounded page.
`dispatch` admits pending occurrences using the caller's current permissions
and the ordinary Line admission checks; by default it drains every pending
occurrence (`--limit N` stops after N). `run` and `dispatch` of a Line whose
runs can propose or settle (and `procedure run` of such a Procedure) need
governed write; an observe-only Line or Procedure runs at read-only. Permanent
input failures close as `rejected`; changed Line bindings, and occurrences
whose Trigger changed or no longer aims at the Line, close as `superseded`.
Both leave the runnable queue, retaining their evidence and a typed refusal
with repair instructions. Invalid event bindings, unavailable event material,
and Captures that exceed their fixed read budget close as rejected. Budget
refusals name the limiting Line or CaptureContract cap. Accepting a successor
Line can raise its own limit; it cannot override the exact CaptureContract's
cap. Transient authority/provider failures and events whose recorded time has
not arrived remain blocked. Historical evaluation does not reopen closed work.

`dispatch --occurrence-id DIGEST --retry` explicitly retries one occurrence,
binding the current accepted Line version only within the same occurrence epoch
and only while its Trigger still aims at the Line unchanged.
It preserves the exact event/window and rechecks present authority and freshness;
it cannot substitute a newer Capture. An existing admission is always reused.

A Line can bind its trigger Capture to a named Source input
(`trigger_input`); it then declares the exact event it accepts, and every
Trigger aimed at it must fire on that event. Its `max_age` is checked at
admission time, not backdated to when the trigger occurred.

`run` runs the Line once now: one manual occurrence under the Line's own
inputs, budgets, authority ceiling and mandate, recorded on the Line's history.
It never selects, consumes or waits on a Trigger, and runs whether or not the
Line is enabled; a Trigger's ticks and events are unaffected by it. A Line
whose Procedure takes an event (`trigger_input`) gets it through `--event`, a
retained Capture event reference (JSON/YAML, or `-` for stdin); the manual
occurrence's identity names that event. A run on an event the Line's Triggers
already admitted refuses (`occurrence_already_admitted`, naming the run) unless
`--repeat` says to run it again. The occurrence's evaluation instant is the
daemon's; `--evaluation-time` only asserts the instant the caller believes it
is running at, and an assertion outside the daemon's skew bound is refused.
That bound is operational, not wire: the daemon reads
`evaluation_instant_skew_seconds` from `daemon/procedure-runs.json` in its own
state root, defaulting to the 300-second ProcedureMandate skew the bound
protects, and refuses the run if that file exists but cannot be read as one.
A Line whose runs can propose or settle needs a current accepted
ProcedureMandate over its exact Procedure; without one each run refuses
`line_mandate_required`, and `enable` refuses up front with
`cruxible.line.mandate_required`, both naming `authoring example
procedure-mandate`. An observe-only Line -- one whose Procedure's terminals, or
whose `max_authority`, stop at observe -- needs no mandate. A mandate whose
`resource_ceiling` exceeds the Procedure's hard caps is refused naming each
widened cap with both values; `authoring example procedure-mandate` uses the
`authoring example procedure` caps.

A Line whose Procedure ends in a `propose_change_set` terminal produces a
proposal. Its `candidate_templates` are a fixed list of templates, or
`{"items": "$steps.<alias>.<list>"}` to fan out over data: each element a
provider, Source or transform produced becomes one item of the one proposal,
with its own dependency closure and evidence. An empty list proposes nothing
and the run succeeds; an element that is not a Claim proposal item refuses
`proposal_item_invalid` naming its `child_index`, and `items` that does not
resolve or is not a list refuses `proposal_item_invalid` with reason
`items_unresolved` or `items_not_a_list` (an object is never unwrapped). Each
element cites the Capture its own lineage reached; a list built from several
Captures by a step that keeps no per-element lineage refuses
`proposal_item_evidence_ambiguous` rather than giving every element all of
them. Each resolved candidate
template must be one Claim proposal item --
a statement, a rationale, and optionally the Claim lineage it revises. The
daemon supplies the evidence: the produced Capture in that item's own
dependency closure is cited -- or, when the closure produced none, the one
Capture the run was admitted (a Line's `trigger_input` Capture); none is
`proposal_item_evidence_missing` and more than one
`proposal_item_evidence_ambiguous`, and an item that names its Capture must
name one in its own closure, produced or admitted -- so a computed interpretation is a Claim under its
ClaimType's evidence admission policy, never an attested observation and never
a self-asserted one. The items are lowered through the same change-set
authoring every surface uses, the exact live ProcedureMandate is evaluated
against the paths lowering actually changed, and the proposal door is called
once. The run reports, per terminal, the proposal id, the exact candidate
digest, the operation key, the mandate bound, and the Claim path each item
lowered into; `--json` carries them in `terminal_egress`. Producing the
proposal activates nothing: retrieve it with `cruxible get PROPOSAL_ID`, review
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

A settle terminal whose run's authority reaches propose but not settle -- a
Line whose `max_authority` is `propose`, or a Procedure whose only mandate
grants propose -- proposes instead: the same fallback a failing condition
takes, reported `settle_outcome: proposed` with `fallback_reason`
`cruxible.settle.authority_capped_by_<term>` naming the term that capped it
(`line_max_authority`, `mandate_grant`, `propagated_sensitivity`). It binds the
mandate a proposal would and never consults a settle grant, so it settles
nothing. A Line graduates from proposing to settling with a Line successor
that raises `max_authority` to `settle`, plus a covering settle mandate, over
the same Procedure: one Procedure serves both stages, so its digest -- the key
its track record is folded under -- does not change at graduation.

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
refuses `terminal_authority_capped_by_<term>`. The one exception is a settle
terminal capped at propose, which proposes as described above; capped below
propose, it is refused like any other.

Known limitation: a settle run submits its delegated proposal against the
accepted head and then activates it. If another generation is accepted between
the two, activation refuses `settle_publication_refused`, and that proposal
stays open but can never be activated, because its candidate no longer
reproduces against the new head. Retrying the same operation recovers the
same proposal and refuses the same way. Withdraw it with `cruxible proposal
withdraw`; the Line's next due occurrence settles against the current head
under a new operation key.

## prediction

~~~text
cruxible prediction propose REQUEST_FILE [--json]
cruxible prediction settle PREDICTION_ID --observation CLAIM_ID [--json]
cruxible prediction settle PREDICTION_ID --request REQUEST_FILE [--json]
cruxible prediction list CLAIM [--json]
~~~

A prediction is stored as a resolution contract: `get ResolutionContract:NAME`
reads one, with its bound windows and their state.

Every Claim version these commands need is named by Claim ID (`CLM-...` or
`Claim:CLM-...`); the daemon resolves its artifact and statement digests and the
coordinate that accepted it. The exact `ClaimVersionReference` object is still
accepted, as the advanced form, anywhere a Claim ID is.

`prediction propose` submits a governed ResolutionContract whose `hypothesis` is
an already accepted Claim (by ID) and returns the proposal ID and authoring
intent. The contract pins the exact version the ID resolved to, and must be
accepted before it can bind an investigation or settlement. `prediction list
CLAIM` returns the accepted contracts testing that exact Claim version (retired
ones included) and says so when there are none; contracts testing an earlier
version of the Claim are not listed, and pending predictions and settlement
outcomes are read with `get` and `next`.

`prediction settle` names the prediction by its contract name, or one of its bound windows
by its bound contract ID (`RSC-...`), and the settling observation by Claim ID.
The daemon resolves the exact live contract reference and, for a bound window,
its anchor event; a window the worker does not hold, or whose contract version
is no longer live at the accepted head, is refused with
`prediction_window_unknown`. It checks the observation against the contract's
selector, mechanical rule, and bound window. `--request` takes the advanced
request: an exact contract reference, an explicit anchor event, or terminal
evidence. Terminal-backed settlement additionally requires one delivered
`settle_change_set` receipt from the same investigation whose outcome is
`settled`; one that fell back to a proposal does not qualify. It records
the activation and resolution in operational exhaust; it does not create or
mutate Claims. A failed attempt or an unevaluable
observation does not settle the hypothesis as false. Effectful terminal nodes
remain disabled in the public Procedure runner. The `prediction_settleable` row
in `cruxible next` renders `cruxible prediction settle RSC-...`; add
`--observation CLAIM_ID`.

A window that closed with no accepted observation inside it cannot settle:
`prediction settle` refuses with `prediction_deadline_passed`, and the
`prediction_settleable` row stays until the contract is retired. Cruxible does
not assign a meaning to a window that closed unobserved (a lapse, or a
resolution where the contract declares absence decisive); retire the contract
to clear the row.

## block

~~~text
cruxible block repin SOURCE_ID BLOCK_ID [--claim ID]... [--query ID]...
  [--backing SHA256] [--params CANONICAL_JSON]... [--workspace-root DIR]
  [--evaluation-time TS] [--artifact ID]... [--currency-policy warn|require_current]
  [--clear-claims] [--clear-queries] [--clear-artifacts] [--render] [--dry-run]
cruxible block sync [PATH]... [--all] [--workspace-root DIR] [--json]
cruxible block detach PATH... [--workspace-root DIR] [--dry-run|--commit] [--at DIGEST] [--json]
cruxible block depublish SOURCE_ID BLOCK_ID [--dry-run|--commit] [--at OID] [--json]
~~~

Blocks are authored and stamped; nothing regenerates them. The workflow: write
the markers and the prose (or `repin --render` for a rendered table or list),
`repin` to stamp, let `next` or `block sync` report a block stale or dirty,
re-check it and repin; `block detach` and `block depublish` when its backings
are gone. The client, not the daemon, computes every stamp: `repin` reads the
backings, writes the marker and then declares the block to the instance.
`--render` writes the body from the block's one `--query` backing: a Markdown
table of the result Subjects and their projected fields, or a bulleted list of
Subjects when the query projects none, and `_No rows._` when it returns none.

### The two roads a governed passage takes

A page is a **source**. Its bytes are captured, its capture is evidence, and a
passage of it can be cited like any other evidence. What a page holds is one of
exactly two kinds of governed block, and they never overlap.

A **source block** is ordinary prose the author wrote, made governed by a Claim
that cites its span. There is no marker: write the passage, capture the page
through `cruxible sources compile` / `propose`, and author the Claim citing the
span it states -- `copied_from` when the passage states the value verbatim,
`supported_by` when it rests on the passage as evidence. This is the road for
"the page says this, and here is the Claim that stands behind it".

A **projection block** is agent-authored prose HELD TO an explicit list of
accepted Claims and artifacts, marked in the file by a marker pair and declared
with `block repin`. **Nothing renders it.** The body is git-tracked text the
agent writes; what the marker commits to is which accepted state the passage is
accountable for, and `cruxible next` and `block sync` prove that state has not
moved under it. This is the road for "this table reflects these Claims".

Evidence never comes from a projection window. A citation of any role and any
origin whose span lies inside a stamped block refuses --
`cruxible.projection.evidence_from_projection`, at the daemon, not only in the
SDK -- because a block that was both kinds would let a page attest itself into
concrete. Prose outside every window is the author's own and stays citable.

### Declaring a projection block

`block repin --claim ID --claim ID ...` is how a projection block is created.
Write the marker pair by hand around the prose you want governed (see
[Projection block markers](#projection-block-markers)), then repin it naming
every backing: the client re-reads and re-proves each Claim at the
accepted coordinate, computes the stamp, writes the marker, and registers the
block with the instance. Up to 512 backings fit in one block
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
a blocking `cruxible next` finding. Invalid markers and integrity failures always
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
projection -- the overlap the two-block-kinds law refuses; a Claim payload has
no field for it.

On MCP the same adapter runs in the MCP server process:
`cruxible_block_repin` takes the block and its page (`file`,
workspace-relative, or `source`, its catalog id) and computes the stamp there,
so an agent never builds one (`render: true` is `--render`);
`cruxible_block_sync` is `block sync`, a read that edits no page. Detaching is
`block detach` and the write-tier `cruxible_block_detach` (`files`, `dry_run`,
`at`): its preview
reports what the edit would change and is pinned to the pages' bytes, and a
commit with `at` refuses if a page changed since. `--dry-run` (MCP `dry_run`)
on a repin computes and checks the stamp and writes nothing: no manifest, no
page edit, no declaration.

### Projection block markers

A projection block is the byte range between one opening and one closing
marker, each on its own line:

~~~text
<!-- cruxible:block:BLOCK_ID -->
...the governed prose...
<!-- /cruxible:block:BLOCK_ID -->
~~~

- `BLOCK_ID` matches `[a-z][a-z0-9_.-]{0,63}` and is unique within its page.
- The opening above is the **bootstrap** form you write by hand. `repin`
  replaces it with a stamped opening, by default the compact form
  `<!-- cruxible:block:BLOCK_ID:ref:HEX12 -->`, where `HEX12` is the first 12 hex
  of the stamp's digest and the stamp itself is kept in
  `.cruxible/manifests/`; a block repinned without compaction carries the stamp
  inline as `<!-- cruxible:block:BLOCK_ID:STAMP -->` (unpadded base64url of the
  canonical stamp JSON). Never edit a stamped opening by hand; repin it.
- Each marker starts at column 0 and ends with a line feed (LF, not CRLF), and
  the body ends with a line feed.
- Markers inside a fenced code block (``` or ~~~) are text, not markers.
- Blocks never nest or overlap, and every opening has its closing marker.
- The page must be a source in `.cruxible/sources.yaml` (or `sources.yaml`);
  that catalog names the block's `source_id`, which a bootstrap marker cannot.

### Checking and detaching

`block sync` and `cruxible next` use the same currency evaluator. A sync checks
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

`block sync` writes nothing. Advisory findings remain visible without a nonzero
exit; `require_current` findings and integrity errors fail the check. An unreadable or ambiguous lineage remains an incomplete check, with
exact successor candidates where available. `repin --backing DIGEST` selects a
live successor explicitly.

`block detach PATH...` removes markers from a retired block or a declaration
belonging to a different instance, preserving its prose and all bytes outside
the block; live blocks are refused. It uses a whole-file compare-and-swap and
does not rewrite or approve prose. `--dry-run` reports what would change and is
pinned to the pages' bytes; `--commit --at DIGEST` refuses if a page changed
since that preview.

### Depublishing

`block depublish SOURCE_ID BLOCK_ID` releases the registration that demands a
block's frame, whichever road declared it. Every projection block is registered
with the instance -- a `block repin` records a declaration, and an instance that
published under the retired road folds its bound publications -- and `cruxible
next` reports a registered block whose marker is no longer in the file as
blocking, correctly, until the block is meant to be gone. Depublishing is what
says so, and the blocking row names this verb.

The registration is protocol state and says nothing about what a block CONTAINS:
it records that this instance stands behind this marker. It is also the identity
`workspace detach` refuses on, so a worktree cannot move out from under markers
a host still owns.

It edits no page and retires no Claim. Strip the markers (`block detach`,
or by hand), retire the backing Claim through the ordinary retirement road if
the statement is also being withdrawn, and depublish when the block itself is
not coming back. A registration whose backing Claim is already retired no longer
demands its frame, so a retirement alone clears the row without this verb.

**Depublishing is one of two steps: the marker still has to leave the page.**
The verb touches the registration and nothing in the workspace. There is no
`--strip` -- removing bytes from a file is the operator's explicit act, not a
side effect of a ledger release -- so between the two steps `cruxible next`
reports the marker as `unregistered_projection_block` with the repair
`remove_or_register_projection_block`. That is a warning rather than a blocking
row, and it is the opposite instruction to the row it replaces, which asked for
the frame to be restored. Remove the marker pair with `block detach PATH`
or by hand and it clears.

## next

~~~text
cruxible next [--evaluation-time TS] [--access-profile FILE]
  [--expiring-within P7D] [--workspace-root DIR] [--delta DIGEST]
  [--limit N] [--cursor CURSOR] [--brief | --json]
~~~

Returns the deterministic repair queue at one accepted coordinate. The client
stamps the current UTC evaluation time when `--evaluation-time` is omitted,
parses `--expiring-within` ISO-8601 durations client-side without changing the
integer-microsecond daemon wire, and observes its configured floor locally. If
`.cruxible/sources.yaml` or root-level `sources.yaml` exists, the client also
observes readable source-file digests, including paths explicitly authorized by
a local overlay. Unreadable or unresolved sources are omitted individually; the
daemon compares observed sources with accepted whole-source snapshots and names
drifted or unobserved cited sources. The daemon reads no clock or workspace.
Without actual source or drift observations, `workspace_sources` remains explicitly
unobserved. Procedure-catalog coverage is accounted for separately as
`workspace_projections` and cannot imply that workspace sources were scanned.
An entry with `kind: procedure`, a `Procedure` identity, and a workspace-relative
`locator` declares projection intent for that accepted Procedure. Where the
workspace turns on the Procedure projection advisory (off by default), a
complete, coordinate-bound catalog observation reports every live Procedure
without such an entry in the `procedure_catalog` status facet; the repair
carries their exact hand-edit entry shapes.

The result's `status` reports the environment the queue was read in, beside the
work rather than as rows: `instance` (active or decommissioned; decommissioned
sets `blocking`), `floor`, `ledger_mirror`, `provider_lane`,
`procedure_catalog`, `compiler`, and `line_dispatch`. Each facet carries a
`state`, and a `repair` while it needs attention. The CLI prints facets that
need attention before the rows.

`compiler` compares the accepted head's compiler with the one the daemon runs.
`upgrade_available` means an explicit forward edge exists, and its repair is
`cruxible compiler upgrade --to DIGEST --name NAME`, which only
proposes the upgrade: an admin still approves and activates it.
`no_upgrade_path` means the accepted compiler has no edge to the running one,
for example a daemon older than the state it serves; nothing is proposed.

`line_dispatch` counts the Line occurrences that `line evaluate` or a listening
daemon queued for dispatch and nothing has admitted yet, per Line. It is
`waiting` while every queued occurrence's window is still open and `due` once
one could be admitted at the evaluation time; the repair is
`cruxible line dispatch LINE_DIGEST [--limit N]` for the Line with the
oldest due occurrence. Nothing dispatches implicitly. A caller whose access
profile excludes instance material reads `not_observed`.

These rows come from the daemon's consumers rather than from a computation at
read time. Findings are what a worker last observed, so each row reflects its
last check:
- `evidence_unavailable` names a Capture the next worker found missing or
  corrupt, with the live Claims that cite it. Restore its bytes, or recapture
  and re-cite; the worker's next check clears the row.
- `prediction_settleable` names a ResolutionContract with a closed bound window
  whose resolution journal holds no current answer, with its hypothesis Claim.
  `detail` carries the window, its `anchor_event` (null for a fixed window), the
  `bound_contract_id`. An event window has one row per
  anchor. The repair is `cruxible prediction settle RSC-...`; add
  `--observation CLAIM_ID` naming an accepted observation inside the window. The worker does not check that such an observation exists; if
  none does, see the note under `cruxible prediction settle`.
  The worker clears the row when the settlement lands, and restores it if that
  answer is overturned.
- `prediction_window_unbindable` names a ResolutionContract with a matching
  anchor Capture whose retained material no longer binds a window, with the
  refusal `code`. Restore the material, and the worker binds the window on its
  next retry trigger event or new capture landing; or retire the contract.
- `consumer_stalled` names a consumer that stopped by itself or stopped
  keeping up, with its kind in `detail.kind` and the kind's own repair.

Because those rows are what a worker last observed, `status.consumers` says how
current that observation is: `current`, `lagging` (a worker is behind on
generations or has not finished earlier sweep/retry work before another fire;
this facet asks for
attention), `stalled` (already a `consumer_stalled` row), or `not_running` when
no consumer loop is running, as while a daemon shuts down. There, worker rows stand as
of each worker's last pass. `detail.workers` lists each built-in worker's state
and cursor, including disabled ones, and `detail.line_enablements` counts the
instance's enabled Lines as `running`, `stalled` or `stopped` (the text header
prints `Status: line enablements ...` when any is stalled or stopped). The `next` entry
keeps each part's figures under `queue`, `evidence` and `prediction`. Evidence
rows carry when they were observed in their own detail; prediction rows omit
observation timestamps so their identity stays stable while the finding is
unchanged.

A current `unsure` examined attestation holds a row, and `status.held` counts
the rows held. A hold lasts only while its basis is unchanged:

| Row | Held while |
|---|---|
| `claim_conflicted` | every contender carries a hold made by someone whose accepted coordinate already had every contender's current version |
| `claim_contradicting_evidence_available`, `claim_new_evidence_unreviewed` | the hold was attested after that evidence |
| `claim_dependency_stale` | the hold's coordinate already had each upstream Claim's current version |
| `claim_stale_evidence`, `claim_uncovered` | the hold was attested after the last expiry, until its `valid_until`, else the ClaimType's `unsure_hold_for`, else 30 days |

A revised Claim, a later support or contradict from the same principal, or a
lapsed validity window ends the hold, and the row returns.

Every caller sees every row. Each repair needs the permission tier of the tool
that performs it (approval needs graph write; settle, line enable, line evaluate and
authoring need governed write; a Line dispatch needs what the Line's runs need). When the
caller cannot perform a row's repair, or a nested finding's, the row stays: its
`repair` is withheld (`null`) and `repair_requires` names the `tool`, the `tier`
it runs at, and `because` (`tier`, or `profile` when an MCP session's tool
profile does not advertise it; `profile: "full"` does; or `authoring` when the
caller cannot author on the instance at all, with `authoring_refusal` carrying
the code, detail and repair `whoami` reports). The text output prints
`repair withheld: <tool> needs the <tier> tier`, led by the identity repair
when authoring gates it. Nothing is left out. A status facet (the compiler, floor, ledger mirror and
so on) always reports its state; when its repair is one the caller cannot
perform, the repair is dropped and the facet carries `repair_hidden: true` and
`repair_requires` instead. Each repair's `command`
renders for the caller's surface: a CLI command here, an MCP tool call on
`cruxible_next`.
Empty `items` means only that no work exists in the explicitly observed domains.

Rows are typed: each carries `severity`, `reason`, `subject_identity`,
`related_identities`, `detail`, a `repair` (with a runnable `command` when the
operation's arguments name every operand), and any further `findings` about the
same underlying fact. `--delta DIGEST` returns only the rows added or removed
since that earlier queue, when the daemon still remembers it; otherwise it
returns the whole queue.

Results are paged. `--limit` sets the rows per page (default 100, at most 1000),
and `total_items` counts every row of the answer, so page one already gives the
queue's size (or, on a delta, the number of changed rows). While more rows
remain, `next_cursor` is set and the CLI prints `Next: --cursor CURSOR`. A
cursor carries the evaluation time, coordinate, attestation head and delta base
of the page that minted it, so later pages read the same queue even as the
clock moves. `result_digest` names the whole queue on every page. If the queue
has moved since the cursor's first page, or a delta's base has been forgotten,
the request is refused with `cruxible.next.cursor_mismatch`. The repair is to
run `cruxible next` again without a cursor.

`--brief` prints one line per row: severity, reason, subject, and the repair
command if there is one. It also prints the status lines that need attention
and the next-page cursor, and leaves out repair details and findings.
Conflicting values in the same claim slot require revisions into distinct
qualifiers; when a shared value field such as `topic` separates the contenders,
the repair identifies that field.

A `proposal_stale` row is exactly a `proposal list` entry whose terminal reason
is `stale`: a candidate neither accepted, refused nor withdrawn whose parent is
no longer the coordinate's semantic root, so it cannot activate. The row names
the proposal's author in `detail.actor_id`; its repair is
`cruxible proposal readmit PROPOSAL_ID`, which only that author may
run, so only the author's queue shows the row (the principal `whoami` reports;
a read with no principal shows none). `proposal withdraw` is the alternative
when the change is no longer wanted. A readmission at the same coordinate, or a
withdrawal, closes the row; so does a readmission that still carries the
change: an accepted one supersedes the source for good, and a live one that went
stale shows as its own row instead.
A proposal a settle terminal made carries `detail.settle_submission` (`mode`
and `mandate_digest`). A `delegated` settle that went stale is automation that
did not finish: readmitting it re-evaluates it as an ordinary proposal that
needs approval, because the mandate authorized the submission it was, not a
rebased one, so its repair says so.

A `proposal_awaiting_approval` row is an open candidate whose parent is the
coordinate's semantic root and whose approval requirement is not yet met, and
which you could approve. "You" is the daemon's authenticated caller: the
principal `whoami` reports. It must be active and `ordinary` in the accepted
registry, must not be the candidate's author, and must not have approved it
already. The caller is never a request field, so a queue read by an unattributed
request, or by a caller who is not a registered principal (an auth-off daemon's local
`operator`, say), has no such rows. The repair is
`cruxible proposal approve PROPOSAL_ID --signer-id PRINCIPAL`. Add
`--key` with the path to your signing key: it stays in your own custody and the
daemon never learns where. Your approval closes the row. So does another
signer's approval that meets the requirement first, because the candidate then
needs activation, not more approval.

A `mandate_expiring` row is a live, unsuspended ProcedureMandate whose
`expires_at` falls after the evaluation time and within `--expiring-within`.
Nothing renews a mandate, so its repair starts a successor from
`cruxible authoring example procedure-mandate`; a successor
whose window reaches past the lead time, or the mandate's retirement, closes
the row.

## curation

~~~text
cruxible curation list [--access-profile FILE] [--limit N] [--cursor CURSOR] [--json]
cruxible curation observe [--workspace-root PATH] [--dry-run|--commit] [--at DIGEST] [--json]
cruxible curation overrule ITEM_ID
  --expected-latest-event-digest DIGEST --reason TEXT [--attribution-ref REF]...
  [--dry-run|--commit] [--at OID] [--json]
cruxible curation accept-fixed ITEM_ID
  --expected-latest-event-digest DIGEST --reason TEXT
  (--proposal-id DIGEST [--changeset-digest DIGEST] | --generation N)
  [--attribution-ref REF]... [--dry-run|--commit] [--at OID] [--json]
cruxible curation suppress ITEM_ID
  --expected-latest-event-digest DIGEST --reason TEXT
  --scope item|lineage [--until-generation N] [--attribution-ref REF]... [--json]
cruxible curation unsuppress ITEM_ID
  --expected-latest-event-digest DIGEST --reason TEXT [--suppression EVENT_ID]
  [--attribution-ref REF]... [--json]
~~~

Detection runs on its own: the `curation.detect` internal action, fired by the
seeded `curation-detect` Trigger on every accepted generation, runs every
detector at the head and records what it found, evaluated at the fire's recorded
instant. `curation list` is a pure read of that queue: it prints each item's ID,
pattern kind, subject and `latest_event_digest` (what every ruling needs), when
detection last ran (`current`, `behind` or `never_run`) and whether its Trigger
is live, and the detectors that cannot run here and why (dead vocabulary needs
`CRUXIBLE_CONSUMPTION_RECEIPTS=on`; block churn needs a recorded workspace scan).
An instance created before the seeded Trigger needs it proposed once (`next`
reports `curation.detect` as unscheduled).

Block churn is the one detector that reads the workspace, which the daemon never
does: `curation observe` scans the workspace's declared blocks client-side and
records them, with the scan's accounting, for detection to read when it next
runs.

The queue is paged (default 25 items, at most 200); a cut page has
`truncated: true` and a `next_cursor` for `--cursor`, which continues only while
accepted state and the queue itself are unchanged; otherwise it is refused as
`cruxible.list.cursor_stale`.

The rulings append attributed operational events; they do not create governed
proposals or mutate accepted knowledge. `overrule` closes an item permanently:
its pattern is never raised again. `accept-fixed` links an item to the accepted
change that fixed it, named by proposal (pinned with `--changeset-digest` if
wanted) or by `--generation`; the change must postdate the item and touch its
subject or evidence. Detection never closes an item itself, even one whose
artifact a later change retired: closing a fixed pattern is always this
attributed ruling. `suppress` hides the item (`item`) or its whole lineage,
the successors its pattern opens after a fix (`lineage`), until
`--until-generation` or until `unsuppress` lifts it; detection keeps running.
`unsuppress` names the item that recorded the suppression, even once that item
is fixed: a lineage suppression on a resolved item keeps hiding its successors
until lifted there.

## audit

~~~text
cruxible audit [--claim-type ID]... [--subject-kind KIND]...
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

## set, add, retire and write

~~~text
cruxible set SUBJECT FIELD VALUE --because TEXT
  [--evidence-file PATH#ANCHOR | --capture CAP-HANDLE|DIGEST | --evidence-contract NAME]
  [--role ROLE] [--contend] [--expect VALUE... | --expect-absent]
  [--workspace-root DIR] [--dry-run] [--no-accept] [--at GIT_OID] [--json]
cruxible add SUBJECT FIELD VALUE --because TEXT
  [--evidence-file PATH#ANCHOR | --capture CAP-HANDLE|DIGEST | --evidence-contract NAME]
  [--role ROLE] [--expect-absent]
  [--workspace-root DIR] [--dry-run] [--no-accept] [--at GIT_OID] [--json]
cruxible retire TARGET [FIELD] --because TEXT
  [--reason was-rescinded|was-wrong|superseded] [--expect VALUE]... [--dry-run] [--no-accept]
  [--at GIT_OID] [--json]
cruxible write FILE [--because TEXT] [--workspace-root DIR] [--dry-run] [--no-accept]
  [--at GIT_OID] [--json]
cruxible write --schema
~~~

`set` puts VALUE in FIELD of SUBJECT (`kind/id`). On a single-value field it
replaces the live value: the Claim it revises is found for you. A Subject of a
known kind that does not exist yet is added in the same change set; a
Subject-valued VALUE must already exist. FIELD is a field of the kind as
`orient` names it, or the full predicate. VALUE is text: an enum member, a
number or `true`/`false` for such fields, a Subject as `kind/id` (`@kind/id`
names the same Subject, in SUBJECT too), or the text itself for exact content
(which is also its own evidence). The default evidence
is `--because` as self evidence; `--evidence-file` cites text found once in a
catalogued workspace file, read on this side, and `--capture` an existing
Capture by its sha256 digest or its handle `CAP-<12+ hex>` (a digest prefix
unique among the verified Captures the instance holds, cited or not; ambiguous
or unknown handles refuse with the nearest handles). `--evidence-contract NAME` cites the newest verified Capture of
that CaptureContract about SUBJECT, cited or not: one an accepted Claim on
SUBJECT cites, or whose own source names SUBJECT; with none it refuses
`cruxible.write.contract_capture_not_found`. Only a Capture committed as exact
bytes can back a Claim: when the newest is a canonical value (as the external
record reader commits records) it refuses `contract_capture_not_citable`
naming it, or cites an older exact-bytes Capture with a
`newer_capture_not_citable` warning. Either resolves to the digest
before the write is lowered, and the change prints the Capture as
`evidence CAP-<12 hex>`. The write accepts in the same call when the approval policy and your
tier allow it; otherwise it prints the eligible approvers and the approve
command. `--no-accept` only proposes. `--dry-run` runs every check and writes
nothing; pass its coordinate back as `--at` to refuse
(`cruxible.write.slot_changed`) if the field moved since. `--expect VALUE` is
the compare-and-set by value: it refuses `cruxible.write.slot_changed`, showing
what the field holds, unless it holds exactly VALUE (repeat `--expect` for every
value of a many-valued field); `--expect-absent` expects the field to hold
nothing. Both compose with `--at`. Each change prints
before and after, its Claim and its verdict; a verdict other than `supported`
prints a warning with its repair.

`add` puts one more VALUE in a many-valued FIELD, beside the values already
there; a value already live is answered as done, and `--expect-absent` refuses
it instead (`cruxible.write.value_already_present`). `retire` ends one live
Claim, named by ID or by SUBJECT FIELD when that field holds one value; the
Claims that depend on it retire with it; `--expect` compares the field's values
as on `set`. `write` applies a
FILE (YAML or JSON) of changes as one change set: `{"because": ..., "changes":
[...]}`, or a bare list with `--because`, each change
`{"op": "set" | "add", "subject", "field", "value"}` or
`{"op": "retire", "target"}`. `add` puts one more value in a many-valued field;
two adds on one field land in one change set. A top-level `"subject"` is the
Subject of every change that names none (a retire's target may then be
`{"field": ...}`); a change's own subject overrides it, and a change with
neither refuses `cruxible.write.subject_required`. `--schema` prints what FILE
holds; `write -` reads the change set from stdin, so a heredoc or a pipe needs
no file. A refusal prints its code, the nearest valid names and the repair, and
exits 1.

Two lanes change accepted state. Value changes are these verbs: one change uses
its verb (`set`, `add`, `retire`; MCP `cruxible_set` and `cruxible_retire`; SDK
`cx.set` and `cx.retire`), and several changes that must land together use
`write` (MCP `cruxible_write`; SDK `cx.changes(because=..., subject=...)` with
`.set`, `.add` and `.retire`, then `.write()`): atomic, one proposal under a
review policy, one generation, one `because`, one preview. Each is applied
under the approval policy, accepting in the same call when it and your tier
allow. Definitions go through authoring instead (`cruxible authoring compile`
and `submit`; SDK `cx.changes(rationale=...)`): an intent you compile and
preflight, a proposal, then review and activation. ClaimTypes keep their own
`claim-type` group, because changing vocabulary disposes the Claims that
depend on it.

## get

~~~text
cruxible get REF [--detail summary|evidence|why|history|proof|body]
  [--range START:END] [--limit N] [--cursor C] [--at GIT_OID]
  [--evaluation-time TS] [--json]
~~~

Reads one thing by any reference form you have seen: `CLM-...` (or a unique
prefix of at least four hex digits), `kind/id` or `Subject:kind/id`, a
predicate (full, or a leaf unique across kinds) or `ClaimType:<predicate>`,
`Document:<name>`, `Procedure:<name>`, `query:<name>`, `CaptureContract:<name>`,
an artifact path, or a proposal id or prefix. Operational things resolve too:
`Line:<name>` (or the Line identity digest `next` names a due Line by, in full
or as a 12+ hex prefix) answers the Line's Procedure, its schedule kinds, each
Trigger aimed at it by name and version (with the `get Trigger:<name>` that
reads it), its authority, `triggers_inactive: not enabled` while Triggers aim
at it but it is not enabled, its enablements (the principal kind, state and
stop reason, the pinned Line digest and Trigger versions, how far its daemon
matched (`evaluated_until`), and who enabled each: a runtime credential's id
and label only to that credential or an admin, otherwise
`enabled_by_withheld`), due and waiting occurrences and recent runs; `CAP-<12+ hex>` or `Capture:<digest>`
answers a Capture's contract and version, observation time, size, availability
and the Claims (and their Subjects) that cite it; `ResolutionContract:<name>`
answers the hypothesis Claim, window, rule and bound-window state; and
`Mandate:<name>` (or `ProcedureMandate:<name>`) answers the grant, validity and
state. Enablements, occurrences, runs, windows and capture availability are
operational state with no history: they are always read as of now at the
current head, whatever `--at` names, and the answer says so with `live`
(`as_of`: that head's 12-hex git oid and generation; `fields`: what was read
live). `orient` marks its runs, lines and predictions sections, and its map's
enablement attention and run counts, the same way. `--detail` picks the depth:
`summary` (default) prints a values-first card -- a Subject's Claims as an
aligned table, a Claim's value, verdict and flags (`stale`, `contested`,
`contradicted`, `unsure_hold`) -- `evidence` lists a Claim's captures by
CaptureContract name and version, its attestations and rationale, `why` and
`proof` print today's explanation and full envelope, `history` lists revisions
newest first with values and who changed them (`--limit`, default 20, per page;
a cut page prints the `--cursor` command that continues it), and `body` prints
a Document's bytes. A body over 64 KiB needs `--range`. A summary cuts a string
value over 500 characters and says how long it is; `--detail evidence` or
`proof` shows it whole. Subject rows carry the Claim id behind each value. A
summary's coordinate is the git oid's 12-hex prefix and the generation; the
full accepted coordinate is under `--detail proof`. `--at` takes a git oid, a
unique 12+ hex prefix of one, or a generation number, so either half of a
printed coordinate reads back; each history row prints both (`seq N at
<12 hex>`). An all-digit value of 11 or fewer characters is always a
generation, never an oid prefix; twelve or more digits are a prefix. Evidence
names each Capture by its `CAP-<12 hex>` handle, which `get` and `capture read`
both accept. A wrong or ambiguous REF
refuses with a code and the nearest names. `--json` prints the whole structured
result.

## orient

~~~text
cruxible orient [--kind KIND | --section SECTION]
  [--limit N] [--cursor C] [--at GIT_OID] [--evaluation-time TS] [--json]
~~~

The map of accepted state, in one call. With no option it prints each Subject
kind with its live Subject count and its predicates (short name, cardinality,
type or enum members, and the CaptureContracts whose evidence the ClaimType
admits, by name), the artifact counts, the named queries with their parameters,
who you are and whether you can author (when not, the same `authoring_refusal`
code, detail and repair that `whoami` reports), what the `next` queue
holds, and the next commands to run. When any Line was ever enabled, attention
counts the enablements as the instance's Line consumer reports them (running,
stalled, stopped) and names up to three stalled or stopped Lines with the stop
reason, for any caller of the instance, without daemon scope; it also notes
when enabled Lines have no consumer loop running here and when the provider lane
is unavailable. When any live ClaimType still names
CaptureContracts by digest, attention says so and suggests
`cruxible claim-type upgrade`.

`--kind` reads one kind in full: every predicate with its roles, freshness
horizon and live Claim count, the predicates of other kinds that point at it
(`incoming`, full names: follow one backwards with `query KIND --follow-in
PREDICATE:alias`), plus up to five sample Subject IDs. A kind that
does not exist is refused as `cruxible.orient.kind_not_found` with the nearest
kinds. `--section` pages one artifact family as compact rows; follow
`next_cursor` with `--cursor` while `truncated` is true. `--section runs` lists
Procedure runs, newest admission first, each with its Procedure, status,
admission time, Line and finished-node count, and `--section running` lists
only the runs still running. Both page by the admission's immutable position,
so a run admitted or finished after the first page is never repeated or
skipped; status is shown, never part of the order. Read one
with `cruxible get ProcedureRun:RUN-...` (or a `RUN-` prefix of 12+
hex): nodes done over the graph's nodes, the node a running run is on,
elapsed time (against the read's evaluation time while it runs, the measured
wall clock once it finished), the last finished nodes, the Line, occurrence and
enablement that admitted it, and the receipt digest once it is terminal. Per-node
durations are not shown: every journal record of a run carries the run's
evaluation instant.
The other operational sections page the same way: `lines` (each Line's
Procedure, trigger, authority, latest enablement state, `triggers_inactive`
when its Triggers do nothing because it is not enabled, and due/waiting counts),
`captures` (the Captures accepted Claims cite, newest first, keyset-paged,
each as its `CAP-` handle), `capture_contracts` (version, grade, how many
ClaimTypes admit each), `predictions` (each live ResolutionContract with its
bound windows by status and the next close) and `mandates` (grant, state and
expiry). The default map counts each family under `Artifacts` and never
inlines their rows; each section suggests the `get` of its first row.
`--section interfaces` lists the provider interfaces a Procedure node can call,
each with its interface digest, operation contract and the implementing
Providers with their implementation digests (`get ProviderInterface:NAME`
reads one, `--detail proof` its accepted inventory entry). `--section
principals` lists the principal registry (`get Principal:ID` reads one), and
`--section policies` every live standalone or embedded governed policy with its
declaring artifact (`get ApprovalPolicy:instance` reads the approval policy,
`get ProcedureRuntimePolicy:instance` the Procedure runtime ceilings and
`get SourceAcquisitionPolicy:NAME` one source acquisition policy).
The default map also counts every accepted Claim by status (accepted,
conflicted, overturned, refused, retired) under `artifacts.claims`. Kinds page the same way
when there are more than `--limit`. `--at` reads an earlier accepted generation.
`--json` returns the whole structured answer, including the coordinate and
generation.

When the current Git worktree holds this instance's floor (see
[floor](#floor)), orient also reports
`floor: {at, generations_behind}`: the accepted Git OID the floor was exported
at, and how many accepted generations it is behind this answer (`null` for a
floor exported before generations were stamped). The text output prints
`Floor: .cruxible/floor at <oid>, N generation(s) behind; refresh: cruxible
floor export --force`. It reads only the floor's `manifest.json` (its
coordinate and generation); it never re-exports.

## stub

~~~text
cruxible stub [--out PATH]
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

## since

~~~text
cruxible since GENERATION [--max-rows N] [--max-bytes N]
  [--access-profile FILE] [--cursor FILE] [--json]
~~~

Returns signed accepted ChangeSet members in `(GENERATION, pinned head]` order.
Follow `next_cursor` to continue against the same historical head even if main
advances; the cursor binds the lower bound, access profile, and page budgets.

## floor

~~~text
cruxible floor export [--force] [--with-discovery] [--json]
cruxible floor delivery STATE [--instance-id ID] [--json]
~~~

The floor has one writer. A local daemon with a registered workspace and
delivery on writes it after every accepted generation (the `floor-refresh`
Trigger), and `floor export` over its socket only asks it to deliver now; over
TCP the export refuses, naming `floor delivery off` as the way to write from the
client. Everywhere else `floor export` is the pull. `floor delivery` (local
socket only) chooses between the two.

Writes the deterministic greppable floor of accepted state to the fixed derived
cache `.cruxible/floor/` under the current workspace. The floor is the
grep-first front door to accepted state: agents search it with grep and read
the file a hit lands in. It does no matching of its own. The read and write
verbs (`get`, `query`, `orient`, `set`, `retire`, `write`) confirm live
verdicts and act.

### The loop

1. **Grep.** `grep -rn "some text" .cruxible/floor/current`.
2. **Read the header.** Every `current/` file starts with one line naming its
   ref and the generation it last changed, for example
   `# dev.roadmap_item/surface-pass  kind=dev.roadmap_item  changed gen 412`.
3. **Get the ref for live verdicts.** `cruxible get dev.roadmap_item/surface-pass`.
   The floor shows accepted values and no verdicts; `orient` says how many
   generations behind the head the floor is.
4. **Write with the verbs.** `cruxible set dev.roadmap_item/surface-pass
   adoption_state adopted --because "…"`.

An agent without a shell searches the values with
`cruxible query --contains "some text"` (`cruxible_query`
with `contains` on MCP) instead.

### What is in the floor

The floor is a pure function of the accepted coordinate (and the pinned review
notes snapshot its change rationale is read from). It holds no source content:
Document and evidence bodies stay behind `get`, and the ledger clone is the
audit path.

| Path | What it holds |
|---|---|
| `current/<kind>/<id>.yaml` | One file per Subject, values first (below). |
| `current/<kind>/<id>.<field>.txt` | A text value too long to inline (over 2 KiB or 40 lines), whole; never truncated. |
| `current/<kind>/INDEX` | One tab-separated line per Subject of the kind: ref, a title-like value, `field=value` for each state-like field. |
| `changes/<seq>.json` | The accepted change that introduced a current Claim revision: time, actor, the rationale its proposal recorded, and the refs it changed. |
| `sources/INDEX` | Written by the client from `sources/LEDGER` and its own source catalog, outside the daemon manifest: one tab-separated line per evidence source (self-source Captures excluded; a Document is a source, cited or not, on the line of its name): source, CaptureContract(s), where it lives (the workspace path the catalog binds it to, `(missing)` when that file is gone; otherwise the ledger locator), citing-Claim count, the generation it last changed. |
| `sources/LEDGER` | The same lines from accepted state alone, which holds no workspace paths: the locator is the Document of the source's name (`Document:<id>`); otherwise every distinct `coordinate/selector` type pair its Captures were taken at, sorted and comma-separated (a foreign source has none); or `-`. |
| `projections/INDEX` | Written by the client from its own workspace bindings, outside the daemon manifest: one line per workspace file bound to accepted state (Document body, evidence source, rendered block), its role, bound ref and the generation that ref last changed. A bound file that does not exist is a `workspace_binding_missing` row in `next`, not a line here. |
| `manifest.json` | The coordinate, its generation, the renderer, and every file's digest and `changed_at`, bound into the floor digest. |
| `subjects/`, `claim-types/`, `procedures/`, `coverage-manifest.json` | Only with `--with-discovery`: the discovery cards other tools read (they carry digests and addresses) and the export's coverage boundary. They need the whole accepted facts read, so they cost most of an export. |
| `README.md` | This loop, for an agent that lands in the floor cold. |

A `current/` file is a strict subset of YAML, so it both greps line by line and
parses with any YAML reader:

~~~yaml
# project.work_item/wi-1  kind=project.work_item  changed gen 5
governs:
  - project.work_item/wi-2  # CLM-985cd53c0dc20d4eb15481d8980bf35c
  - project.work_item/wi-3  # CLM-b5d5f511045eb94131d21f2a59595484
measured: 3  # CLM-714a14a1891fef7eb464b69de3a4e2c6
ruling: |  # CLM-ca6f1cf4aa8db970935fe9792c07b509
  Rulings are text.
  Every line of this one greps on its own.
status: ready  # CLM-ee98966ed497f1b629f44bf4eb686cb5
title: "Tidy the CLI: part #1"  # CLM-77795e37bc864490c35f2c603f2a12d6
incoming:
  - governs <- project.work_item/wi-4  # CLM-0f3a…
~~~

- Fields use the same short names `orient` advertises and `query` resolves.
  Every live Claim of a slot is shown: a many-valued field, or a single-valued
  one with several live Claims, lists every value.
- Each value ends with the Claim that states it (`CLM-…`); `get` on it lists the
  Captures behind it. A Subject-valued field shows the other Subject's ref.
- `incoming:` lists the live Subject-valued Claims of other Subjects that point
  here, as `<field> <- <ref>`, so either end of an edge greps.
- `flags:` carries only the structural `contested`: a single-valued field with
  more than one distinct live value. No verdict (stale, uncovered,
  contradicted) is in the floor: verdicts move with time and evidence, which no
  coordinate fixes.
- An exact-content value (a ruling) is its text, read by digest. Accepted
  bodies (and the Capture envelopes `sources/LEDGER` reads) are retained for as
  long as their Claim is in history, so the floor is fixed by the coordinate;
  one lost anyway refuses a fresh render as an integrity failure rather than
  publishing a different floor. Bytes that are not UTF-8 text show as
  `{exact_content: binary, bytes: N}`.

### Freshness and deltas

Every file is stamped with `changed_at`, the latest accepted generation that
touched any of its inputs (for a `current/` file: the Subject, its Claims of
any lifecycle, the Claims pointing at it, and the ClaimTypes naming its
fields). A file's bytes differ between two generations exactly when its stamp
moved, so a floor at generation B lacks exactly the files stamped after B plus
the paths dropped since. `orient` reports `floor: {at, generations_behind}`
from the manifest alone.

The manifest also names the review notes the change rationale was read from
(`notes_digest`, the digest of every rationale `changes/` shows); a rationale
revised after acceptance changes it, and a refresh then replaces the floor
whole.

The daemon keeps a rebuildable floor index on each instance and advances it by
the ledger diff. `floor export` and every refresh send the generation and
renderer of the floor the workspace holds to `POST /floor/delta`,
which answers with a delta (or the whole floor, for a missing, newer or
foreign base). One shared apply verifies the base and head manifest digests
and the bytes actually installed before writing anything (a hand-edited,
missing or stray file makes it ask for the whole floor, which repairs it while
keeping the client's own `projections/INDEX`), writes each file atomically
through directory descriptors that never follow a link, and writes
`manifest.json` last, so an interrupted refresh resumes cleanly.

### Writing the directory

The daemon returns bytes keyed by floor path and never writes a client path;
export refuses a non-empty directory that holds no floor unless `--force` is
given, and a floor already at the head is a no-op success reported as
`unchanged`. With `--with-discovery` the export is a full export and also
carries its coverage boundary in `coverage-manifest.json`. `floor_output.path`
is obsolete and refused; a v2 coverage config enables refresh with only the
fixed profile. `floor export` records that profile, and its opt-in parts, so a
refresh after an activation exports the same parts and the following `next`
observation no longer reports the floor as `missing` after a successful export:

~~~json
{
  "tag": "playbill-coverage-workspace-config-v2",
  "floor_output": {
    "tag": "playbill-floor-output-v1",
    "format": "playbill-floor-export-v5",
    "include": ["discovery"]
  }
}
~~~

`include` is present only for a floor exported `--with-discovery`. A profile an
earlier build recorded at an older format is refused until
`floor export --force` rewrites it. `manifest.json` inventories and digests the
exact rendered bytes, so repeated exports at one accepted coordinate remain
byte-identical.

## coverage

~~~text
cruxible coverage resolve
  [--bind PATH=PLANE:IDENTITY]... [--bindings FILE] [--root DIR]
  [--file PATH]... [--range PATH:START-END]...
  [--grep-results FILE] [--all] [--view cards|manifest] [--brief] [--json]
~~~

resolve answers what the working files you just read or changed have to do with
accepted state. Every working path is bound to a logical source by a
declaration, never inferred from a filename, because identical bytes in another
file are precisely not the same source. The declaration is the workspace's
source catalog (`.cruxible/sources.yaml`): each catalogued file binds to
`external:<name>`, the identity file evidence is cited under. `--bind` and
`--bindings` (a mapping, `-` for stdin) override it path by path, for example to
bind a file to a ledger path. The CLI reads and hashes the bytes locally; the
daemon reads no client filesystem.

Governed spans are annotated inline in card order. Ungoverned results are
summarized once per operation, never one line per result:

~~~text
Cruxible coverage: 2 exact, 1 drifted, 3 candidates, 41 none
coverage complete for 47 returned spans at generation gen-sha256:...
omitted cards: 0, truncated spans: 0
~~~

A `none` is factual only inside a complete boundary, so a span whose health is
`partial`, `stale`, `denied`, or `unavailable` prints that health and its reason
codes rather than reading as an absence.

`--all --view manifest` renders the coverage manifest over every bound file:
epoch, health, completeness, and the sources a `none` would have been factual
inside.

Resolving coverage changes no accepted state. It writes the local coverage
manifest cache, and appends a consumption receipt when the daemon runs with
`CRUXIBLE_CONSUMPTION_RECEIPTS=on`.

`.cruxible/coverage.json` holds the workspace's instance, transport and floor
profile only; it carries no path rules (the Claude Code hook that read them is
gone). A harness that owns its tool executor embeds the vendor-neutral
middleware in `cruxible_core.coverage.middleware` and passes its own rules.

## proposal

~~~text
cruxible proposal list [--status open|settled|incomplete] [--limit N]
  [--cursor CURSOR]
cruxible proposal readmit PROPOSAL_ID
cruxible proposal withdraw PROPOSAL_ID --reason TEXT
cruxible proposal review PROPOSAL_ID [--include-body|--redacted]
  [--workspace-root DIR]
cruxible proposal approve PROPOSAL_ID
  --signer-id ID --key FILE [--yes]
cruxible proposal activate PROPOSAL_ID
cruxible get PROPOSAL_ID [--detail proof]
~~~

`get` is the one proposal read. Its card names the status, verdict, actor,
rationale and changes; a refused proposal's card carries every refusal
diagnostic with its code, message and repair, and `--detail proof` adds the
admission, evaluation and candidate records.

`cruxible whoami` names the actor and where its ID came from (the
credential's principal, the configured principal ID, or the local operator),
whether a credential authenticates it (with auth off the ID is a claim, not
authentication), its effective permission mode, accepted principal-registration
status, and current coordinate. It also says whether this actor can author and,
if not, why: `can_author` and `authoring_refusal` carry exactly the code, detail
and repair authoring would return (`cruxible.identity.principal_unconfigured`,
`principal_absent`, `principal_revoked`, `credential_unbound`,
`permission_insufficient`, or `cruxible.instance.decommissioned`). Authoring
refuses such an actor at `authoring compile` and `submit`, before any payload is compiled or
preflighted, rather than at proposal evaluation
(`cruxible.proposal.creator_principal_invalid`).
`proposal list` prints a labeled `COORDINATE_TIME` column and deterministically
separates current open candidates from accepted, refused, and stale terminal
evidence so retries do not depend on remembered IDs. It returns one page
(default 50, at most 500); a cut page has `truncated: true` and a `next_cursor`
for `--cursor`, which keeps reading the first page's accepted coordinate. A
proposal admitted or withdrawn between pages changes the listing, and the
cursor is then refused as `cruxible.list.cursor_stale`: list again without it. Proposal actions accept a
full digest, a unique digest prefix (`sha256:` plus at least 8 hex characters),
or a target ref whose current Git target
names exactly one admission; unknown and historical ambiguous selectors are
typed refusals that point back to `proposal list`.
`proposal readmit` replays a stale proposal's authored content through the current
governed rebase and returns a fresh, idempotent proposal without changing the old
proposal evidence. It refuses `cruxible.proposal.readmit_already_accepted` when
the change is in accepted state -- the proposal itself was accepted, or its
readmission was (`context.accepted_as`) -- and `cruxible.proposal.readmit_not_stale`
for an open or refused proposal. A stale generated ClaimType dependency-closure migration is not
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
Activation is a daemon act and writes nothing locally; it returns the
activation receipt. A workspace the local daemon serves gets its floor from the
daemon's floor-refresh trigger; other setups pull it with `cruxible floor
export`. Read exactly what was accepted with `get` or `query` at the receipt's
coordinate; `next` reports any projection block the change left stale.

### Reviewing a proposal

The ledger is Git, so review is Git. The daemon fetches its own refs into the
attached workspace on every proposal, so a reviewer compares the candidate
against accepted state with standard tooling and no bespoke rendering:

~~~text
git diff cruxible-ledger/accepted...cruxible-ledger/proposals/<proposal-id>
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
runs the same diff against `origin/main`; see [ledger](#ledger) for what the mirror carries and how to get its URL.

## principal and whoami

~~~text
cruxible principal add PRINCIPAL_ID --key-dir DIR [--signer-key PATH]
  [--mode governed_write] [--kind ordinary] [--name NAME] [--dry-run|--commit] [--at OID] [--json]
cruxible principal rotate ...
cruxible principal revoke ...
cruxible principal recover ...
~~~

The principal registry is read with `cruxible orient --section
principals`, one principal with `cruxible get Principal:ID`.
Registration, rotation, revocation, and recovery are governed principal-change
proposals. `principal add` generates the Ed25519 private key exclusively in the
client-held `--key-dir` outside the current workspace and sends only its public
principal record. Every principal-lifecycle proposal requires the PROPOSING
actor's own cryptographic approval before it can settle — the identity shown
by `cruxible whoami`, which coincides with the affected principal only for
self-rotation: run `cruxible proposal approve PID --signer-id <the proposing
actor> --key <that actor's current private key> --yes`, then
`cruxible proposal activate`. For `principal add` and `principal recover`,
the signer is the actor performing the operation, never the new or
locked-out principal. Registration neither
grants authority immediately nor sends a private key to the daemon. Other
non-creator principals may record additional voluntary approvals. `--kind`
is explicit and may be `ordinary` or `recovery`; the daemon kind is
instance-owned. Recovery principals cannot approve ordinary Document candidates.

`principal add` is the one command that sets up an agent. With `--signer-key`
(your own private key; also `CRUXIBLE_PRINCIPAL_KEY`) it proposes the
registration, approves it as you, and activates it. When the daemon runs with
auth it then mints the new principal's bearer credential at `--mode` (default
`governed_write`), signed with the new principal's key. Everything the agent
needs lands owner-only in `DIR/cruxible.env`: the transport, the instance, its
principal ID, its key path, and, with auth, its credential (written, never
printed). The agent loads it with `set -a; . DIR/cruxible.env; set +a`; the CLI,
SDK and MCP server all read those variables. Without `--signer-key` the
registration is only proposed and the command prints each remaining step:
`proposal approve`, `proposal activate`, and, with auth,
`credential mint --principal-id ID --key-dir DIR`, which writes the token into
the same settings file. `cruxible init` writes the owner's settings file the
same way. `proposal approve` defaults `--signer-id` to the configured principal
and `--key` to `CRUXIBLE_PRINCIPAL_KEY`.

A propose-only agent is a principal whose credential is `governed_write`: it
can author and propose, and the tier refuses approvals and activation. That
limit needs daemon auth; with auth off every process of the OS user is equally
trusted and could load another principal's settings. See the
[quickstart](quickstart.md#add-a-propose-only-agent) for the worked example.

## sources

~~~text
cruxible sources check [--catalog FILE] [--local-catalog FILE] [--root DIR]
  [--root-alias NAME=PATH]... [--json]
cruxible sources compile --output FILE [--catalog FILE] [--local-catalog FILE]
  [--root DIR] [--root-alias NAME=PATH]... [--json]
cruxible sources propose --source NAME --name NAME [--bundle FILE]
  [--catalog FILE] [--root DIR] [--dry-run|--commit] [--at OID] [--json]
~~~

The source catalog (`.cruxible/sources.yaml` or `sources.yaml`, plus an optional
`.cruxible/sources.local.yaml` overlay) is the one mapping from workspace files to
logical sources. Every command discovers it the same way; `--catalog` names
another. An entry needs only `name` and `locator` to be cited as evidence
(`set --evidence-file`, `cx.file`), covered (`coverage resolve`) and watched by
`next`; adding `document_id`, `document_kind`, `title`, `media_type` and
`governance_scope` (all five together) makes it a Document that compiles and
can be proposed.

`sources compile` reads the catalogued Documents' bytes client-side and writes a
path-free bundle; `sources check` reports each one's alignment (aligned,
modified, ahead, pending, behind, diverged, untracked); `sources propose` proposes
one source as its Document's next revision, compiling the catalog first unless
`--bundle` names a frozen one. This is the repair `next` names for a
`document_modified` row. The daemon never reads a submitted client path.

Governance and provenance explanations are `get` details:
`cruxible get Document:NAME --detail why`, `get KIND/ID --detail why`,
or `get CLM-... --detail why`; `--detail proof` reads the full accepted
envelope and facts.

Use --json on operation commands for machine-readable output. Run any command
with --help for its exact options.

## compiler

`cruxible compiler upgrade --to DIGEST --name NAME [--dry-run]` creates an admin-only
proposal bound to the exact accepted head and target compiler. Review it, sign
through `cruxible proposal approve`, then use
`cruxible proposal activate`. Activation validates the full target
projection before advancing the signed ledger. Installing or restarting a daemon
does not upgrade an instance. Historical generations keep their original compiler.
The instance descriptor retains its genesis compiler; inspection reports the
active compiler from accepted history. Unsupported transitions and downgrades are
refused. Artifact format migrations are separate work. See
[Upgrading](upgrading.md) for the whole flow, from installing a release to
activating a compiler upgrade.
