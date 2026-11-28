# MCP tool reference

The MCP server (`cruxible mcp`) is a client of the daemon's HTTP API, the same
API the CLI and the Python SDK use; every tool runs on the daemon. Tool names
follow the CLI command path: `cruxible proposal approve` is
`cruxible_proposal_approve`. A few tools with no CLI command keep their own
names (`cruxible_server_info`, `cruxible_query_spec`).

`CRUXIBLE_MCP_PROFILE` takes two values. `default` (or unset) advertises the
everyday agent loop:

- read with three verbs: `cruxible_orient` (the map of accepted state,
  one kind in full, or one paged section), `cruxible_query` (any
  question as rows of values with verdict flags) and `cruxible_get`
  (one thing by any reference; `detail` picks summary, evidence, why, history,
  a Subject's incoming edges or pairs, a Line's runs, backlinks, proof, or a Document body range);
- the work queue: `cruxible_next`;
- the write verbs: `cruxible_set` (one value in one field, replacing
  the live value), `cruxible_retire` (end one live Claim), and
  `cruxible_write` (create, set, add and retire changes as one change set);
- proposals through activation: `cruxible_proposal_list`,
  `cruxible_proposal_review`, `cruxible_proposal_approve`, and
  `cruxible_proposal_activate`;
- identity and versions: `cruxible_whoami` and `cruxible_server_info`.

To find something by name, grep the floor (`.cruxible/floor/`, which
`cruxible floor export` and `cruxible_floor_export` write)
and `get` the ref a hit names; an agent without a shell searches values with
`cruxible_query` and `contains`.

`full` advertises the complete catalog below, including the `authoring_*`
intent tools, `cruxible_query_spec`, `cruxible_since`,
coverage, the floor, files, blocks, kits, Procedures, Lines, and the
split approval pair
`cruxible_proposal_approve_prepare` and `cruxible_proposal_approve_submit` for
a signer outside the MCP process. `CRUXIBLE_MCP_TOOLS` (a comma-separated
list) narrows the advertised set further to exactly those tools. Profiles and
the list change discoverability only: the server's tier (`CRUXIBLE_MODE`, below)
hides tools above it, and permission tiers still gate every call. Every tool
not in the `default` list above is in `full` only, including `cruxible_init`,
`cruxible_kit_add`, `cruxible_authoring_submit_file`, the Line, Runbook and
inbox tools, `cruxible_capture_read`, `cruxible_proposal_readmit`,
`cruxible_proposal_withdraw` and the vocabulary tools. An agent that will
install a kit, submit an automation template or run automation needs
`CRUXIBLE_MCP_PROFILE=full` in its MCP server's environment. There is no
separate version tool: `cruxible_whoami` and `cruxible_server_info`
both report the MCP adapter's package version and the daemon's (`GET /version`).

**Argument names.** Each tool's schema (from `tools/list`) names its
arguments, and some differ from the CLI flags. A preview-first tool commits
only with `dry_run: false` (with `at`, the preview's git oid, where it takes
one); there is no `commit` argument, so `cruxible_kit_add` previews (status
`would_propose`) until `dry_run` is `false`. `cruxible_kit_remove` and
`cruxible_capture_read` take their arguments nested under `request`.
`cruxible_runbook_preview` and `cruxible_runbook_run` take the Runbook's name as
`runbook`. A tool refuses an argument its schema does not declare
(`cruxible.mcp.argument_unknown`) and lists the arguments it takes, so a
misspelled one (`commit: true`, `name: ...`) never runs with defaults:

~~~text
cruxible.mcp.argument_unknown: tool 'cruxible_kit_add' takes no argument 'commit'; its arguments: instance_id, reference, bundle, keep, keep_local_edits, retire_dependents, keep_build, allow_downgrade, detail, dry_run, at
~~~

## The daemon

Every tool runs on a Cruxible daemon; the MCP server is a client of it and never
serves state in its own process. It picks the daemon in this order:

1. `CRUXIBLE_SERVER_SOCKET` or `CRUXIBLE_SERVER_URL` in its own environment;
2. the transport the MCP workspace's `.cruxible/coverage.json` binds together
   with an instance;
3. the default socket, `~/.cruxible/run/daemon.sock` (under `CRUXIBLE_STATE_ROOT`
   when that is set), when a daemon answers there;
4. otherwise it starts one under that state root, holding
   `<state root>/run/autostart.lock` so two MCP servers never start two: a live
   daemon already serving the state root is reused, an installed user service
   (`cruxible server install-service`) is started, and with neither it runs
   `cruxible server start --socket <default socket>` detached, writing its
   output to `<state root>/run/daemon.out`. The daemon outlives the MCP server.

A transport from 1 or 2 that does not answer is refused by name; the server never
starts a daemon in its place, whichever socket a workspace binding names (the
default one included). When no daemon can be started, the call fails with
`cruxible.mcp.daemon_unavailable` naming the failed step and the repair: start
one with `cruxible server start`, install the service, or set a transport. A
started daemon inherits an allowlist of the MCP server's environment: process
basics (`PATH`, `HOME`, user, shell, temporary directories, locale, `TZ`, the XDG
directories, `VIRTUAL_ENV`, `PYTHONPATH`), proxy and certificate settings, uv's
cache and index URLs, `GIT_SSH_COMMAND`, and daemon configuration. Nothing else
reaches it: not the server's transport, tier, instance or principal, and no
credential (Cruxible tokens and keys, API keys, cloud or forge tokens).

The MCP server, the CLI and the SDK each ask the daemon's `/version` once per
process before their first real request. The same authoring contract snapshot
digest proceeds silently whatever the package versions. A different digest in
the same `major.minor` release series (a 0.6.0 client and a 0.6.1 daemon)
still serves reads (orient, query, get, since, next, whoami, server info,
proposal list and review, inbox list and get, capture read, floor export), and
refuses each write before sending it. A daemon in another release series, or
one that reports no digest, refuses every call. A refusal is
`cruxible.sdk.daemon_version_incompatible` in one line naming both versions,
both digests, where the daemon is and the fix (stop it with `cruxible server
stop` and rerun, or use the daemon's version). `cruxible --version`, `--help`,
`server stop` and `context show` never refuse. `CRUXIBLE_SKIP_VERSION_CHECK=1`
skips the check, for development only. `cruxible --version` prints the version,
the contract digest's first 12 hex and, in a source checkout, the commit.

The MCP server logs warnings and errors on stderr. `FASTMCP_LOG_LEVEL`
(`DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`) picks another level, and
`CRUXIBLE_DEBUG` turns on `INFO`, which logs each daemon request and tool
call.

## Instances and the adapter environment

**Loading a principal's `cruxible.env` without copying tokens.** Point the MCP
server at the file `cruxible init` or `cruxible principal add` wrote with
`--env-file DIR/cruxible.env` (on `cruxible mcp` or `cruxible-mcp`) or
`CRUXIBLE_ENV_FILE=DIR/cruxible.env` in the MCP client config's `env` block.
Only allowlisted `CRUXIBLE_*` variables are read; process environment variables
always take precedence; a file holding `CRUXIBLE_SERVER_BEARER_TOKEN` must be
owner-only (`0600`) and never a symlink. Keeping the bearer token inside the
`0600` file avoids pasting secrets into MCP client JSON.

Every tool that acts on one instance takes an optional `instance_id`. Omitted,
it defaults to `CRUXIBLE_INSTANCE_ID` in the MCP server's own environment (set it
in the `env` block of the MCP client config, or via `CRUXIBLE_ENV_FILE`), and then to the instance the MCP
workspace's `.cruxible/coverage.json` binds, provided the binding names the
server's daemon; a binding on another daemon than the environment's is refused.
The server never reads remembered CLI context. With none of these, the call fails and
names `CRUXIBLE_INSTANCE_ID`. `cruxible_whoami` returns the instance it resolved together
with the caller's identity there.

`CRUXIBLE_PRINCIPAL_ID` in the same `env` block names the principal the MCP
server acts as; it is sent with every request, and the daemon checks it is a
registered, active principal before any write it attributes to it (reads stay
open). With daemon auth off
it is a claim of identity, not authentication (`authenticated: false` in
`whoami`): every process of the same OS user is equally trusted. `whoami` also
reports `can_author` and, when false, the `authoring_refusal` (code, detail and
repair) that authoring would return.

**The folder's identity record.** On one machine the person and an agent started
in their project folder cannot be told apart, so `cruxible init` (and
`cruxible workspace attach`, when the CLI knows who acts) writes
`.cruxible/identity.json` beside the binding: the owner's principal ID, its
instance ID and the absolute path of its key directory, which stays outside the
project. It never holds a key or a token; committed by accident it reveals a
principal ID, an instance ID and one local path (it is in Git's `info/exclude`
like the binding). With none of `CRUXIBLE_PRINCIPAL_ID`, `CRUXIBLE_PRINCIPAL_KEY`,
`CRUXIBLE_MCP_KEY_DIR` or a bearer token in its environment, the MCP server
started in that folder acts as that principal for attributed writes,
`cruxible_proposal_approve` and `cruxible_claim_attest`, and the CLI there does
the same when its remembered context names no principal for the instance. Any
of those variables takes precedence, so each agent session of a team can still
be its own principal. The record is read only at the workspace root the shared
resolver chooses and only over a local daemon socket; a TCP daemon ignores it.
Before acting on it the server checks the principal is active on the instance and
the key in that directory is its registered key; otherwise it refuses with
`cruxible.identity.folder_record_invalid`, `folder_key_unavailable`,
`folder_key_mismatch` or `folder_principal_unknown` and the repair, never falling
back to `operator`. The repair fits the case: restore a missing or exposed key at
the recorded path (mode 0600, in a 0700 directory); for a key or principal the
instance does not hold, record the right principal by attaching with it and its
key (`CRUXIBLE_PRINCIPAL_ID=ID CRUXIBLE_PRINCIPAL_KEY=/abs/keys/ID.ed25519 cruxible
workspace attach --replace`) or remove the record;
for a record that is not this folder's, remove it or record it again. The
environment variables above override the record in every case. `whoami`'s `identity_source` says which applied:
`environment`, `folder` or `none`.

So an MCP server started in an initialized project acts as its owner by
default. A reviewer's server is a second server whose `env` block sets
`CRUXIBLE_PRINCIPAL_ID=reviewer` (or `CRUXIBLE_ENV_FILE=/path/to/reviewer/cruxible.env`);
nothing else is needed on the machine where `cruxible init
--require-independent-approval` or `cruxible principal add` made the reviewer,
because its key is found from its remembered settings (init keeps it in
`$XDG_CONFIG_HOME/cruxible/keys/INSTANCE/reviewer`, by default
`~/.config/cruxible/keys/INSTANCE/reviewer`, or the `--reviewer-key-dir DIR`
given). Cosign between agents on one machine is a convention, not a
security boundary: any process of the OS user can read both keys.

`CRUXIBLE_MCP_WORKSPACE_ROOT` selects the client-owned workspace for tools that
read or write local files. The stdio MCP process is the client-side adapter; the
workspace defaults to its working directory.
All tool paths are normalized relative paths confined under that root; the
daemon receives bytes and typed observations, never a client filesystem path.
Floor operations always target the containing Git worktree's canonical
`.cruxible/floor`. With no explicit workspace root, the adapter discovers that
worktree from its working directory. An explicit `CRUXIBLE_MCP_WORKSPACE_ROOT`
must equal the worktree root for floor export and status; a nested explicit
root is refused rather than allowing a write above its configured filesystem
boundary. `cruxible_proposal_activate` is a daemon act and writes nothing
locally: the daemon's floor-refresh trigger delivers the floor to a workspace it
serves, and `cruxible_floor_export mode=write` pulls it elsewhere.

`CRUXIBLE_MCP_KEY_DIR` names the directory of local approval keys that
`cruxible_proposal_approve` signs with: an absolute path outside the workspace
holding `<signer_id>.ed25519`, the layout `cruxible principal add
--key-dir` and `cruxible init --key-dir` write. Set it in the MCP
server's environment; no tool argument can name a key path. The tool signs as
`signer_id`, else as the principal the server acts as, else as the directory's
only key. A server started with a principal's `cruxible.env`
(`CRUXIBLE_ENV_FILE`, `--env-file`, or `CRUXIBLE_PRINCIPAL_ID` and
`CRUXIBLE_PRINCIPAL_KEY`) needs no key directory:
the key is found by the lookup the CLI and the SDK use (see `principal add` in
the [CLI reference](cli-reference.md)), never another principal's. The order
is: `<signer>.ed25519` in `CRUXIBLE_MCP_KEY_DIR`; `CRUXIBLE_PRINCIPAL_KEY`
(never when `CRUXIBLE_PRINCIPAL_ID` names another principal); the settings
remembered for that principal on this instance; the folder identity record,
if it names that principal; the default key directory `cruxible init` uses.
With no key found, the tool refuses and says how to configure it:

~~~text
cruxible_proposal_approve: no signing key for principal nobody here; repair: start this MCP server with CRUXIBLE_ENV_FILE=DIR/cruxible.env (or --env-file DIR/cruxible.env), or set CRUXIBLE_PRINCIPAL_ID and CRUXIBLE_PRINCIPAL_KEY (as its cruxible.env sets them) in the env block of the MCP client config, or set CRUXIBLE_MCP_KEY_DIR to the directory holding nobody.ed25519. A remote signer uses cruxible_proposal_approve_prepare and cruxible_proposal_approve_submit (profile full)
~~~

Passing the reviewed `candidate_digest` binds the approval to that candidate:
if accepted state moved and the daemon proposed the change again at the head,
the tool refuses `cruxible.approval.reviewed_candidate_changed`, naming the
new proposal and whether its members are identical, and signs nothing; review
that proposal and approve it. Without `candidate_digest`, a proposal proposed
again at the head with identical members is signed in the same call (the
receipt names the replaced proposal in `readmits`). The replaced proposal
reads `settled` with `terminal_reason: superseded`, `superseded_by` and
`closed_by` in `cruxible_proposal_list` and `cruxible_get`. A rejected or
withdrawn proposal carries `closed_by`, `closed_at` and `closed_reason` (the
reviewer's or author's reason, whole) in the same two reads, so its author can
read why. `cruxible_get` takes a proposal id with or without `sha256:`, whole
or as a unique prefix of at least 8 hex characters.

~~~text
cruxible.approval.reviewed_candidate_changed: proposal sha256:5ed08a4a… was proposed again at the head as sha256:04bf2c9f… (candidate sha256:391b803f…), not the reviewed sha256:612bcb60…; members identical: true; nothing was signed. Review sha256:04bf2c9f… and approve it Repair: cruxible_proposal_review(proposal_id="sha256:04bf2c9f…")
~~~

`cruxible_init` takes one argument, `principal_id`, and creates the owner only, under
`self_approval_allowed`: cosign setup on an existing instance (a reviewer
principal and no self-approval) is two CLI steps, in this order: `cruxible
principal add reviewer --key-dir DIR --signer-key OWNER_KEY --mode graph_write`
(the tier that may submit approvals when the daemon runs with auth), then submit the
approval policy (`cruxible authoring example approval-policy > policy.json`,
`cruxible authoring submit policy.json`; its mode is
`independent_approval_required`). Add the reviewer first: once the policy
lands, the owner can no longer land a change alone. A new instance gets both
at creation with `cruxible init --require-independent-approval`, which
creates the reviewer's key and settings beside the owner's
(`$XDG_CONFIG_HOME/cruxible/keys/INSTANCE/reviewer`; `--reviewer-key-dir DIR`
puts them elsewhere); on an initialized instance that command answers
"already initialized" and adds nothing.
Set `CRUXIBLE_PRINCIPAL_ID` beside it: the key directory makes the
environment the identity source, so with no principal ID the server's
requests name no principal, and submitting the approval refuses
`cruxible.identity.principal_unconfigured` (as do activate, reject, withdraw
and readmit).
Only the public attestation leaves the process; key bytes never appear in a
result or a log line.

## Which daemon operations each tool publishes

The served-surface snapshot under `tests/goldens/playbill/` is the
machine-readable inventory of the whole served surface. Its `surface.mcp_tools` rows carry
`client_operations`: the daemon client operations each tool reaches, per tool.
MCP reaches state only through the daemon, so each operation is an HTTP route,
and that route's row in `surface.http_routes` names the facade verbs it reaches.
A deployment that decides what may be reached over MCP reads this join, or
enforces at the daemon's routes, rather than inferring it from the
`cruxible_<verb>` / `handle_<verb>` spelling, which nothing guarantees.

The list is a reachability closure, not a read of the handler's own body: it
covers the operations the handler calls itself, those of a sibling handler it
delegates to, and those of the shared client-side code it hands the client to
(block repin, the next-workspace observation, source compilation). An empty list
means the tool reaches no daemon operation at all -- one tool is in that position
today, `cruxible_authoring_example`, and it is `READ_ONLY`. A mutating tool may
not publish an empty list without a declared exception naming its reason
(`tests/test_guardrails/test_playbill_v1_served_surface.py`), because an
overlay reading `[]` as "reaches nothing" would fail open.

Every row is covered by the snapshot's `succession.surface_digest`, so a tool
that starts reaching one more operation moves the pin.

## Runtime

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_server_info` | Return the adapter and daemon versions with daemon metadata; an instance-scoped credential gets its own instance's host and identity instead of a refusal | `READ_ONLY` |
| `cruxible_init` | Make the workspace folder an instance over the local daemon socket, as a plain `cruxible init`: create the host attached to the folder, the binding, the owner principal (`principal_id`, its only argument, default your OS username) and its key in the default key directory outside the folder, and the folder identity record; this session then authors as the owner. The policy is `self_approval_allowed`; a reviewer and independent approval are CLI steps (see [the adapter environment](#instances-and-the-adapter-environment)). An initialized folder reports its instance (`status: already_initialized`) and changes nothing. Returns the instance, principal, key directory and approval policy, never key material | `ADMIN` |
| `cruxible_console` | Open the read-only console for the instance in the user's browser: the same loopback listener `cruxible console` serves, run on a background thread of this MCP server (one per instance; it ends with the server, or `stop=true` stops it). The tool opens the one-time sign-in link itself and returns only the address `http://127.0.0.1:<port>/i/<instance>`; the link is never returned. When no browser can be opened (`browser_opened: false`), a listener the call started is stopped (`status: no_browser`) and `message` tells the user to run `cruxible console` in their own terminal, which prints the link to them only. A second call reuses the running listener and opens a fresh link | `READ_ONLY` |
| `cruxible_whoami` | Name the resolved instance, the credential-derived actor's identity and registration there, whether it can author (and the repair when not), the server's `mode_cap` and `effective_permission_mode`, and the adapter and daemon versions | `READ_ONLY` |

## Host and providers

Setting up a host (`cruxible init`, or `cruxible host create` to allocate one
without becoming its owner), releasing its worktree (`cruxible workspace
detach`), decommissioning an instance (`cruxible instance decommission`) and
rebuilding the `next` worker's state (`cruxible instance rebuild-next`) are
operator acts with no MCP tool. So are making a principal's key (`cruxible
principal add NAME --key-dir DIR`; `cruxible_principal_propose` proposes a
registration for a public key made elsewhere), registering a file in the file
catalog (`cruxible files add PATH`), and stopping the daemon (`cruxible server
stop`). An agent that needs one of these names the CLI step to its operator,
or runs it in a shell.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_provider_list` | List installed Provider builds (each with its package, version and operations), then the packages a configured daemon repository offers | `READ_ONLY` |
| `cruxible_provider_install` | Install a provider package by name (from the configured repository, else the provider index) or from staged wheel and lock digests, and register its Provider build and operations (each declaring the capabilities it serves, with its own carried shapes); lands at once when the approval policy requires no approval, otherwise stops at `awaiting_approval`; grants no execution permission. `request.move: true` also moves every live Line bound to another build of the provider onto this build in the same proposal (`move_only` narrows it to `Line:NAME`s), reporting `moved`, `automation_carries` and `stops` | `ADMIN` |
| `cruxible_provider_move` | Move Procedures to an exact build or the most recently registered live build; preview re-pins, carried disabled Lines and propose-only Mandates | `GOVERNED_WRITE` |
| `cruxible_provider_retire` | Retire an exact build with no live pinners, retaining its runtime environment | `ADMIN` |

## Kits

A kit is a release of definitions (ClaimTypes, SubjectKinds, CaptureContracts,
Contracts, capabilities, QueryDefinitions, Procedures and Documents) that
installs as a diff against the consumer: one proposed change set that
adds, replaces and retires definitions. A kit also bundles the provider packages
its Procedures run on (wheel, uv lock and path-dependency wheels; never source):
`kit_add` installs the missing ones first, under the install permission, and
proposes the definitions once they land (`awaiting_providers` while an install
awaits approval; the activation that lands it proposes them on its own). A kit can instead name a default provider for a capability
its Procedures' slots name (`delivery: index`): `kit_add` installs it by name
and exact version from the provider index unless live operations here already
serve and fit every slot (`satisfied`). A kit may require another kit
(`requires`, most often the base kit every instance starts with): `kit_add`
refuses `cruxible.kit.requires_missing` or `requires_unresolved` until it is
met, and `kit_remove` refuses `cruxible.kit.remove_required` while an
installed kit requires the kit. Activation and any approval stay the ordinary
steps.

`kit_build` requires governed-write access because checking its automation
template is an authoring preview performed as the caller's principal.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_kit_build` | Export the definitions under owned identity prefixes as one kit release; `providers` names provider packages staged with `cruxible_body_store`, bundled or (`delivery: index`) as a default installed by name (every Provider a carried Procedure pins must be named; pins to another installed kit's definitions are left out and recorded in `requires`); `files` names plain files in this adapter's workspace the release carries, listed in the manifest with sha256 | `GOVERNED_WRITE` |
| `cruxible_kit_status` | List installed kits, paths edited since install, kept divergences, provenance, provider packages with their delivery and install state, and (for registry kits) the latest available version (`offline` skips the check) | `READ_ONLY` |
| `cruxible_kit_add` | Install or upgrade a kit by `reference` (a kit directory or layout the adapter reads, or a registry reference it pulls) or `bundle`; always proposes, `keep`/`keep_local_edits`/`retire_dependents`/`allow_downgrade` decide divergences; lands at once when policy requires no approval; a commit stages and installs bundled providers, and a default provider nothing here implements, first (`ADMIN` for the install); the result carries the kit's automation template text (`automation_template_content`) when the adapter holds the kit's files (any `reference`), never installed | `GOVERNED_WRITE` |
| `cruxible_kit_remove` | Retire the definitions a kit owns (never what it only carried, nor its provider packages); keeps Contracts, capabilities, Documents and pair-endpoint ClaimTypes; a definition edited since install, or live Claims or definitions that depend on what it would retire, block it, and the refusal counts them by kind with the repair; a blocked commit records nothing; arguments nested under `request` (`kit_id`, `dry_run`, `at`); lands at once when policy requires no approval | `GOVERNED_WRITE` |

## Documents and proposals

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_body_store` | Store inert body bytes in CAS | `GOVERNED_WRITE` |
| `cruxible_document_propose` | Propose a canonical Document envelope | `GOVERNED_WRITE` |
| `cruxible_proposal_review` | A proposal as a reviewer reads it: each value change as a write prints it, with its role, the Captures it cites and their text, then one block per definition (Line, mandate, Runbook, Trigger); page with `changes_from` / `definitions_from` (the `changes_next` / `definitions_next` a page returns); `groups=true` adds the stored fields grouped by kind, `detail=true` the full review; a Procedure proposal's item reasons page with `item_reasons_after` / `item_reasons_limit` | `READ_ONLY` |
| `cruxible_proposal_approve_prepare` | Return the exact approval challenge | `READ_ONLY` |
| `cruxible_proposal_approve_submit` | Submit a public signed attestation; activates a candidate it completes unless `hold` | `GRAPH_WRITE` |
| `cruxible_proposal_approve` | Challenge, sign with the signer's local key (`CRUXIBLE_PRINCIPAL_KEY` or `CRUXIBLE_MCP_KEY_DIR`), and submit in one call; the approval that completes the candidate activates it unless `hold` | `GRAPH_WRITE` |
| `cruxible_proposal_activate` | Activate by compare-and-set; returns the activation receipt (the daemon delivers the floor) | `GRAPH_WRITE` |
| `cruxible_proposal_list` | List one page of open and terminal proposal evidence (`limit`, `cursor`) | `READ_ONLY` |
| `cruxible_proposal_readmit` | Recompile retained inputs at head, or readmit exact bytes | `GOVERNED_WRITE` |
| `cruxible_proposal_withdraw` | Retire an open proposal that will never activate | `GOVERNED_WRITE` |
| `cruxible_proposal_reject` | Reject another actor's open proposal as its reviewer, with a recorded reason; the author reads it as `closed_reason` in `cruxible_get` and `cruxible_proposal_list` | `GRAPH_WRITE` |

MCP never accepts a private key as a tool argument. `cruxible_proposal_approve`
signs client-side, in the MCP server process, with a key its environment
names; a signer elsewhere uses `cruxible_proposal_approve_prepare`
and `cruxible_proposal_approve_submit`. Only the public attestation reaches the
daemon.

## Accepted reads

Accepted state is read through `cruxible_orient`,
`cruxible_query` and `cruxible_get` (see
[Queries, orient and get](#queries-orient-and-get)). Documents are listed by
`orient(section="documents")` and read by `get("Document:<name>")`, its
`why`, `history` and `body` details; a body read needs `GOVERNED_WRITE` and
names the whole body's `body_digest`.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_capture_read` | Verify retained Capture evidence and read bounded material (`request.max_bytes` bounds it); `request.capture_digest` may be the full digest, a `CAP-<12 hex>` handle or a 12+ hex prefix unique among the Captures the write verbs resolve (cited, or retained and verifying). A restricted CaptureKind's Capture answers its inventory view and a `body_restricted` notice unless the caller holds the non-MCP `cruxible_body_read_restricted` (`GOVERNED_WRITE`) | `READ_ONLY` |
| `cruxible_capture_erase` | Erase one Capture's retained body (the bytes go; the envelope and an erasure record of who, when and `because` stay) when its CaptureContract authorizes erasure; irreversible, so it previews unless `dry_run=false` with `at` = the preview's `coordinate.digest` | `GRAPH_WRITE` |

## Sources

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_files_compile` | Read the workspace's catalogued files and derive a file bundle | `READ_ONLY` |
| `cruxible_files_check` | Check the catalogued files (or a compiled `bundle`) against accepted state | `READ_ONLY` |
| `cruxible_files_propose` | Compile the catalog and propose one file (`file`) as its Document; the adapter reads the file | `GOVERNED_WRITE` |

The catalog is discovered as every other command finds it
(`.cruxible/files.yaml` or `files.yaml`, plus `.cruxible/files.local.yaml`);
`catalog_path` names another. The adapter owns local path traversal and digest
construction, so an agent supplies a file catalog name, not compilation wire or file
bytes. A catalog entry with only `name` and `locator` is evidence-only: it can be
cited and covered but not proposed. No MCP tool adds a catalog entry: a write
that cites or creates a file catalogues it, and registering one otherwise is
the CLI's `cruxible files add PATH`.

## Principals

The principal registry is `orient(section="principals")`; one record is
`get("Principal:<id>")`.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_compiler_upgrade` | Propose an exact compiler transition; signed approval and activation use the ordinary proposal workflow. | `ADMIN` |
| `cruxible_principal_propose` | Propose a principal registration, rotation, revocation, or recovery | `ADMIN` |

## Subjects, ClaimTypes, and Claims

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_claim_type_propose` | Propose a governed predicate interface, or adopt a builtin (`builtin: semantic.alias` or `semantic.distinct_from`) | `GOVERNED_WRITE` |
| `cruxible_capture_contract_propose` | Propose authored evidence: `input: {name, shape}` with optional grade, replay, retention, erasable, max_bytes, selector_privacy and body_access (`open` or `restricted`, fixed with the name); omitted edit fields keep accepted values; lands when allowed (`accept: never` keeps the proposal) | `GOVERNED_WRITE` |
| `cruxible_claim_type_migrate` | Compose a ClaimType successor with dependent dispositions | `GOVERNED_WRITE` |
| `cruxible_claim_attest` | Sign and append a support, contradict, or unsure observation of the current exact Claim; `because` cites your own words and `evidence` takes one of write's evidence options (self, section with `own_words`, file, capture, contract), made into Captures before signing; with neither, it cites the Claim's own evidence. Never a proposal | `GOVERNED_WRITE` |
| `cruxible_set` | Put one value in one field of one Subject (`kind/id`), replacing the live value without its Claim ID; a missing Subject of a known kind is added; `evidence` defaults to `because` as self evidence (an exact-content value is its own evidence); accepts in the same call when policy and tier allow it, else answers `awaiting_approval` with the eligible approvers and the approve call; `dry_run` writes nothing; `at` refuses `cruxible.write.slot_changed` if the field moved since; `based_on` names the Claims the value was judged from (`CLM-...`, or `kind/id#field` for that field's one live Claim, resolved at the write's base to its `CLM-...` id) and pins each at its current version; each change carries its `verdict`, and a verdict other than `supported` comes with a warning and its repair | `GOVERNED_WRITE` |
| `cruxible_retire` | End one live Claim, by Claim ID or by Subject and single-value field, with its dependent Claims, in one change set | `GOVERNED_WRITE` |
| `cruxible_write` | Apply `create`, `set`, `add` (one more value in a many-valued field), `retire`, `rebind` and `about` (a note: link a section about Subjects) changes as one change set, accepted or refused together. A `create` (`label`, `subject_kind`, `key`: a one-key value, `{field: value}` for each key field, the full `[{predicate, value}]` form, or none for a minted kind) makes a Subject the other changes name `{"created": label}` in any order; a key one live Subject holds lands on it (`resolved: existing`) unless `expect_new`. File or section evidence cites a workspace file by path; one the catalog does not name is catalogued once the write is admitted (`catalogued` in the outcome). A `set` or `add` change takes `based_on` as `cruxible_set` does | `GOVERNED_WRITE` |
| `cruxible_link` | Link a section (`source` `FILE#Heading/Path`) `about` Subjects with `because` (a note, one write), or a whole registered page (`source` `FILE`) `guides` a ClaimType or Procedure (recorded on its catalog entry and proposed as its Document's next revision); `unlink` removes the named links. FILE is a path or a catalog name and must be in the file catalog: a page that is not refuses `cruxible.link.file_unregistered` (a section, `cruxible.doc.file_uncatalogued`) with the CLI repair `cruxible files add PATH`. Mismatched arguments refuse `cruxible.link.*`, saying which link is which | `GOVERNED_WRITE` |
| `cruxible_backlinks` | What points into one target: `ref` a Subject (`kind/id`) for the notes and Documents linked `about` it and the workspace sections its live Claims cite; a ClaimType or Procedure for the Documents linked `guides` it (review guidance); `file` and `section` for the live Claims resting on that section. Keyset pages (`limit`, `cursor`); each section row carries `drift` (`changed_since_cited`, `unchanged`, `unknown`) checked against the MCP workspace. Notes are written with `cruxible_link` or a `cruxible_write` `about` change, and read by handle with `cruxible_get(ref="NOTE-...")` | `READ_ONLY` |
| `cruxible_get` | Read one thing by any reference (Claim id or prefix, `kind/id` or `Subject:kind/id`, predicate (a dotted tail like `work_item.status` matches live predicates only; an exact predicate reads any lifecycle), `ClaimType:`/`Document:`/`Procedure:`/`Capability:NAME@N`/`Contract:NAME@N`/`query:`/`CaptureContract:`/`Trigger:`/`Principal:`/`Provider:NAME[/BUILD]`, `ApprovalPolicy:instance`, `ProcedureRuntimePolicy:instance`, artifact path, proposal id or 8+ hex prefix (with or without `sha256:`), a note handle `NOTE-...`, or an operational reference: `ProcedureRun:RUN-...`, `Line:<name>` or the Line identity digest `next` names, `CAP-<12+ hex>`/`Capture:<digest>`, `ResolutionContract:<name>`, `Mandate:<name>`; their operational parts are read live at the head whatever `at` names, and the answer marks them with `live: {as_of, fields}`); `detail` is `summary` (values-first card with verdict flags; a string value over 500 characters is a TruncatedText preview `{truncated: true, preview, length, read_whole}`, never the value, and an object value is shown whole as `{object: {...}}`, where `read_whole` is the exact `cruxible_get` call `{ref, detail, at}` that returns it whole at the generation it was previewed at, and Subject rows name each value's `claim`), `evidence` (with the whole value), `why` (a Claim's verdict and law evidence, or a Subject's or Document's governance and provenance), `history` (newest first, paged by `limit` and `cursor`: a Claim's revisions each with `change` -- `stated`, `changed`, `reobserved` or `retired` with its `retirement` -- and `reason`, its Procedure item's reason or its change set's rationale; a Subject's `{kind: "subject", revisions, events}`, each event one field change with `before` for a changed value and `contended` for a competing one, `field` narrowing to one field), `incoming` (a Subject's pointing edges: one page, by `limit` and `cursor`, of the live Claims whose subject-valued object it is, each `{claim, subject, predicate, qualifier}` naming the asserting Subject), `pairs` (the pair Subjects a Subject is an end of: one page, by `limit` and `cursor`, of `groups`, each `{kind, part, pairs, hidden}` in pair kind, part and id order, listing only pairs holding a visible (supported, current, positive) fact and counting the rest as `hidden`; a page examines at most `2 * limit + 8` candidates, so it may hold fewer rows than `limit` while `truncated`; a summary card lists at most five per group with `more`, and a pair's card lists its `ends`), `runs` (a Line's Procedure runs, newest admission first, paged by `limit` and `cursor` over the run journal's per-Line index, read live), `backlinks` (what points at a Subject, ClaimType or Procedure, as `cruxible_backlinks`), `proof` (the full accepted envelope and facts, with the full `accepted_coordinate`; on a Claim a summary `{verdict, captures, full}`, each cited Capture on one line), `proof_full` (a Claim's whole envelope and facts), or `body` with a byte `range` and the whole `body_digest`; other answers carry a compact `coordinate` (12-hex git oid prefix and `generation`), either of which `at` accepts back (a history row carries both; an all-digit `at` of 11 or fewer characters is always a generation); evidence names Captures by `CAP-<12 hex>` handles; a wrong name refuses with the nearest names | `READ_ONLY` |

A proposal is not accepted state. A Claim's verdict is computed at read time
from accepted law evidence, never carried forward from acceptance.

`based_on` (compiler revision 74) records what a judgment rests on: each named
Claim is pinned at its exact version as a `based-on` pin. It is provenance, not
evidence, and never affects the verdict. Revising, retiring or contradicting a
pinned Claim leaves the judgment live; `cruxible_next` lists it as
`claim_dependency_stale` until it is set again with `based_on` (re-pinning the
current version), set with `based_on: []` (dropping the pins), or retired.
Leaving `based_on` out of a revision keeps the pins it had. A pinned Claim must
be live and not the one written (`cruxible.authoring.based_on_not_found`,
`based_on_retired`, `based_on_self`); `kind/id#field` naming a field with no
live Claim refuses `cruxible.write.based_on_not_found`, and one with several
`cruxible.write.based_on_ambiguous`, listing their ids. In `cruxible_write` the
field is read as the whole write leaves it, so a set or add of the same write is
the Claim pinned, and `label#field` names a field of a Subject a create change
labels; changes resting on each other refuse `cruxible.write.based_on_cycle`. An instance still on
revision 73 refuses `based_on` with
`cruxible.authoring.based_on_requires_compiler_upgrade`.

## Authoring intents

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_authoring_example` | Return a template for an authoring input: Claims, Subjects, SubjectKinds, named queries, Procedures, Contracts, capabilities, Lines, Triggers, mandates, approval and runtime policies, change sets and ClaimType successions | `READ_ONLY` |
| `cruxible_authoring_get` | Read one authoring intent | `READ_ONLY` |
| `cruxible_authoring_list` | List the caller's in-progress intents | `READ_ONLY` |
| `cruxible_authoring_compile` | Stage a payload as an intent (new, or revising `intent_id`) and run every check | `GOVERNED_WRITE` |
| `cruxible_authoring_bind` | Read an anchored selection of a catalogued workspace file (the stub's `source_id` must be its catalog name; `occurrence` picks one of several anchor matches), derive commitments, and compile | `GOVERNED_WRITE` |
| `cruxible_authoring_preflight` | Produce a binding certificate and repair frontier | `GOVERNED_WRITE` |
| `cruxible_authoring_submit` | Compile and submit a `payload` in one call, submit a staged `intent_id`, or both (revise, then submit); idempotent | `GOVERNED_WRITE` |
| `cruxible_authoring_submit_file` | Submit the authoring input file at a workspace-relative `path` (YAML or JSON, as `cruxible authoring submit FILE`), read beneath the workspace root without following a link, at most 1 MiB; `accept="never"` keeps the proposal | `GOVERNED_WRITE` |
| `cruxible_authoring_status` | Read the causal path to acceptance | `READ_ONLY` |
| `cruxible_block_repin` | Stamp or refresh one projection block by its page (`file`: the workspace path or the catalog name) and `block`; a block whose body is still its one query's rendering is rendered again; the adapter computes the stamp, registers the block and records the page's declared blocks for block-churn detection; `render: true` writes the body as a table or list from its one query backing | `GOVERNED_WRITE` |
| `cruxible_block_sync` | Check each projection block's backings (the CLI's `block sync --check`); edits no page; each row's repair names this server's tools and their arguments | `READ_ONLY` |
| `cruxible_block_detach` | Remove retired blocks' markers from pages, keeping the prose; `dry_run` previews, `at` pins the commit to the pages' bytes; status `unchanged` when no block can be detached (the rows say why) | `GOVERNED_WRITE` |
| `cruxible_block_depublish` | Release one block registration by `file_id`, the stored projection file identity; the file and catalog entry need not still exist | `GOVERNED_WRITE` |

The daemon mints every identity, digest, base, timestamp, and proposal reference.
It reports approval conditions but never obtains or impersonates an approval.

The flow is `compile` (or `bind`), then `preflight` as needed (a refused intent
whose base the head moved past advances when it checks clean at the head), then
`submit` and `status`, with `get` and `list` to find work again; a one-shot write
is `cruxible_authoring_submit` with a `payload`. `cruxible_authoring_example`
prints a template for any input kind.

Every payload is one tagless input, and the
`change_set` kind carries any mix of members -- `claim`, `claim_type`,
`claim_type_succession`, `claim_retirement`, `subject`, `subject_kind`,
`subject_succession`, `query_definition`, `procedure`, `procedure_mandate`, `acquisition_policy`, `line`,
`trigger` -- as one intent that admits or refuses whole, typed to the
offending member index. A `subject` member creates a Subject by its declared
kind and key (`{"kind": "subject", "subject_kind": ..., "key": {...}}`), never by
id: core assigns the id and the result reports it; a key a live Subject holds
names it instead. Every new Subject's kind needs a `subject_kind` (keyed
`unique`/`hint`, or `minted`); `cruxible_authoring_example` with
`subject-kind` prints one, and the CLI reference's "Subject kinds and creation"
gives the full rule. `approval_policy` and `procedure_runtime_policy` parse
as members but a change set refuses them; send each as its own singleton input.
A one-key reference can use `{"subject_kind": "sec.package", "key": "Demo"}`;
composite keys name each part in a mapping. Claim subjects and Subject-valued
objects can also name a same-set creation as `sec.package/Demo`; core applies
the key's canonical form and the usual reference ambiguity rules.
There is no second batch tool.
A `claim_type_succession` member succeeds an accepted ClaimType and dispositions
its whole reverse-pin closure in the same generation, so vocabulary evolution
needs no second tool and no second generation either.
`cruxible_authoring_example` serves `change-set` and
`claim-type-succession` as starting points, and `procedure`, `line`,
`trigger` and `procedure-mandate` templates that are accepted
together. A `line` input's `parameters` is checked against its Procedure's input
contract at authoring; its optional `acquisition` map sets per-capture-output
rules (`<call>.<field>` to `max_age`, `on_unavailable`, `on_stale`, ...).

## Procedures

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_procedure_run` | Execute a directly runnable Procedure (`cruxible_get` shows `runnable: direct`) -- accepted-state reads, deterministic computation, and `source`/`call` occurrences through exactly pinned Providers -- at an explicit coordinate and time | `READ_ONLY` |
| `cruxible_procedure_measure` | Evaluate due Procedure measurements from real evidence, persist the resolution, and credit one run's exact grain | `GOVERNED_WRITE` |
| `cruxible_procedure_dispose` | Record an upheld/overturned measurement judgment with reason and authenticated reviewer attribution | `GOVERNED_WRITE` |
| `cruxible_procedure_readings` | Inspect measurement standing and retained exact-grain readings (read-only, paginated) | `READ_ONLY` |
| `cruxible_line_enable` | Enable a Line forward-only: its Triggers do nothing until then. The daemon admits what they match under the caller's credential, rechecked before each run, pinned to the current Line and Trigger versions. Needs governed write even for an observe-only Line; a proposing or settling Line needs a covering mandate. Repeating it unchanged returns `outcome: already_enabled`. Read it with `cruxible_get(ref="Line:NAME")`. | `GOVERNED_WRITE` |
| `cruxible_line_disable` | Stop a Line admitting work on its own; admitted runs keep going. A stopped enablement returns `outcome: already_disabled`. | `GOVERNED_WRITE` |
| `cruxible_line_run` | Run a Line once now as a manual occurrence under its own inputs, budgets, authority and mandate; it never consumes a Trigger. `event` is the event input for a Line whose Procedure takes one; an event its Triggers already admitted needs `repeat`. A Line that can propose or settle needs a mandate, an observe-only one none | `READ_ONLY` |
| `cruxible_runbook_run` | Run an accepted Runbook now: its steps in order, each Line as your manual run and each action fired. It stops at the first step that fails or sends writes to review (a proposal); later steps report `not_run`. Each step is gated as its own Line run or action fire. | `READ_ONLY` |
| `cruxible_runbook_preview` | (`runbook`: the Runbook's name) What each Runbook step can read, write, call, settle, propose and set off, and its order problems, from accepted definitions only: nothing runs and no outcome is predicted. | `READ_ONLY` |
| `cruxible_line_evaluate` | Evaluate a historical range (`since`, `until`) into pending work; never executes. `dry_run` only reports what the range makes eligible (a read, no range needed); enqueueing needs governed write. | `READ_ONLY` |
| `cruxible_line_dispatch` | Admit retained pending occurrences under the current caller's authority, up to `limit` (default 100) per call; follow `cursor` for more. | `READ_ONLY` |
| `cruxible_inbox_list` | List one page of the Line inbox as compact rows (id, Line, kind, title, status, seen count), newest postings first; filter by `line` and `status`, follow `next_cursor`; `cruxible_inbox_get` reads one whole. | `READ_ONLY` |
| `cruxible_inbox_get` | Read one inbox item whole: envelope (Line, node, dedupe key, occurrences, first and recent sightings with their source Captures, disposition), payload as posted (at most 65,536 bytes), and its 10 most recent dispositions (`disposition_count` says how many in all). | `READ_ONLY` |
| `cruxible_inbox_ack` | Acknowledge an inbox item: seen, owed nothing further. | `GOVERNED_WRITE` |
| `cruxible_inbox_dismiss` | Dismiss an inbox item with a recorded `reason`. | `GOVERNED_WRITE` |
| `cruxible_inbox_link` | Link an inbox item to the proposal (`{"kind": "proposal", "proposal_id": ...}`) or accepted Subject (`{"kind": "subject", "subject": "kind/id"}`) that records your judgment. | `GOVERNED_WRITE` |

`procedure_run`, `line_run` and `line_dispatch` are read-tier only for targets
that observe. A Procedure whose terminals can propose or settle, or a Line whose
runs can (its Procedure's capability capped by its `max_authority`), needs
`GOVERNED_WRITE` to run or dispatch, whichever path triggers it; the daemon
decides this per target, and a read-only caller is refused with
`PermissionDeniedError`.

Read-tier Procedure runs append receipted journal records, following the same
precedent as QueryDefinition runs. They never alter accepted state or grant
themselves a governed track record; promotion remains a separate governed act.

## Predictions

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_prediction_list` | Find the accepted predictions (resolution contracts) testing one Claim, by `claim` | `READ_ONLY` |
| `cruxible_prediction_propose` | Propose a governed resolution contract whose hypothesis is a Claim ID | `GOVERNED_WRITE` |
| `cruxible_prediction_settle` | Settle one prediction (`prediction_id`) from the Claim ID of an accepted observation (`observation`) | `GOVERNED_WRITE` |

`cruxible_prediction_settle` requires `prediction_id` and the accepted
observation Claim ID (`observation`). Its MCP form has no advanced `request`
parameter. Exact references and mandated terminal evidence remain available in
the SDK and HTTP `SettleRequest`; settlement remains full-profile only.

Every Claim version these tools need can be a plain Claim ID (`CLM-...` or
`Claim:CLM-...`); the daemon resolves its digests and accepting coordinate. The
exact reference, and settle's full `request` (exact contract reference, anchor
event, or terminal evidence), remain as the advanced form.

Prediction settlement records the activation and resolution in operational
exhaust; it does not create or mutate Claims, and it does not create a second
authority plane beside accepted state.

Settlement reads the observation Claim version's acceptance and that
version's own per-Claim re-checks; the identical bytes and row sets that keep
a Claim fresh do not settle a prediction. Freshness and settlement have known
limits in 0.6.0: see [what freshness does and does not do](evidence-and-admission.md#what-freshness-does-and-does-not-do-in-060).

## Queries, orient and get

`cruxible_query` takes exactly one mode: compact or a query `name`
(a full spec runs through `cruxible_query_spec`, in the `full`
profile, so the default tool stays small). Compact mode names a Subject
`kind` or a definition kind, and/or free text `contains`. The definition
kinds list every accepted definition of the ontology, live unless a
`lifecycle` filter is given, each read at the ontology's size (never a scan
over Claims or Captures): `ClaimType` (`predicate`, `subject_kinds`, `object`,
`cardinality`, `members`, `description`, `evidence`; `select` adds `role`,
`roles`, `stale_after`, `selector`, `on_conflict`, `captured_required` and
`kit`), `Procedure` (`name`, `runnable`), `Trigger` (`name`, `schedule`,
`target_kind`, `target`, `lifecycle`), `Line` (`name`, `procedure`,
`authority`, `lifecycle`, `enabled`, `triggers`, `triggers_total`),
`SubjectKind` (`kind`, `rule`, `keys`, `uniqueness`, `description`, `subjects`
-- its live Subjects -- `kit`, `lifecycle`), `CaptureContract` (`name`,
`version`, `lifecycle`, `source_kinds`, `grade`,
`admitted_by`, `required_by`: the live ClaimTypes admitting it, and those of
them that require a Capture), `ProcedureMandate` (`name`, `procedure`,
`grant`, `state`, `scope`, `change_kinds`, `condition`, `suspended`,
`lifecycle`, `expires_at`) and `QueryDefinition` (`name`, `parameters`,
`description`, `result_shape`, `lifecycle`); `select` adds `version` to
Triggers, Lines, SubjectKinds, mandates and QueryDefinitions. Their columns filter by type (integers
and `expires_at` also compare with `lt` .. `gte`) and order typed, nulls last. Each `where` filter is `{field, <operator>: value}` with one of `eq`,
`ne`, `lt`, `lte`, `gt`, `gte`, `in` (a list), `exists` (a boolean) or
`contains` (case-insensitive text); filters combine as all-of. An ordered enum
(`ordered_enum[...]`) compares and orders by declared rank; `lt` .. `gte` refuse
on an unordered one. `exists: true`
matches a field holding any live Claim, an explicit none included; `exists:
false` only a field with no Claim. Filter on a value (`eq`) to match real
values. A field is a
predicate's full name, its name after the `KIND.` prefix (`adoption_state`),
`subject_id`, or `alias.field` after `follow: [{field, as}]`; columns show that
short name unless it is itself another predicate's full name or a reserved name
(`subject_id`, `subject`, `kind`, `predicate`, `claim`, `flags`, `value.*`). Values are checked against the
ClaimType first: an unknown kind, field or enum member, or an operator that does
not apply, refuses with a code, the nearest valid names and a repair. `ne` means
no value equals, so a Subject without the value matches. `contains` alone
searches every live Claim value across kinds. Rows lead with values (an array
for a many-valued predicate or a contested slot) and carry `flags` (`stale`,
`contested`, `contradicted`, `uncovered`, `unsure_hold`); without `select` a kind
shows up to 12 predicates and names the rest in `notes`. Cells show each slot's
answer as `get` shows it (its accepted and conflicted Claims, or every live
Claim when resolution accepted none); `status` adds Claims resolution set aside
(`overturned`, `ineligible`) or withdrew (`retired`), and `claims: true` answers
each cell's Claims under `rows[].claims[<column>]` with `claim`, `value`,
`verdict`, `status` and `role`, so a slot's winner reads apart from its losers.
`subject`, `subject_id`, `flags` and `claims` are row metadata; a column with one
of those names is served as `value.<name>`. ClaimType rows name the
CaptureContracts their evidence rules admit, never digests. `receipt` records the
mode, the definition digest, the coordinate and the evaluation time (default
now). A named query takes `budgets` up to its definition's maximum, and
`receipt: "full"` adds `receipt.replay`: the engine result (the Claims each row
read, traversal paths, bound parameters, verdict) and its execution receipt.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_query` | Answer any question over accepted state in one call: compact (`kind` -- a Subject kind or a definition kind: `ClaimType`, `Procedure`, `Trigger`, `Line`, `SubjectKind`, `CaptureContract`, `ProcedureMandate`, `QueryDefinition` -- and/or `contains`, with `where` filters shaped by operator, `select`, one-hop `follow`, `order_by`, `status`, `claims`) or a query `name` with `params` (`budgets`, `receipt`); rows of values with `flags`, paged by `limit` and `cursor` | `READ_ONLY` |
| `cruxible_query_spec` | Run one full `QueryDefinitionSpec` inline (`spec`, `limit`, `cursor`, `at`, `evaluation_time`) with the same evaluation, rows, flags and paging as `cruxible_query`; `full` profile only | `READ_ONLY` |
| `cruxible_orient` | Map accepted state in one call, as a small index: each Subject kind with its live count, identity rule and field names (`fields`, with `more_fields` when cut), artifact counts, named queries, `you` (can this caller author, and why not), `attention` from the accepted-state part of the `next` queue (`next_scope: accepted_state`: `next_items` leaves out the rows `cruxible_next` adds from the workspace, such as drifted citations, modified Documents and stale blocks; with `enablements`: the instance's Line enablements by state and the stalled or stopped Lines by name, no daemon scope needed), and `next` suggestions written as MCP tool calls; `kind` reads one kind in full (each predicate's type, cardinality, enum members and accepted evidence as CaptureContract names) with sample Subject IDs, `section` pages `documents`, `procedures`, `claim_types`, `subject_kinds` (each live SubjectKind's `rule`, `keys` and `uniqueness`: how a new Subject of the kind is named), `queries`, `capabilities` (each capability with its effect, request and result Contracts), `operations` (the provider operations a slot can bind to, with what each serves), `contracts`, `principals`, `policies` (every live standalone or embedded governed policy), `generations` (the ledger timeline: accepted generations newest first, each `{sequence, git_oid, proposed_at, changeset_digest, proposal, proposer, approvals, because, members}`, paged by sequence), `graph` (the Subject graph: `graph.nodes` `{subject, kind}` for each live Subject, then `graph.edges` `{claim, source, predicate, qualifier, target}` for each live Subject-valued Claim, keyset-paged at one coordinate, with `graph.totals.subjects`), `pins` (each live definition's required pins `{source, role, target, target_digest, current}`, keyset-paged: read by target, who uses a contract, ClaimType or Procedure), `activity` (what just happened, newest first: one row per `event` -- generations accepted, Line occurrences and enablement changes, runs started and finished, Captures landed and erased, inbox items posted and decided -- each with its `at` and the `ref` that reads it, read live and keyset-paged), `sources` (each logical source runs have landed Captures from, cited or not, by name: `{logical_source, landings, latest, contracts}`, its landing count, latest landing with its run, Line and Capture, and contract versions, read live and keyset-paged, with `sources_unattributed` for landings whose record can no longer be read), or an operational family -- `runs` (Procedure runs, newest admission first, paged by an immutable key) or `running` (only the runs still running; read one with `cruxible_get(ref="ProcedureRun:RUN-...")` for its live progress), `lines`, `runbooks`, `captures`, `capture_contracts`, `predictions`, `mandates` -- which the map only counts under `artifacts` (`limit`, `cursor`); when the MCP workspace holds this instance's floor, `floor: {at, generations_behind}` says how far behind the head it is | `READ_ONLY` |
| `cruxible_since` | Read signed accepted ChangeSet members after a generation | `READ_ONLY` |
| `cruxible_next` | Rank outstanding repair work, each row with its exact next operation; observes the MCP workspace's floor and declared sources as `cruxible next` does | `READ_ONLY` |
| `cruxible_duplicates` | List possible duplicates of one kind (`kind`, `limit`, `cursor`): Subjects and values equal under a broad comparison but distinct under the declared rules; a pure, bounded read | `READ_ONLY` |
| `cruxible_audit` | Rank visible Claim verification work and record completed coverage | `READ_ONLY` |
| `cruxible_floor_export` | `mode=bytes` returns the greppable floor as base64 bytes; `mode=write` verifies and exactly replaces `.cruxible/floor` under the MCP workspace (status `unchanged` when it already holds this floor) and records the workspace `floor_output` profile, `include` too, exactly as `cruxible floor export` does, so the daemon's delivery exports the same parts (a daemon that delivers this workspace's floor is its only writer: over its socket it delivers now, over TCP `write` refuses); `mode=status` reports whether that floor is current, stale, or missing. The floor is `current/<kind>/<id>.yaml` (values first, one header line naming the ref), `current/<kind>/INDEX`, `ontology/` (the accepted definitions: kind cards and INDEX files), `changes/`, `sources/`, `projections/INDEX` and `manifest.json` (see [floor](cli-reference.md#what-is-in-the-floor)). `include=["discovery"]` adds the discovery cards and `coverage-manifest.json`. Grep it, then `cruxible_get` the ref for live verdicts; an agent without a shell uses `cruxible_query` with `contains` | `READ_ONLY` |
| `cruxible_vocabulary_export` | Write the accepted kinds and fields to `.cruxible/vocabulary.yaml` under the MCP workspace, as `cruxible vocabulary export` does: shorthand where it round-trips, the full ClaimType input form otherwise, every entry checked by lowering it back; `force` replaces a file that holds changes not in accepted state | `READ_ONLY` |
| `cruxible_vocabulary_plan` | What applying the vocabulary file (`file`, default `.cruxible/vocabulary.yaml`) would add, replace and retire, read at the export it names; the Claims an enum mapping moves come first, then the daemon's write-free preflight; writes nothing. The adapter reads the file and sends the change set, never a path | `READ_ONLY` |
| `cruxible_vocabulary_apply` | Submit the vocabulary file's changes as one change set (`rationale`; `accept: never` keeps the proposal); an edited field pins the digest its export read, so one changed since refuses at preflight | `GOVERNED_WRITE` |
| `cruxible_coverage_resolve` | Resolve working sources against accepted state, from `observations` you built or from a workspace file selection (`files`, `ranges`, inline `grep_results` text, or `whole_working_set`) bound by the file catalog; `bindings` (`path`, `source` as `external:NAME` or `ledger:PATH`) override it path by path | `READ_ONLY` |

`cruxible_next` renders each repair's `command` as the MCP tool call
that performs it (for example `cruxible_prediction_settle(prediction_id="RSC-...")`,
adding the observation's Claim ID), or none when its operands are local files.
A row or nested finding whose repair the session cannot perform -- its profile
does not advertise the tool that performs it, or its tier is too low -- stays in
the queue with `repair: null` and `repair_requires: {tool, tier, because,
profile?}` naming what running it needs, so `orient` attention and the queue
count it for every caller. A status facet keeps its
state either way, but drops a repair the session cannot perform and says
`repair_hidden: true` with the same `repair_requires`. The `default` profile
advertises neither `cruxible_prediction_settle` nor the Line tools, for example:
a stopped Line enablement still shows as `consumer_stalled`, its repair withheld with
`because: ["profile"]`. A session that cannot author on the instance at all (an
unbound credential, or a principal that is not configured, registered or
active) has every writing repair withheld with `because` including
`"authoring"` and `authoring_refusal` carrying the same code, detail and repair
`whoami` reports.

Lists that can outgrow one answer are paged. `query`, `orient` sections,
and `proposal_list` take `limit` and `cursor`; a cut page
carries top-level `truncated: true` and a `next_cursor` to pass back as `cursor`.
A cursor whose listing changed since its first page is refused as
`cruxible.list.cursor_stale`; list again without it.

Query execution is a read: a named query's `receipt: "full"` returns its
`playbill-query-execution-receipt-v1`. Qualifying direct reads, named query
runs, coverage delivery, and Procedure dependency resolution also append
idempotent per-artifact touches to the daemon-local operational store. A
`READ_ONLY` actor can therefore grow that store, but these records never alter
accepted state or any semantic/generation root. Audit likewise appends an
idempotent completed-run record, but audit never creates
qualifying consumption touches and never execute Procedures or emit repair
recommendations. The floor export returns bytes keyed by floor path;
materializing a directory is a client act.
Coverage resolution takes observations -- a declared logical-source binding and
the bytes the caller read -- rather than paths, so the daemon reads no client
filesystem. It changes no accepted state, and the evidence-index, overlay, and
manifest digests it returns reproduce the answer; it appends a consumption
receipt only when the daemon runs with `CRUXIBLE_CONSUMPTION_RECEIPTS=on`.

## Permission tiers

Read operations require read_only. CAS/proposal operations require
governed_write. Approval submission and activation require graph_write.
Principal changes, compiler upgrades and provider installs require admin.

`CRUXIBLE_MODE` (`read_only`, `governed_write`, `graph_write`, or `admin`, the
default) fixes the MCP server's own tier: tools above it are not advertised,
and every call is capped at it. The cap is enforced by the daemon: below
`admin` the server sends it with every request as a ceiling, and the daemon
serves the request at the lower of that ceiling and the request's own
authority, so under a cap below `graph_write` a `cruxible_set` or `cruxible_write` stays a
proposal it cannot activate, and `cruxible_kit_add`, `cruxible_kit_remove`
and `cruxible_provider_move` stay proposed below `graph_write`. A Line enabled
through a capped server keeps the cap for its later automatic runs. A call to
a tool above the cap refuses `cruxible.mcp.mode_insufficient`, naming the tier
it needs and the repair (raise `CRUXIBLE_MODE` and restart the server, or ask
someone whose server serves it). `cruxible_whoami` reports `mode_cap` and
`effective_permission_mode` (the lower of the credential's tier and the cap);
below `governed_write` it says the session cannot author
(`cruxible.identity.permission_insufficient`, repair: raise
`CRUXIBLE_MODE`).

The daemon capability ceiling and bearer credential tier both apply. A
Cruxible principal signature is an additional governance condition, not a
replacement for transport authorization. Under a `read_only` ceiling
`cruxible_line_run`, `cruxible_line_dispatch` and `cruxible_procedure_run`
refuse even for an observe-only target, since every run retains its journal and
Captures.

Workspace source tools take `root_aliases` as a list of `{alias, path}` records.
Coverage takes `bindings` as a list of `{path, source}` records. Duplicate aliases
or paths are refused. Named-query `params` and Procedure `input` use the vocabulary
and input contracts declared in accepted state, which the daemon validates. A `params`
value may be `null`, which binds an optional parameter explicitly; omitting it takes the
parameter's default.

Approve and activate on a proposal whose base moved behave as `cruxible proposal approve` and `activate` do ([proposal](cli-reference.md#proposal)): activation refuses `cruxible.approval.requirement_unsatisfied` until the approvals are in; a moved or late-signer proposal lands at the head first and receipts name its source in `readmits`; an already-accepted proposal refuses `cruxible.proposal.already_accepted`; while approvals remain, the approval receipt names `required_signers` and `eligible_signers`. Stale authoring/write and Procedure proposals recompile on approve or activate. A typed recompilation result names the fresh proposal and whether its members are byte-identical. `changed_members` lists up to 50 sorted paths marked added, replaced, or removed; `changed_members_truncated` marks a longer list. Review it normally; no approval carries. Providers are not run again.

CaptureContract input uses the closed shapes `http_response`, `procedure_output`,
`foreign_span`, `database_snapshot`. Defaults: `grade: observed`, `replay: attested`
for HTTP and `exact` otherwise, `retention: P30D`, `erasable: false`,
`max_bytes: 16777216`, `selector_privacy: direct_allowed`. Derived grade (a
provider derive's output) is declared on `procedure_output`, `http_response` or
`foreign_span`, with `json` or `csv` decoding. A `procedure_output` kind takes
only a pure operation's output, committed with its bytes as its source. The engine supplies components; clients author this compact
form. A rule's `capture_contracts` accepts names or
`{contract: NAME, logical_sources: [SOURCE]}`. Empty sources means any; a
restricted entry matches the external Capture's recorded source and cannot
admit a CAS Capture. Each served Capture admission account carries one
`decision`, with status, rule, admission mode and any refusal or repair hint.
