# Project state walkthrough

Keep agreed scope, architecture decisions, and roadmap progress in governed
state so coding agents pick up where the last session left off instead of
trusting stale `CLAUDE.md` notes or compacted chat history. When an architecture
doc changes or a decision is superseded, `cruxible next` wakes only the work
items that depend on it.

### What you'll have by the end

- A project-state instance running `project-state:2.0.0` (8 SubjectKinds, 40
  ClaimTypes, and the `project.roadmap-membership` saved query).
- A ratified `dev.decision` citing a specific passage of `ARCHITECTURE.md` and
  two `dev.roadmap_item` Subjects, one pinned `based_on` the decision and one
  unrelated.
- Live proof of both automatic attention triggers: `citation_drifted` when the
  cited doc line moves, and selective `claim_dependency_stale` when the
  decision is superseded while unrelated work stays quiet.
- The **`project-state` skill** installed for your coding agent so future
  sessions plan from state, record agreements before coding, and reconcile work
  in one checkpoint.

### What you'll do

1. Initialize Cruxible in a Git repository and install `project-state:2.0.0`
   from GHCR.
2. Run Session 1 (`--seed`) to create `ARCHITECTURE.md`, a cited decision, and
   two roadmap items (one pinned `based_on` the decision).
3. Edit the cited line (`--edit-doc`) and watch `cruxible next` surface
   `citation_drifted`.
4. Run Session 2 (`--supersede`) to supersede the decision and watch
   `cruxible next` wake only the dependent roadmap item.
5. Install the `project-state` skill for your coding agent and inspect the
   graph in `cruxible console`.

### Prerequisites

Python 3.11+, Git, Linux (x86-64) or macOS (Apple silicon; Windows via WSL2),
and network access to `ghcr.io` (for `project-state:2.0.0`) and `github.com`
(to download the starter script and skill).

---

## 1. Initialize and install `project-state`

Create a fresh scratch repository (or run inside an existing repo) and install
the kit from `ghcr.io/cruxible-ai/kits`:

~~~bash
uv tool install cruxible        # or: pip install cruxible
mkdir project-state-demo && cd project-state-demo
git init -q -b main
cruxible init --principal-id owner
cruxible kit add project-state --commit
~~~

`cruxible kit add project-state --commit` resolves `project-state:2.0.0` from
GHCR and installs 8 SubjectKinds (`dev.track`, `dev.roadmap_item`,
`dev.decision`, `dev.batch`, `dev.card`, `dev.law`, `dev.recommendation`,
`dev.product_feedback`) and 40 ClaimTypes in a single generation.

Confirm the map with `cruxible orient`:

~~~text
$ cruxible orient
Cruxible inst_82d5592d285545ee generation=2 at 8ab799c8ad94 ...
Floor: .cruxible/floor at 8ab799c8ad94, current
You: owner, can author
Kinds (8):
  no subjects yet (8): dev.batch, dev.card, dev.decision, dev.law, dev.product_feedback, dev.recommendation, dev.roadmap_item, dev.track
Artifacts: claim_types=42 documents=2 queries=1 operations=2 capture_contracts=2 capabilities=8
Queries:
  project.roadmap-membership()  All retained project roadmap Subjects; watch membership so newly added work cannot silently disap...
Attention: next=0 open_proposals=0 (workspace rows not counted: next)
SDK: cruxible python SCRIPT (types: .cruxible/world.pyi)
~~~

Cruxible also generated `.cruxible/world.pyi` from the installed vocabulary so
`cruxible python --check` and your editor validate every kind name, field, and
enum value statically.

## 2. Session 1: seed a cited decision and two roadmap items

Download the 60-second session-handoff script from the
[project-state examples](https://github.com/cruxible-ai/cruxible-kits/tree/main/kits/project-state/examples/session-handoff):

~~~bash
curl -fsSLO https://raw.githubusercontent.com/cruxible-ai/cruxible-kits/main/kits/project-state/examples/session-handoff/demo_handoff.py
~~~

Inside `demo_handoff.py`, `--seed` uses the typed `0.6` World SDK to write one
atomic generation containing:

- `ARCHITECTURE.md` with a `## Database` section explaining why the API service
  uses PostgreSQL 16 (concurrent multi-worker writes, ACID transactions, JSONB).
- `dev.decision/primary-database` (`standing="ratified"`) citing that section by
  heading path via `SectionEvidence`.
- `dev.roadmap_item/connection-pool-tuning`, pinned in the same write via
  `based_on=[decision.pin("standing"), decision.pin("ruling")]`.
- `dev.roadmap_item/dark-mode-toggle`, an unrelated UI task with no dependency
  on the database decision.

~~~python
db_section = SectionEvidence(file="ARCHITECTURE.md", section=("Architecture", "Database"))

decision = w.dev.decision.define(slug="primary-database").set(
    standing="ratified",
    ruling="PostgreSQL 16 (concurrent multi-worker writes, JSONB)",
    evidence=db_section,
)
pool_item = w.dev.roadmap_item.define(slug="connection-pool-tuning").set(
    task_title="Tune Postgres connection pool size and idle timeout",
    adoption_state="adopted",
    implementation_state="partial",
    completion_criterion="p99 query latency under 20ms at 500 concurrent connections.",
    based_on=[decision.pin("standing"), decision.pin("ruling")],
)
ui_item = w.dev.roadmap_item.define(slug="dark-mode-toggle").set(
    task_title="Add dark mode toggle in user settings",
    adoption_state="adopted",
    implementation_state="not_started",
    completion_criterion="Setting persists across reloads.",
)
w.write(track, decision, pool_item, ui_item, because="Session 1: ...")
~~~

Run Session 1 and inspect the roadmap and queue:

~~~text
$ cruxible python demo_handoff.py --seed
Seeded generation 3: decision + 2 roadmap items.

$ cruxible query dev.roadmap_item --select slug,implementation_state,task_title
subject                                  slug                    implementation_state  task_title                                           flags
dev.roadmap_item/connection-pool-tuning  connection-pool-tuning  partial               Tune Postgres connection pool size and idle timeout  -
dev.roadmap_item/dark-mode-toggle        dark-mode-toggle        not_started           Add dark mode toggle in user settings                -

$ cruxible next
No repair work in the observed domains.
~~~

Everything is current and the attention queue is clean.

## 3. Between sessions: edit the cited passage

Suppose someone updates `ARCHITECTURE.md` to record a storage migration from
Postgres 16 to SQLite with Litestream without remembering to update the roadmap:

~~~text
$ cruxible python demo_handoff.py --edit-doc
Updated ARCHITECTURE.md#Database to SQLite with Litestream.

$ cruxible next
repair  citation_drifted  Claim:CLM-df47585e9742bbc20fe4bc2b719e64de  next=cruxible.rebind
  repair: cruxible rebind CLM-df47585e9742bbc20fe4bc2b719e64de --citation 756d9d7726e1 --section 'doc-architecture.md-8f6366fd8e7a#Architecture/Database' --because '<TEXT>'
  or: cruxible set dev.decision/primary-database dev.decision.ruling '<VALUE>' --role normative --evidence-file '<FILE#ANCHOR>' --because '<TEXT>'
  or: cruxible retire CLM-df47585e9742bbc20fe4bc2b719e64de --because '<TEXT>'
  fill in: <TEXT> (why, in your words), <VALUE> (the new value), <FILE#ANCHOR> (the file and anchor holding the new text)
repair  citation_drifted  Claim:CLM-357b36d25dbf1e891230594d14cd2544  next=cruxible.rebind
  repair: cruxible rebind CLM-357b36d25dbf1e891230594d14cd2544 --citation af991c7bc0ee --section 'doc-architecture.md-8f6366fd8e7a#Architecture/Database' --because '<TEXT>'
  or: cruxible set dev.decision/primary-database dev.decision.standing '<VALUE>' --role observation --evidence-file '<FILE#ANCHOR>' --because '<TEXT>'
  or: cruxible retire CLM-357b36d25dbf1e891230594d14cd2544 --because '<TEXT>'
  fill in: <TEXT> (why, in your words), <VALUE> (the new value), <FILE#ANCHOR> (the file and anchor holding the new text)
~~~

Because the decision cited that exact passage, the next agent session cannot
silently trust the stale Postgres summary: `cruxible next` flags both affected
Claims immediately with exact repair commands.

## 4. Session 2: supersede the decision and watch selective wake-up

Session 2 updates `dev.decision/primary-database` to `standing="superseded"`
citing the new SQLite line in `ARCHITECTURE.md`:

~~~text
$ cruxible python demo_handoff.py --supersede
Updated decision at generation 4. Run `cruxible next` to see affected items.

$ cruxible next
repair  claim_dependency_stale  Claim:CLM-1ea3b989ae9e77bc396e2ea7c3092e6b  next=cruxible.claim.retire
  input changed: CLM-2ffa1e5a0746d96a456eeab891b7f276  current=4ecde63b0f4f  used=df961b16ebc7  reasons=source_superseded
  input changed: CLM-660eb8243b9aecb91ca72a2b004a061e  current=bec928d29e25  used=01b06e03f3dd  reasons=source_superseded
  repair: cruxible retire CLM-1ea3b989ae9e77bc396e2ea7c3092e6b --because '<TEXT>'
  or: cruxible get CLM-2ffa1e5a0746d96a456eeab891b7f276
  or: cruxible get CLM-660eb8243b9aecb91ca72a2b004a061e
  fill in: <TEXT> (why, in your words)
... (4 rows total, all on dev.roadmap_item/connection-pool-tuning)
~~~

Notice what happened:

- The `citation_drifted` rows cleared as soon as the decision was re-cited.
- **`dev.roadmap_item/connection-pool-tuning`** woke up as
  `claim_dependency_stale` (`reasons=source_superseded`), naming the exact
  upstream decision Claims that changed so Session 2 knows to retire or rework
  the Postgres connection pool task.
- **`dev.roadmap_item/dark-mode-toggle`** never fired at all. Unrelated work
  stays quiet instead of forcing an agent to re-read the whole repo to figure
  out what changed.

Open the read-only Console to see the Subjects, edges, and generation history:

~~~bash
cruxible console
~~~

## 5. Install the `project-state` skill for your coding agent

The kit includes a dedicated agent skill
([skills/project-state/SKILL.md](https://github.com/cruxible-ai/cruxible-kits/blob/main/kits/project-state/skills/project-state/SKILL.md))
that teaches coding agents the four habits that keep project state ahead of the
work:

1. **Plan from state first:** check `cruxible orient`, `cruxible next`, and
   open `dev.roadmap_item` / `dev.decision` rows before touching code.
2. **Record agreements before coding:** capture scope rulings and acceptance
   criteria with `based_on` pins and file citations rather than leaving them in
   chat history.
3. **Type-check and dry-run writes:** run `cruxible python --check` against
   `.cruxible/world.pyi` and `dry_run=True` before landing state changes.
4. **Reconcile once per coherent milestone:** batch one checkpoint per completed
   feature, merge, or scope decision rather than creating noisy per-edit churn.

Install it with `npx skills` (works across Claude Code, Codex, Cursor, and 75+
agents), or download `SKILL.md` directly into `.claude/skills/` or
`.agents/skills/`:

~~~bash
npx skills add cruxible-ai/cruxible-kits --skill project-state
~~~

Or manually:

~~~bash
mkdir -p .claude/skills/project-state
curl -fsSL \
  https://raw.githubusercontent.com/cruxible-ai/cruxible-kits/main/kits/project-state/skills/project-state/SKILL.md \
  -o .claude/skills/project-state/SKILL.md
~~~

When you are ready to enforce independent review so coding agents propose state
changes and a second principal approves them, enable cosign as shown in
[Quickstart](quickstart.md#6-require-a-second-reviewer-optional) and
[Review and governance](review-and-governance.md).

## Where to go next

- [Building team knowledge](building-team-knowledge.md): deciding what belongs
  in typed fields versus prose documents, and lessons from running Cruxible's
  own project state on this kit.
- [Documents and notes](documents-and-notes.md): authoring Markdown sections in
  the same write as the Claims that cite them, plus self-checking projection
  blocks.
- [Security walkthrough](security-walkthrough.md): add automated Lines and
  two-principal cosign to dependency and code security audits.
- [IoT fleet walkthrough](iot-fleet-walkthrough.md): deterministic telemetry
  filtering with zero LLM tokens on routine batches and automatic wake-up on
  sustained faults.
