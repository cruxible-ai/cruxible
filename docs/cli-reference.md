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
cruxible server stop
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
configured transport to shut down gracefully, then waits for the state-root lock
to be released and says whether it was. Reaching for `kill` or a terminal
multiplexer's quit instead kills the launching shell and orphans the daemon,
which is how one state root ends up served by several live processes.

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
JSON clients read the same text in `provider_lane.detail`.

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
| `seed_materializations` | `[]` | Provider-sorted local checkout, commit, environment key, and measured materialization records used to verify a configured seed source; checkout paths remain operational and never enter governed bytes. |
| `workspace_allowed_roots` | `[]` | Canonical absolute roots that widen `workspace.file` beyond an attached workspace; these are daemon-local authority and never come from an environment variable. The daemon state root, its trust, custody, Provider-secret, and instance substrate stay refused inside any allowed root. |

Unknown entries, non-positive timing values, malformed JSON, unsafe deployment
paths, and an unreadable file degrade only the Provider lane with a typed cause.
Because the compiler-owned `workspace.file` seed is a local materialization,
initialization and explicit re-seeding require its `seed_materializations`
entry and refuse rather than trusting an unchecked checkout.
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

## playbill host

~~~text
cruxible playbill host create [--instance-id ID] [--workspace DIR] [--replace]
cruxible playbill host show INSTANCE [--json]
cruxible playbill workspace attach [--instance-id ID] [--replace]
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

`host show` is a zero-authority inspection of workspace registration, exact
compiler coordinate/revision, and write compatibility; the CLI adds the selected
transport. The daemon-local managed root is visible only to an unscoped operator,
not an instance-scoped credential. `workspace attach` is client-local and requires a Unix
socket: it writes `.playbill/coverage.json` for an existing host only after the
daemon proves that it registered the exact current Git worktree. A missing or
different registration is a typed refusal and no config is written.

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
  [--no-seed]
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

Initialization seeds the compiler-owned `workspace.file` Provider by default and
refuses when its `seed_materializations` entry is absent, so a host is never
seeded from an unchecked checkout. `--no-seed` is the explicit opt-out, never a
silent default: the instance is created, the seed step is skipped, and the
result carries a typed `provider_seed` row with status `unseeded` whose `repair`
names the one way to finish — configure `seed_materializations`, then run
`cruxible playbill provider seed`. Self-approval and independent-approval
instances honour the flag identically, and an exact init retry carrying it stays
idempotent. The equivalent request field is `seed` on the HTTP/SDK init body and
on MCP `cruxible_playbill_init`; all four surfaces carry the same default and the
same typed `unseeded` row, so an MCP-first client can initialize a daemon whose
`seed_materializations` are not configured yet.

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

## playbill provider

~~~text
cruxible playbill provider seed
~~~

Submits the compiler-owned `workspace.file` interface and Provider as an
ordinary governed proposal. Self-approval instances activate it immediately;
independent-approval instances return the proposal ID for the usual review,
approval, and activation ceremony. This write is available through CLI, SDK,
and HTTP; the Provider write family does not yet have MCP parity.

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
cruxible playbill authoring create --example claim-flow-a|claim-self-source|claim-subject-relation|procedure|change-set
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
cruxible playbill authoring prepare-publication INTENT_ID OBSERVATION
  [--expectation-id ID]
cruxible playbill authoring confirm-insertion INTENT_ID OBSERVATION
  [--expectation-id ID]
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
may retire a Claim the set does not otherwise touch.
Succeeding an accepted ClaimType stays on the claim-type proposal route, where
the migration a succession demands is decided. Two sibling Claims contending for
one cardinality-one slot are un-authorable in a single set by construction, not
merely unrepaired: dispositioning one needs the other's Claim ID, which the
daemon mints only at create from the already-frozen payload, so that refusal's
repair is to merge the two decisions or split the set. `--example change-set`
prints a mixed set to start from. There is no semantic member ceiling; how many
changed members one daemon will receive in a single submission is the operator's
`max_changed_members` bound in `daemon/proposal-receive.json`.

An intent that publishes more than one Claim owns one publication expectation
per publishing member, so `prepare-publication`, `confirm-insertion` and
`abandon-insertion` take an `expectation_id` naming the one they are about. A
singular Claim intent owns exactly one and may omit it.

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
V2 publication preparation commits a deterministic Claim-backed block against fresh
whole-source bytes before the client applies it. A successful response includes
`inserted_block_base64`: standard RFC 4648 base64 for the exact UTF-8 bytes to
insert. (The canonical stamp token inside the opening marker uses URL-safe base64;
these are deliberately different layers.) Decode with validation, then apply the
preparation without inventing an offset: for `replace_window`, replace
`rebased_selector.start_byte:end_byte`; otherwise splice the decoded block at
`rebased_selector.insertion_offset`. In Python, the complete byte operation is:

~~~python
block = base64.b64decode(preparation["inserted_block_base64"], validate=True)
selector = preparation["rebased_selector"]
if preparation["operation"] == "replace_window":
    postimage = preimage[: selector["start_byte"]] + block + preimage[selector["end_byte"] :]
else:
    offset = selector["insertion_offset"]
    postimage = preimage[:offset] + block + preimage[offset:]
~~~

The decoded block is one LF-only opening marker line, the accepted body (ending
in LF), and one LF-only closing marker line:

~~~text
<!-- playbill:block:BLOCK_ID:BASE64URL_CANONICAL_STAMP -->
BODY
<!-- /playbill:block:BLOCK_ID -->
~~~

The marker must start in column zero; blocks cannot overlap, nest, or repeat an ID,
and marker-looking text inside a Markdown fence is not a declaration. Clients should
insert the returned bytes verbatim rather than synthesize this grammar. Insertion
confirmation verifies the exact client observation; legacy v1 opens an ordinary
backing-only successor candidate, while v2 binds the stamped block without a copy
citation. Abandon closes only an unprepared publication expectation.

## playbill policy

~~~text
cruxible playbill policy list [--json]
~~~

Lists the live standalone and embedded governed policies at the accepted coordinate.

## playbill query

~~~text
cruxible playbill query propose --envelope FILE --name NAME
cruxible playbill query list
cruxible playbill query get NAME
cruxible playbill query run NAME [--parameters FILE] [--evaluation-time TS]
~~~

run executes one accepted QueryDefinition and prints its
`playbill-query-execution-receipt-v1`: the definition digest, the resolved
parameter digest, and the result digest that replays it.

## playbill procedure

~~~text
cruxible playbill procedure readiness NAME --evaluation-time TS
cruxible playbill procedure bind NAME REQUEST_FILE
cruxible playbill procedure run NAME INPUT_FILE --evaluation-time TS
cruxible playbill procedure status RUN_ID
~~~

The first served profile runs deterministic `state_tap`, `transform`, and
`project` graphs only. `readiness` names open slots or unsupported nodes before
execution. `bind` proposes a same-identity Procedure successor with exact accepted
pins; it never mutates the accepted Procedure in place. Runs append replay-verifiable
journal records and `status` reconstructs the one-read run state from those records.

## playbill line

~~~text
cruxible playbill line run LINE_IDENTITY_DIGEST --evaluation-time TS
  [--occurrence-id ID] [--json]
~~~

Triggers one daemon-derived due occurrence. The occurrence's evaluation
instant is the daemon's; `--evaluation-time` only asserts the instant the
caller believes it is running at, and an assertion outside the daemon's skew
bound is refused. That bound is operational, not wire: the daemon reads
`evaluation_instant_skew_seconds` from `daemon/procedure-runs.json` in its own
state root, defaulting to the 300-second ProcedureMandate skew the bound
protects, and refuses the run if that file exists but cannot be read as one.
The accepted Line's governed mandate authorizes execution; without one the
operation returns a typed no-mandate refusal.

## playbill predictions

~~~text
cruxible playbill predict REQUEST_FILE [--json]
cruxible playbill settle PREDICTION_ID REQUEST_FILE [--json]
~~~

`predict` proposes the predicted Claim and retains its settlement declaration.
`settle` requires later accepted observation evidence or the prediction's
governed terminal, then records the declared score and resolution as Claims.

## playbill block

~~~text
cruxible playbill block repin SOURCE_ID BLOCK_ID [--claim ID]... [--query ID]...
  [--backing SHA256] [--params CANONICAL_JSON]... [--workspace-root DIR]
  [--evaluation-time TS]
cruxible playbill block sync [PATH]... [--all] [--check]
  [--detach PATH]... [--discard-local PATH]... [--workspace-root DIR]
~~~

Refreshes only the opening marker of an agent-authored declared Markdown block.
An unstamped block requires explicit Claim or QueryDefinition backings; omitting
them on an already stamped block preserves its existing backing identities and
resolved query parameters. The client validates accepted state and atomically
replaces the marker only when the complete local source still matches its
observed bytes. It never renders prose, edits the body, or mutates governed
state. Evidence anchors inside declared blocks are refused client-side; an
explicit copy citation remains available.

`block sync` converges only publication-origin blocks backed by one live Claim
whose retained coordinator self-source carries the accepted body. It follows
accepted Claim successors, updates the marker and body with one whole-file
compare-and-swap, and proves the bytes outside every marker by digest before and
after the atomic temp-file rename. `--check` writes nothing and exits nonzero
when a change is needed. `--detach PATH` strips a retired block's markers while
preserving its current body.

A block body is overwritten only when it still matches the last-synced body
digest in its stamp. A local edit is preserved and refused as
`block_locally_modified`; revise it through governed authoring, or explicitly
discard it with `--discard-local PATH`. Retired backings with no live successor
refuse until `--detach PATH` is explicit; two live terminal successors refuse
as ambiguous. The stale-block row from `playbill next` repairs with
`cruxible playbill block sync`.

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
`cruxible_core.playbill.coverage.middleware` is the full-fidelity path and
covers all four tool kinds, including same-turn edit drift.

## playbill proposal

~~~text
cruxible playbill proposal inspect PROPOSAL_ID
cruxible playbill proposal list [--status open|settled]
cruxible playbill proposal readmit PROPOSAL_ID
cruxible playbill proposal refusal PROPOSAL_ID
cruxible playbill proposal review PROPOSAL_ID [--include-body|--redacted]
  [--workspace-root DIR]
cruxible playbill proposal approve PROPOSAL_ID
  --signer-id ID --key FILE [--yes]
cruxible playbill proposal activate PROPOSAL_ID [--workspace-root DIR]
  [--no-sync]
cruxible playbill review open PROPOSAL_ID [--workspace-root DIR]
cruxible playbill review close PROPOSAL_ID [--workspace-root DIR]
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

approve signs locally. The private-key path is not sent to the daemon.
When `.playbill/coverage.json` at `--workspace-root` declares `floor_output`,
activate refreshes floor-v2 as a verified exact directory replacement. An
accepted activation followed by a failed local refresh reports both truths and
exits nonzero; the daemon never receives the workspace path.

After an accepted activation, the client runs block sync last unless
`--no-sync` is explicit. An unattached workspace retains a typed `skipped`
`workspace_not_attached` row and exits zero; a sync refusal in an attached
workspace reports the already-accepted truth, names `cruxible playbill block sync
--all`, and exits nonzero. `review open` refreshes the remote refs and creates a
detached, gitignored worktree at `.playbill/review/<proposal-digest>/`; `review
close` removes only a clean review worktree. A `review_workspace_not_attached`
refusal names the local-socket `playbill host create --workspace` command needed
when creating a host that supports review worktrees. This provides editor/diff
access without creating a local branch. No review-ref mirror script is needed:
standard Git tooling already lists the proposal namespace as remote branches.

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
