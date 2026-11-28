# Quickstart

Attach Cruxible to any Git repository, record a typed decision tied to a
section of your docs plus a task that depends on it, edit the doc to see
Cruxible catch the drift and wake the dependent task, and inspect the live
graph in your browser. Everything runs locally with no LLM inside Cruxible.

### What you'll have by the end

- Cruxible attached to your repository, ready for coding agents and teammates
  to read and write shared state.
- A starter vocabulary (either tailored to your repo by `/survey` or installed
  from the `project-state` kit).
- A cited decision and a dependent task showing both automatic drift detection
  (`citation_drifted`) and cascade staleness (`claim_dependency_stale`) in
  `cruxible next`.
- The local read-only Console running in your browser.

### What you'll do (~5 minutes)

1. Install Cruxible and initialize your repository.
2. Pick a starting vocabulary (`/survey` with your agent, or `project-state`).
3. Record a decision citing a doc section, plus a task pinned to that decision.
4. Edit the doc, update the decision, and watch `cruxible next` catch drift and
   wake only the affected task.
5. Open the local Console.
6. Require a second reviewer for agent writes (optional).

### Prerequisites

Python 3.11+ and Git on Linux (x86-64) or macOS (Apple silicon). On Windows,
install Python and Cruxible inside WSL2 and run every command there.

---

## 1. Install and initialize

~~~bash
uv tool install cruxible        # or: pip install cruxible
cd your-project
cruxible init --principal-id owner
~~~

This creates a local Cruxible workspace for your repository and starts the
background service automatically. Run `cruxible server stop` anytime you want
to stop it.

<details markdown="1">
<summary>What did <code>init</code> create in <code>.cruxible/</code>, and where does state live?</summary>

Accepted state lives in Cruxible's own signed Git ledger under `~/.cruxible/instances/` (mirrored locally as a `cruxible-ledger` Git remote), not as raw files inside `.cruxible/`. Teams share an instance through a hosted or remote daemon ([Isolated deployment](isolated-deployment.md)) or a Git ledger mirror (`cruxible ledger set-mirror`).

| File in `.cruxible/` | Purpose | Commit? |
|---|---|---|
| `coverage.json`, `identity.json` | Machine-local connection and owner identity (never holds private keys) | No (automatically excluded in `.git/info/exclude`) |
| `world.pyi`, `floor/` | Generated Python type stubs and readable YAML mirrors of current state | No (derived and ignored) |
| `files.yaml` | Portable catalog of cited workspace files | Yes |
| `vocabulary.yaml` | Exported kinds and fields (`cruxible vocabulary export`) | Yes |

See [Concepts](concepts.md) for how the ledger works.

</details>

## 2. Pick a starting vocabulary

Choose either path: let your coding agent design a minimal vocabulary for your
repo, or install a ready-made starter kit in one command.

### Option A: Have your agent `/survey` your repository (recommended for custom domains)

Install the five Cruxible skills into Claude Code, Codex, Cursor, OpenCode, or
any other Agent Skills host:

~~~bash
npx skills add cruxible-ai/cruxible
~~~

Then ask your agent to run `/survey`. It reads your repo and docs, proposes the
handful of recurring kinds and fields worth holding as shared state, applies
`.cruxible/vocabulary.yaml`, seeds your first 5-10 real cited decisions and
links (so `cruxible console` opens on a live State graph of your own repo right
away), and spots where live reality checks could plug in later. (Optional
Claude Code and Codex plugin bundles are also documented in
[For AI agents](for-ai-agents.md#connect).)

### Option B: Start right away with `project-state`

To try the typed workflow immediately on any software repo, install the
`project-state` kit (roadmap items, decisions, batches, tracks, and laws):

~~~bash
cruxible kit add project-state --commit
cruxible orient
~~~

Omit `--commit` anytime you want a dry-run preview first. Other starter kits
include `security`, `iot-fleet`, `supply-chain`, and `legal-research`
([browse all kits](https://cruxible.ai/kits)).

## 3. Record a cited decision and a dependent task

Cruxible does not replace your Markdown docs or code. Prose stays where it
lives, while Cruxible holds the canonical current facts, cites the sections
they rest on, and tracks what depends on them.

*(In normal use your coding agent runs these writes for you through the skills
from Step 2 or MCP; running one directly here shows what happens under the
hood. The snippet below uses the `project-state` vocabulary from Option B; if
you ran `/survey`, substitute the kinds and fields it created.)*

Suppose your repository has an `ARCHITECTURE.md` containing:

~~~markdown
# Architecture

## Database

We chose PostgreSQL 16 over SQLite for the API service so multiple workers
can write concurrently with ACID transactions and native JSONB indexes.
~~~

Run `cruxible python` to record two things atomically: a short structured
decision citing `## Database` by heading path, and a roadmap task pinned
`based_on` that decision's ruling (the CLI's built-in interpreter already
includes the SDK, so nothing needs to be installed in your project's
virtualenv):

~~~bash
cruxible python - <<'PY'
from cruxible_client import Cruxible, SectionEvidence

with Cruxible.connect() as cx:
    w = cx.world()
    decision = w.dev.decision.define(slug="primary-database").set(
        standing="ratified",
        ruling="PostgreSQL 16 (concurrent multi-worker writes, JSONB)",
        evidence=SectionEvidence(
            file="ARCHITECTURE.md",
            section=("Architecture", "Database"),
        ),
    )
    pool_task = w.dev.roadmap_item.define(slug="connection-pool-tuning").set(
        task_title="Tune Postgres connection pool size",
        implementation_state="partial",
        based_on=[decision.pin("ruling")],
    )
    w.write(decision, pool_task, because="Initial ADR import")
PY
~~~

Query it back with a typed filter from the CLI (or via MCP / Python):

~~~text
$ cruxible query dev.decision --where standing=ratified --select slug,ruling
subject                        slug              ruling                                                 flags
dev.decision/primary-database  primary-database  PostgreSQL 16 (concurrent multi-worker writes, JSONB)  -
~~~

Unknown fields or invalid enum values (such as `--where standing=active`
instead of `ratified`) are refused with valid suggestions instead of silently
returning empty, and concurrent writes against a stale read are refused instead
of silently overwriting each other. Reusable questions can also be saved and
called by name (for example `cruxible query --name project.roadmap-membership`
in this kit, or multi-hop queries like `security.urgent_exposures`; see
[Asking questions](asking-questions.md)). Cruxible also keeps Python type stubs
in `.cruxible/world.pyi` so editors and `cruxible python --check` catch
mistakes statically.

## 4. Edit the doc and watch `cruxible next` cascade

Now update the `## Database` section in `ARCHITECTURE.md` to:

~~~markdown
# Architecture

## Database

We migrated the API service to SQLite with Litestream to cut infrastructure cost.
~~~

Run `cruxible next`, your attention queue for anything that drifted, expired,
contradicted an upstream claim, or is waiting for review:

~~~text
$ cruxible next
repair  citation_drifted  Claim:CLM-f2a3eeae...  next=cruxible.rebind
  repair: cruxible rebind CLM-f2a3eeae... --citation aedf9abd51ea --section 'doc-architecture.md-8f6366fd8e7a#Architecture/Database' --because '<TEXT>'
  or: cruxible set dev.decision/primary-database dev.decision.ruling '<VALUE>' --role normative --evidence-file '<FILE#ANCHOR>' --because '<TEXT>'
  or: cruxible retire CLM-f2a3eeae... --because '<TEXT>'
~~~

Cruxible does not guess what an edit means: it flags only the decision backed
by `## Database` and offers three explicit repairs so you or your agent can
choose:

- **`cruxible rebind` (still true):** The wording in the doc changed (for
  example, typo fixes or clarification), but the decision itself still stands.
  Re-pin the existing claim to the updated section without changing its value.
- **`cruxible set` (changed):** The underlying decision moved (as it did here,
  from PostgreSQL 16 to SQLite). Record the new value citing the updated
  section; the previous ruling stays in per-field history.
- **`cruxible retire` (no longer applies):** The claim is obsolete. End it with
  a reason so later sessions stop relying on it.

Now update `primary-database` to reflect the migration and run `cruxible next`
again:

~~~bash
cruxible python - <<'PY'
from cruxible_client import Cruxible, SectionEvidence

with Cruxible.connect() as cx:
    w = cx.world()
    w.dev.decision["primary-database"].set(
        standing="superseded",
        ruling="Migrated to SQLite with Litestream",
        evidence=SectionEvidence(
            file="ARCHITECTURE.md",
            section=("Architecture", "Database"),
        ),
        because="Storage migration in ARCHITECTURE.md",
    )
PY
cruxible next
~~~

~~~text
repair  claim_dependency_stale  Claim:CLM-154b61b5...  next=cruxible.claim.retire
  input changed: CLM-f2a3eeae...  current=0273d2bb37ee  used=239471e6de05  reasons=source_superseded
  repair: cruxible retire CLM-154b61b5... --because '<TEXT>'
  or: cruxible get CLM-f2a3eeae...
~~~

This is what plain Markdown cannot do:

- `citation_drifted` cleared as soon as the decision was re-cited.
- **`connection-pool-tuning` automatically woke up** as
  `claim_dependency_stale` because it was pinned `based_on` the Postgres
  decision, while any unrelated tasks stay silent.
- Because the agent skills (`npx skills add cruxible-ai/cruxible`) instruct
  every new coding-agent session to start with `cruxible orient` and
  `cruxible next`, the next agent sees this immediately instead of wasting a
  session tuning a Postgres pool you no longer run.

Every change also keeps its signed per-field history:

~~~text
$ cruxible get dev.decision/primary-database --detail history --field ruling
ruling
  seq 4  34d38f86aa45  proposed 2026-11-27T23:00:43Z  by owner  changed  PostgreSQL 16 (concurrent multi-worker writes, JSONB) -> Migrated to SQLite with Litestream  [CLM-5b6a6809...]
      reason: Storage migration in ARCHITECTURE.md
  seq 3  7aa4ce50cefd  proposed 2026-11-27T23:00:38Z  by owner  stated  PostgreSQL 16 (concurrent multi-worker writes, JSONB)  [CLM-5b6a6809...]
      reason: Initial ADR import
~~~

## 5. Open the local Console

~~~bash
cruxible console
~~~

This opens a read-only local UI in your browser (`127.0.0.1`) showing your State
graph, Type map, Evidence flow, open `next` repairs, proposals, and signed
history. Press Ctrl-C to stop it.

## 6. Require a second reviewer (optional)

By default, your own writes land immediately. When you want coding agents to
propose changes that wait for a second reviewer (a human or a separate
reviewing agent session), initialize a new repository with
`--require-independent-approval`:

~~~bash
cruxible init --principal-id owner --require-independent-approval
~~~

From then on, anything `owner` writes waits until `reviewer` inspects the exact
citations and signs off:

~~~bash
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible next
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal review PROPOSAL_ID
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve PROPOSAL_ID --yes
~~~

To add independent review to the repository you already initialized in Step 1,
see [Review and governance](review-and-governance.md).

## 7. Next gear: keep state aligned with reality

Steps 1 to 6 give you **Gear 1**: typed state, cited documents, cascade drift
detection on local edits, and optional second-party review. **Gear 2** keeps
state aligned with live reality:

- **Normative beliefs** hold decisions, hypotheses, baselines, and verdicts
  (backed by human or agent authority, hardened through review and signed
  attestation).
- **Automated observations** pull live facts from APIs, scanners, test runs,
  experiments, telemetry, or custom Python and ML models at **zero LLM
  tokens**.
- When reality shifts or contradicts a standing belief, `cruxible next` wakes
  only the affected claims so your team or agents re-evaluate what actually
  changed.

Two fast ways into Gear 2:

1. **Try a pre-built automated kit:** run `cruxible kit add` +
   `cruxible kit adopt` on `security`
   ([Security walkthrough](security-walkthrough.md)) or `iot-fleet`
   ([IoT fleet walkthrough](iot-fleet-walkthrough.md)).
2. **Automate your own repo's signals:** run `/automate-state` with your coding
   agent on the reality-alignment candidates `/survey` identified
   ([Lines and automation](lines-and-automation.md)).

<details markdown="1">
<summary>Troubleshooting</summary>

- **Warning: `Provider lane unavailable` on startup.** Provider packages run
  over a Unix socket under Cruxible's state directory; if your home path is
  long and no temp runtime directory is set, run with
  `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` on macOS or
  `XDG_RUNTIME_DIR=/run/user/$UID` on Linux and restart the daemon.
- **A proposal will not activate.** Under independent approval, the author
  cannot approve their own proposal: run `CRUXIBLE_PRINCIPAL_ID=reviewer
  cruxible next` and approve as `reviewer`.

</details>

## Where to go next

- **Project state across coding-agent sessions:** The
  [Project state walkthrough](project-state-walkthrough.md) seeds a cited
  decision and dependent roadmap items, demonstrates both citation drift and
  selective dependency staleness, and installs the `project-state` skill.
- **Security triage & code audits:** The
  [Security walkthrough](security-walkthrough.md) imports an SBOM, pulls OSV,
  CISA KEV, and FIRST EPSS on a schedule, and runs the kit's `security-audit`
  skill under cosign review.
- **Deterministic IoT telemetry & anomaly triage:** The
  [IoT fleet walkthrough](iot-fleet-walkthrough.md) runs an EWMA telemetry Line
  over an edge camera hub, absorbing spikes at zero LLM tokens and waking
  `cruxible next` only on sustained faults.
- **Connect your coding agents:** [For AI agents](for-ai-agents.md) covers the
  typed Python SDK, MCP (`uvx cruxible mcp`), CLI, and the five built-in
  skills (`survey`, `adopt-kit`, `create-state`, `automate-state`,
  `review-state`).
- **Mental model & modeling habits:** [Concepts](concepts.md) and
  [Building team knowledge](building-team-knowledge.md) cover what belongs in
  typed state vs. Markdown prose.
- **Browse all starter kits:** [Kits](kits.md) and
  [cruxible.ai/kits](https://cruxible.ai/kits) (`project-state`, `security`,
  `iot-fleet`, `supply-chain`, `legal-research`, `car-parts`).
