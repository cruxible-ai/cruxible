# Quickstart

Attach Cruxible to any Git repository, record a typed claim citing a passage in
your own docs, watch `cruxible next` wake up when that passage changes, and
browse the live State graph in your browser. Everything runs locally with no
LLM inside Cruxible.

### What you'll have by the end

- A local Cruxible instance attached to your repository, backed by a signed Git
  ledger and `.cruxible/world.pyi` Python type stubs.
- A starter vocabulary (either tailored to your repo by `/survey` or installed
  from the `project-state` kit).
- A cited Claim that automatically surfaces as `citation_drifted` in
  `cruxible next` when its underlying text changes.
- The local read-only Cruxible Console running in your browser, plus a clear
  path to two-principal cosign review and automated domain kits.

### What you'll do (~5 minutes)

1. Install Cruxible and initialize your repository.
2. Pick a starting vocabulary (`/survey` with your agent, or `project-state`).
3. Record a cited decision through the typed Python SDK.
4. Edit the cited passage and watch `cruxible next` flag only what drifted.
5. Inspect the State graph in `cruxible console`.
6. Turn on two-principal cosign review for agent writes.

### Prerequisites

Python 3.11+ and Git on Linux (x86-64) or macOS (Apple silicon). On Windows,
install Python and Cruxible inside WSL2 and run every command there.

---

## 1. Install Cruxible and attach your repository

~~~bash
uv tool install cruxible        # or: pip install cruxible
cd your-project
cruxible init --principal-id owner
~~~

`init` starts a local daemon if none is running (`cruxible server stop` stops
it), creates this project's instance with you as its owner under
`self_approval_allowed`, installs the base kit at once, and remembers the
connection so later commands in this folder need no flags.

What lands in `.cruxible/`, and what to commit:

| File | Written by | Commit? |
|---|---|---|
| `coverage.json` | `init`: this folder's daemon transport and instance | no: machine-local, added to `.git/info/exclude` |
| `identity.json` | `init`: the owner's principal ID and key directory path (never a key) | no: machine-local, added to `.git/info/exclude` |
| `world.pyi` | the daemon's floor delivery: the vocabulary as Python types | no: derived, added to `.git/info/exclude` |
| `floor/` | the daemon's floor delivery: one file per Subject, for grep | no: derived; its own `.gitignore` ignores it |
| `files.yaml` | the first file you catalogue | yes: the portable file catalog (`files.yaml.lock` is excluded) |
| `vocabulary.yaml` | `cruxible vocabulary export` | yes: your kinds and fields as one editable file |

In a Git repository the daemon also mirrors the ledger as the `cruxible-ledger`
remote (`cruxible-ledger/accepted` and `cruxible-ledger/proposals/*` in
`.git/config`, nothing to commit).

## 2. Pick a starting vocabulary

Choose either path: let your coding agent design a minimal vocabulary tailored
to your repo, or install a starter kit in one command.

### Option A: Have your agent `/survey` your repository (recommended for custom domains)

Install the agent plugin for Claude Code (or Codex, see
[For AI agents](for-ai-agents.md#connect)):

~~~bash
claude plugin marketplace add cruxible-ai/cruxible
claude plugin install cruxible@cruxible-plugins
~~~

Then ask your agent to run `/survey`. It reads your repo, notes, and docs,
proposes the handful of recurring kinds and fields worth holding as state, and
writes `.cruxible/vocabulary.yaml` (`cruxible vocabulary export`, `plan`,
`apply`).

### Option B: Start from a published kit (`project-state`)

To try the typed workflow right away on any software repo, install the
`project-state` kit (roadmap items, decisions, batches, tracks, and laws):

~~~bash
cruxible kit add project-state --commit
cruxible orient
~~~

A bare kit name resolves to the highest published release in
`ghcr.io/cruxible-ai/kits` (currently `project-state:2.0.0`) and pins it by
digest. Omit `--commit` whenever you want a dry-run preview first. Other
starter kits include `security`, `iot-fleet`, `supply-chain`, and
`legal-research` ([browse all kits](https://cruxible.ai/kits)).

## 3. Record a cited Claim through the typed Python SDK

Cruxible does not replace your Markdown docs or code. Prose stays where it
lives, and Claims cite the exact passages they rest on.

Suppose your repository has an `ARCHITECTURE.md` containing:

~~~markdown
# Architecture

## Database

We use Postgres 16 for primary transactional storage.
~~~

Run `cruxible python` to write a typed decision citing that passage (the CLI's
own interpreter includes `cruxible-client`, so nothing needs to be installed in
your project's virtualenv):

~~~bash
cruxible python - <<'PY'
from cruxible_client import Cruxible, FileEvidence

with Cruxible.connect() as cx:
    w = cx.world()  # typed against .cruxible/world.pyi
    w.dev.decision.define(slug="primary-database").set(
        standing="ratified",
        ruling="Use Postgres 16 for primary transactional storage.",
        evidence=FileEvidence(
            file="ARCHITECTURE.md#We use Postgres 16 for primary transactional storage."
        ),
    ).write(because="Recorded from ARCHITECTURE.md.")
PY
~~~

Read it back from the CLI (or via MCP / SDK):

~~~text
$ cruxible query dev.decision --select slug,standing,ruling
subject                        slug              standing  ruling                                              flags
dev.decision/primary-database  primary-database  ratified  Use Postgres 16 for primary transactional storage.  -
~~~

Invalid enum values (like `standing="active"` instead of `"ratified"`) or
unknown fields are refused deterministically before anything touches the
ledger. Run `cruxible python --check SCRIPT` to type-check agent scripts
against `.cruxible/world.pyi` before executing them.

## 4. Edit the cited passage and watch `cruxible next` catch drift

Now change that line in `ARCHITECTURE.md` to say:

~~~markdown
We migrated primary transactional storage to SQLite with Litestream.
~~~

Run `cruxible next`:

~~~text
$ cruxible next
repair  citation_drifted  Claim:CLM-a088996d...  next=cruxible.rebind
  repair: cruxible rebind CLM-a088996d... --citation 89984942d1a1 --file '<FILE#ANCHOR>' --because '<TEXT>'
  or: cruxible set dev.decision/primary-database dev.decision.ruling '<VALUE>' --role normative --evidence-file '<FILE#ANCHOR>' --because '<TEXT>'
  or: cruxible retire CLM-a088996d... --because '<TEXT>'
~~~

Every unaffected Claim in the instance stays current and silent; only the Claim
whose cited passage moved reopens, with the exact repair commands printed right
beside it. (Unrelated edits elsewhere in `ARCHITECTURE.md` do not trigger
drift.)

## 5. Inspect the State graph in the Console

~~~bash
cruxible console
~~~

This opens a read-only local UI in your browser (`127.0.0.1`) showing the
State graph, Type map, Evidence flow, open `next` repairs, proposals, and the
signed Git ledger. The link it prints signs one browser in once; Ctrl-C stops
it.

## 6. Turn on two-principal cosign for agents

Before letting coding agents propose high-stakes judgments, switch to
`independent_approval_required` so nothing an author proposes lands until a
second principal (`reviewer`, either a human or a second isolated agent
session) reviews and signs it.

On a fresh project, pass `--require-independent-approval` to `init`:

~~~bash
cruxible init --principal-id owner --require-independent-approval
~~~

`init` generates the `reviewer` key beside `owner` in your per-user config
directory (outside the repo). To upgrade the instance you just created instead:

~~~bash
cruxible principal add reviewer --key-dir ../reviewer-keys --signer-key OWNER_KEY --mode graph_write
cruxible authoring example approval-policy > policy.json
cruxible authoring submit policy.json
~~~

From then on, the reviewer inspects proposals with exact citations and signs
off:

~~~bash
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible next
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal review PROPOSAL_ID
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve PROPOSAL_ID --yes
~~~

Cosign between agents on one machine is a convention, not a security boundary
(both keys are files your OS user can read). See
[Review and governance](review-and-governance.md).

## If something goes wrong

- **The daemon warns `Provider lane unavailable` when it starts.** The daemon
  runs provider packages over a Unix socket under its state root, and that
  directory path is over the 57-byte budget with no `TMPDIR` (macOS) or
  `XDG_RUNTIME_DIR` (Linux) fallback. Set
  `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` on macOS or
  `XDG_RUNTIME_DIR=/run/user/$UID` on Linux (or pass a shorter `--state-root`)
  and restart the daemon.
- **A proposal does not activate.** Under `independent_approval_required`, the
  author's own `proposal approve` is refused
  (`cruxible.approval.creator_forbidden`): approve it as `reviewer` instead.
  `CRUXIBLE_PRINCIPAL_ID=reviewer cruxible next` lists what is waiting.

## Where to go next

- **Security triage & code audits:** The
  [Security walkthrough](security-walkthrough.md) imports an SBOM, pulls OSV,
  CISA KEV, and FIRST EPSS on a schedule, and runs the kit's `security-audit`
  skill under cosign review.
- **Connect your coding agents:** [For AI agents](for-ai-agents.md) covers the
  typed Python SDK, MCP (`uvx cruxible mcp`), CLI, and the five built-in
  skills (`survey`, `adopt-kit`, `create-state`, `automate-state`,
  `review-state`).
- **Build automated pipelines & custom Python providers:**
  [Lines and automation](lines-and-automation.md) shows how scheduled Lines and
  `@derive` providers keep observed facts fresh automatically.
- **Mental model & modeling habits:** [Concepts](concepts.md) and
  [Building team knowledge](building-team-knowledge.md) cover what belongs in
  typed state vs. Markdown prose.
- **Browse all starter kits:** [Kits](kits.md) and
  [cruxible.ai/kits](https://cruxible.ai/kits) (`project-state`, `security`,
  `iot-fleet`, `supply-chain`, `legal-research`, `car-parts`).
