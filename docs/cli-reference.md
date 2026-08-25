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
cruxible server start
cruxible server status
cruxible server info
cruxible server restart
~~~

server start is the long-running daemon process and does not connect to an
existing server.

## playbill host

~~~text
cruxible playbill host create [--instance-id ID]
~~~

Allocates an empty daemon-owned host and remembers it.

## playbill init

~~~text
cruxible playbill init --key-dir DIR
  [--principal-id ID]
  [--recovery-key-dir DIR]
  [--recovery-principal-id ID]
  [--profile local|cloud]
~~~

Generates client-held principal keys outside the workspace and bootstraps the
ledger with public principal records.

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
cruxible playbill claim-type propose --input FILE --name NAME
cruxible playbill claim-type propose --example
cruxible playbill claim-type migrate REQUEST_FILE
cruxible playbill claim-type list
cruxible playbill claim-type get PREDICATE
~~~

A ClaimType is the governed interface a predicate must satisfy before any Claim
may state it. `propose --input` lowers the tagless model-generated form and
returns nonblocking policy/source lint beside the proposal. The optional,
sorted `anticipated_source_ids` input supplies source-specific repair suggestions
without entering the governed ClaimType. Expert proposals, migration preflight
and submission, and SDK cold-dependency preflight deliver the same typed lint;
advisories never enter candidate identity, the approval frontier, or its
certificate. `migrate` atomically succeeds the ClaimType and disposes every
dependent the request names; it never authors retirement decisions from diagnostics.

## playbill claim

~~~text
cruxible playbill claim propose --authoring FILE --name NAME
cruxible playbill claim propose-batch --authoring FILE [--authoring FILE ...] --name NAME
cruxible playbill claim list [--subject PATH] [--predicate P] [--include-retired]
cruxible playbill claim get IDENTITY
cruxible playbill claim history IDENTITY
cruxible playbill claim explain IDENTITY [--evaluation-time TS]
~~~

propose creates one inert Capture and one dependency-closed Claim in a single
governed proposal. propose-batch does the same for several Claims at once, and
the whole set is admitted as one generation or none of it is -- use it when a
Claim is only meaningful beside its siblings. explain returns the verdict
together with the law evidence and source handles it was computed from.

## playbill authoring

~~~text
cruxible playbill authoring create PAYLOAD
cruxible playbill authoring create --example claim-flow-a|claim-self-source|procedure
cruxible playbill authoring get INTENT_ID
cruxible playbill authoring resume INTENT_ID
cruxible playbill authoring list
cruxible playbill authoring compile PAYLOAD [--intent-id INTENT_ID]
cruxible playbill authoring bind --file PATH --anchor TEXT [--window-lines N]
  --payload-file CLAIM_STUB
cruxible playbill authoring preflight INTENT_ID
cruxible playbill authoring rebase INTENT_ID
cruxible playbill authoring submit INTENT_ID
cruxible playbill authoring status INTENT_ID
cruxible playbill authoring confirm-insertion INTENT_ID OBSERVATION
cruxible playbill authoring abandon-insertion INTENT_ID
~~~

The authoring coordinator owns stable identities, timestamps, bases, and proposal
references. `compile` creates or updates an intent and performs a binding preflight;
`rebase` advances an unsubmitted refused intent to the current accepted coordinate;
`submit` is idempotent and never supplies approvals. `status` reports the remaining
approval or activation conditions without impersonating the actors who own them.
Insertion confirmation verifies a client observation and opens an ordinary
backing-only successor candidate; abandon closes only that publication expectation.

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

## playbill expand

~~~text
cruxible playbill expand ARTIFACT_PATH [--facet NAME]... [--evaluation-time TS]
~~~

Returns one bounded context capsule for an accepted address. Repeat --facet to
narrow what the capsule carries.

## playbill seed

~~~text
cruxible playbill seed apply BUNDLE_DIR --name NAME [--plan] [--group GROUP_ID]
~~~

Applies a bundle directory of authoring JSONs — `claim-types/`, `subjects/`,
`documents/`, `claims/`, `query-definitions/`, plus a `bodies/` subtree stored in
CAS first — as the fewest governed proposals it can legally become. Every Claim
settles as one batch proposal carrying the dependencies the Claims themselves
declare; each remaining artifact uses the singular propose operation the served
surface already has for it. No operation is added.

`--plan` prints the grouping and submits nothing; it reaches no daemon. Without
it, one group is submitted per invocation — `--group` names which, defaulting to
the first — because a proposal settles against the base it was admitted at and
two proposals opened against one head cannot both activate. Approving and
activating stay separate acts, so a harness loops plan → apply → approve →
activate over the printed group ids.

## playbill floor

~~~text
cruxible playbill floor export --output DIR [--force]
  [--with-native] [--evaluation-time ISO-8601]
~~~

Writes the deterministic greppable floor of accepted state. The daemon returns
bytes keyed by floor path and never writes a client path; export refuses a
non-empty output directory unless --force is given. The export carries its own
coverage boundary in `coverage-manifest.json`, enumerated in the root manifest
like every other floor file.

Floor export v2 pretty-prints every JSON card with stable key ordering for grep
quality. `manifest.json` inventories and digests those exact rendered bytes, so
repeated exports at one accepted coordinate remain byte-identical. Historical v1
manifests and compact JSON spelling remain readable without reinterpretation.

`--with-native` additionally writes the native knowledge renders into the same
directory, which is the arm-3/arm-4 file surface: floor artifacts (`.json`),
rendered pages (`.md`), and the coverage boundary in one greppable tree. The two
manifests keep their own names, `manifest.json` and `render-manifest.json`, and
neither format changes. The composition is done by the CLI, so the floor service
still touches no filesystem and the render lens stays a pure function of
accepted state. `--evaluation-time` pins the render's read time and applies only
with `--with-native`.

## playbill native

~~~text
cruxible playbill native render --output DIR
  [--evaluation-time ISO-8601] [--stash | --discard]
cruxible playbill native status DIR
cruxible playbill native compile DIR [--preview | --submit --name NAME]
  [--dispositions FILE]
cruxible playbill native stash list DIR
cruxible playbill native stash show DIR STASH_ID
cruxible playbill native stash restore DIR STASH_ID [--drop]
cruxible playbill native review-current PROPOSAL_ID
  [--bound DIGEST | --superseded-proposal PROPOSAL_ID]
~~~

The ledger is the semantic object store; the rendered directory is its editable
working tree. `render` checks out accepted knowledge as browsable Markdown --
one page per Subject with its Claims grouped by predicate, plus pages for
ClaimTypes, QueryDefinitions, and Documents, a `README.md` orientation floor,
and a `render-manifest.json` holding the baseline every field is compared
against. The render is a pure function of accepted state and an explicit render
context, so the same generation and the same read time always produce identical
bytes; the CLI supplies the read time, and the renderer never reads a clock.

Fields are typed. Statement values and qualifiers are editable and free-form
inside themselves; verdict, provenance, and coverage blocks are derived, always
rendered generation- and time-qualified, and regenerate rather than accept an
edit. Editing changes nothing accepted: compile is a separate gate.

`render` writes the bytes -- the daemon never writes into a repository -- and
refuses to overwrite an editable field you have edited, naming every dirty field
it would otherwise have lost and the three things you may do about it: compile
the edits into a proposal, `--stash` them, or `--discard` them.

`--stash` captures the dirty fields' exact bytes under `.playbill-stash/` beside
the render and then re-renders over them, so the default answer to "I edited
this and the head moved" keeps the work. A stash entry is disposable local
material: it commits to its own digest, it is written whole or not at all, and
deleting the directory loses only the edits somebody chose to stash.

`stash list` and `stash show` read those entries without a daemon. `stash
restore` re-applies one to the current render **by region identity**, which
carries no path -- so a stashed edit lands correctly even after its field moved
to another file. A stashed field the current render no longer has, or one that
no longer binds unambiguously, is reported and left in the stash rather than
placed somewhere it might not belong; `restore` exits non-zero when anything was
left behind, and `--drop` deletes the entry only when nothing was.

`status` compares a rendered directory against its own baseline and needs no
daemon:

~~~text
      clean  subjects/project.work_item/wi-43.md  8 region(s): 8 clean, 0 dirty, ...
      dirty  subjects/project.work_item/wi-42.md  8 region(s): 7 clean, 1 dirty, ...
Playbill coverage: 1 exact, 2 drifted, 3 candidates, 1 none
invalidated derived fields: 2 beside 1 edited statement(s); no governance fact
reaches the edited material
~~~

The invalidation half of that answer comes from the coverage resolver, not from
`status` itself: a rendered file is a working source, the render baseline is what
accepted state said its bytes were at that generation, and the drift the resolver
reports is what invalidates the verdict and provenance blocks beside an edited
field. No card it returns can grant the edited material a governance fact.

`compile` is the middle of three gates. It binds the baseline generation the
tree was rendered from, classifies every edited field against the current
accepted head, and assembles one change set: edited locator-bound fields become
successor Claims, and unlocated prose becomes an `unbound_native_draft` that
needs an explicit disposition -- `reuse`, `extend`, `new_distinct`, or
`withdraw` -- stated either in the file's invisible draft marker or in a
`--dispositions` list. A `new_distinct` disposition lowers into an explicit
`semantic.distinct_from` Claim for every blocking candidate, in the same change
set, and `--preview` shows those generated Claims before anything is submitted.
A draft with no disposition refuses, naming the candidates it collided with.

`--preview` and `--submit` render the same compile result; only `--submit`
creates a proposal, which is then approved and activated like any other. When
the accepted head has moved under the baseline, submitting binds the baseline
and the proposal receive path performs the deterministic three-way rebase --
no textual merge decides what is admissible. A rebase produces a different
candidate digest, so approvals collected against the old one no longer verify;
`review-current` reports that as `superseded_by_rebase` and names the digest
fresh approval must bind. It exits non-zero until review evidence binds the
candidate that would settle.

Approvals are stored under the digest they signed, so nothing on the current
candidate can report the earlier act -- the earlier act has to be named. `--bound`
names the digest directly. `--superseded-proposal` names the earlier proposal
instead and reads its candidate digest *and* its signers, refusing a proposal
admitted against a different proposal ref, so the report can say whose approval
must be collected again. Neither form enumerates a lineage: proposals sharing a
ref are not listable through any served read, so an earlier proposal is named
rather than discovered.

Deleting a rendered file or directory loses uncompiled local edits and nothing
else; removal is never inferred as retirement, and no compile path produces a
retirement either.

The render format is deliberately experimental through dogfood. The typed
editable/derived split and the round-trip laws are the contract; the Markdown
spellings and the invisible marker channel are versioned by the lens and
expected to change.

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
proposal evidence.

approve signs locally. The private-key path is not sent to the daemon.
When `.playbill/coverage.json` at `--workspace-root` declares `floor_output`,
activate refreshes floor-v2 as a verified exact directory replacement. An
accepted activation followed by a failed local refresh reports both truths and
exits nonzero; the daemon never receives the workspace path.

## playbill principal

~~~text
cruxible playbill principal list
cruxible playbill principal add PRINCIPAL_ID --role reviewer --key-dir DIR --name NAME
cruxible playbill principal rotate ...
cruxible playbill principal revoke ...
cruxible playbill principal recover ...
~~~

Registration, rotation, revocation, and recovery are governed principal-change
proposals. `principal add` generates the Ed25519 private key exclusively in the
client-held `--key-dir` outside the current workspace and sends only its public
principal record. An existing owner must approve the resulting proposal with
`playbill proposal approve`, then activate it with `playbill proposal activate`;
registration neither grants authority immediately nor sends a private key to the
daemon. `--role` is explicit and may be `owner`, `reviewer`, or `recovery`.
Recovery cannot approve ordinary Document candidates.

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
