# CLI reference

The public CLI has four top-level command groups.

## Global options

~~~text
--server-url TEXT
--server-socket TEXT
--instance-id TEXT
--json-compact
--version
~~~

Server-mode instance selection defaults to remembered context.

## context

Manage remembered daemon and instance context:

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
cruxible server status
cruxible server info
cruxible server restart
~~~

server start is the long-running daemon process and does not connect to an
existing server. State defaults to `~/.cruxible`; `--state-root` overrides
`CRUXIBLE_STATE_ROOT`. The obsolete `CRUXIBLE_SERVER_STATE_DIR` name is
refused. See [Canonical repository and daemon layout](canonical-repository-layout.md)
for the exact directory contract.

## playbill host

~~~text
cruxible playbill host create [--instance-id ID]
~~~

Allocates an empty daemon-owned host and remembers it. When the selected daemon
is reached through `--server-socket` or `CRUXIBLE_SERVER_SOCKET`, the command
also attaches the current Git worktree. A TCP client never sends a local path
for the daemon to interpret.

## playbill init

~~~text
cruxible playbill init --key-dir DIR
  [--principal-id ID]
  [--reviewer-key-dir DIR]
  [--require-independent-approval]
  [--recovery-key-dir DIR]
  [--recovery-principal-id ID]
  [--profile local|cloud]
~~~

Generates a client-held ordinary key outside the workspace and bootstraps the
ledger with its public principal record. An optional `--reviewer-key-dir` adds a
second ordinary principal; pair it with `--require-independent-approval` to make
one non-creator approval mandatory. Local key directories provide attribution
and repository hygiene, not a security boundary. Organization review normally
rides branch protection/CODEOWNERS on the state repository; real custody
separation belongs at the parked Cloud broker/leasing seam. The optional
recovery key remains lifecycle-only.

Successful initialization remembers the initialized instance before rendering
either JSON or human output. For an attached local worktree, the daemon creates
or validates the advisory `playbill` Git remote and fetches accepted `main` and
proposal refs into `refs/remotes/playbill/*`. Those refs are review aids only;
merging one never admits or activates governed state.

## playbill body

~~~text
cruxible playbill body store PATH
~~~

Stores exact bytes in inert CAS and prints their digest.

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
cruxible playbill subject get KIND ID
cruxible playbill subject history KIND ID
~~~

A Subject is an identity-only referent. KIND and ID are the two halves of its
`Subject:<kind>/<id>` identity.

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
cruxible playbill claim get IDENTITY
cruxible playbill claim history IDENTITY
cruxible playbill claim explain IDENTITY [--evaluation-time TS]
~~~

Claims are authored through `playbill authoring create`/`compile`; the retired
direct v1 proposal commands are not a second writer. `retire` preflights or submits one
attributed retirement over the complete dependent Claim closure; the request
must name every dependent reason and never receives a daemon-synthesized end
time. explain returns the verdict together with the law evidence and source
handles it was computed from.

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
cruxible playbill authoring create --example claim-flow-a|claim-self-source|procedure
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
cruxible playbill authoring confirm-insertion INTENT_ID OBSERVATION
cruxible playbill authoring abandon-insertion INTENT_ID
~~~

The authoring coordinator owns stable identities, timestamps, bases, and proposal
references. `compile` creates or updates an intent and performs a binding preflight;
`rebase` advances an unsubmitted refused intent to the current accepted coordinate;
`submit` is idempotent and never supplies approvals. `status` reports the remaining
approval or activation conditions without impersonating the actors who own them.
`bind --occurrence N` counts matching anchors in ascending byte-offset order and
selects the 1-based `N`th match. The resulting selector records the total number
observed while its start/end bytes name the selected occurrence; multiple matches
are therefore truthful input metadata, not an unresolved selection.
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

## playbill block

~~~text
cruxible playbill block repin SOURCE_ID BLOCK_ID [--claim ID]... [--query ID]...
  [--params CANONICAL_JSON]... [--workspace-root DIR] [--evaluation-time TS]
~~~

Refreshes only the opening marker of an agent-authored declared Markdown block.
An unstamped block requires explicit Claim or QueryDefinition backings; omitting
them on an already stamped block preserves its existing backing identities and
resolved query parameters. The client validates accepted state and atomically
replaces the marker only when the complete local source still matches its
observed bytes. It never renders prose, edits the body, or mutates governed
state. Evidence anchors inside declared blocks are refused client-side; an
explicit copy citation remains available.

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
Without a valid source catalog, `workspace_sources` remains explicitly unobserved.
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
enables refresh with only the fixed profile:

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

The workspace config's `instance_id` is the hook's selected instance. Transport
selection (server URL or socket) still comes from the CLI flags, environment, or
remembered CLI context.

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
cruxible playbill proposal approve PROPOSAL_ID
  --signer-id ID --key FILE [--yes]
cruxible playbill proposal activate PROPOSAL_ID [--workspace-root DIR]
~~~

`cruxible playbill whoami` names the credential-derived actor, its effective
permission mode, accepted principal-registration status, and current coordinate.
`proposal list` deterministically separates current open candidates from accepted,
refused, and stale terminal evidence so retries do not depend on remembered IDs.
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

summary and evidence are implemented. proof is reserved and returns a typed
unsupported-detail result.

Use --json on operation commands for machine-readable output. Run any command
with --help for its exact options.
