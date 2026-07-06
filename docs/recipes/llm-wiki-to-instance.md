# LLM Wiki To Instance

Turn an informal LLM wiki — CLAUDE.md files, a memory-bank directory, an
Obsidian vault — into governed Cruxible state without losing the wiki or
trusting a parser to understand it.

The pipeline has three stages with a deliberate seam between them:

1. **Register (deterministic).** Every Markdown page imports as a
   digest-pinned source artifact: stable id, content hash, parsed chunk
   manifest. No interpretation happens here.
2. **Propose (agent judgment).** An agent reads the registered chunks and
   proposes typed state — work items, decisions, risks, open questions,
   notes — citing the exact wiki chunks as evidence.
3. **Review (governance).** A human (or a trusted reviewer role) works the
   proposal queue and approves or rejects each governed claim.

Why is stage 2 judgment and not parsing? Because prose does not carry its own
truth conditions. "We decided to move idempotency to Redis" might be a done
decision, a stalled rollout, or a sentence someone wrote optimistically and
never revisited — the demo fixture below contains all three, on purpose. A
pipeline that auto-mints typed entities from headings would launder that
ambiguity into confident-looking state, which is exactly the failure mode
hard state exists to prevent. So the conversion is an agent reading chunks
and making claims it must attach evidence to, with the judgment-shaped claims
routed through proposals a reviewer can reject. The wiki text stays the
pinned source of record; the typed graph is accountable to it.

## Stage 1: Register The Wiki

`scripts/import_markdown.py` walks a directory, computes a deterministic
artifact id per page (`<prefix>_<slug-of-relative-path>`), and shells out to
`cruxible source register` for each file. It is stdlib-only and talks to the
daemon through the CLI, so it works anywhere the `cruxible` CLI is installed.

Dry-run first (the default when no transport is given):

```bash
python scripts/import_markdown.py \
  --dir path/to/your-wiki \
  --manifest wiki-manifest.json
```

This lists the deterministic ids without touching the daemon. Then register
for real by adding a transport:

```bash
python scripts/import_markdown.py \
  --dir path/to/your-wiki \
  --server-url http://127.0.0.1:8100 \
  --instance-id <instance-id> \
  --manifest wiki-manifest.json
```

`--socket <path>` works instead of `--server-url` for a Unix-socket daemon.
The CLI reads `CRUXIBLE_SERVER_BEARER_TOKEN` from the environment on an
auth-enabled daemon; the script passes the environment through and never
prints the token. Registration requires a `governed_write` (or higher)
credential.

Two things to know before the first real run:

- **Daemon path containment.** `source register` resolves paths on the
  daemon side and refuses paths outside the instance root. If the wiki lives
  elsewhere, start the daemon with `CRUXIBLE_ALLOWED_ROOTS=<wiki-root>` in
  the daemon's environment.
- **Idempotence.** Re-running the import is safe: `source register --id`
  refuses duplicate ids, and the script records those pages as `skipped`.
  If a page's content changes after registration, its artifact keeps the old
  pinned hash and dereferences report `drifted` — register the changed page
  as a new artifact rather than expecting the old one to rebind.

The manifest (`wiki-manifest.json`) is the handoff to stage 2. Per file it
records the path, artifact id, byte count, status
(`registered`/`skipped`/`failed`, or `planned` in dry-run), content hash,
and — for freshly registered pages — the chunk manifest: deterministic chunk
ids with heading paths and line ranges. Keep it next to the wiki.

Useful flags: `--include` (glob, default `**/*.md`), `--exclude` (repeatable;
`.git`, `node_modules`, and `.obsidian` are always excluded), `--id-prefix`
(default `wiki`), `--cruxible-bin "uv run cruxible"` when running from a
source checkout.

## Stage 2: The Agent Brief

Point an agent at the manifest and the brief below. The agent needs CLI
access to the instance and a `graph_write` credential — and that tier is the
*designed* fit, not a compromise. Entities on their own encode no integrated
knowledge: a Decision or StateNote row is a restatement of the source
material plus metadata, verifiable against its cited chunk in seconds, and
an orphan quality check flags anything that never integrates. Knowledge
enters when things *connect*, and that is where governance lives:
judgment-shaped relationship types carry `proposal_only` write policies that
hold at **every** permission tier — a `graph_write` agent structurally
cannot bypass review on a governed edge. So the migration's trust story is
exact: restatement-grade rows land directly and auditable, claims land as
reviewed proposals, regardless of the credential. Do not run stage 2 with an
`admin` credential; it needs no admin surface.

The brief is a prompt, not a program. Copy it verbatim, fill in the three
placeholders at the top, and give the agent access to a shell with the
`cruxible` CLI.

````text
You are converting a team's Markdown wiki into typed Cruxible state. The wiki
pages are already registered as digest-pinned source artifacts; your job is
the judgment step: read them, decide what they actually claim, and turn those
claims into typed state that cites the wiki text as evidence. You do not get
to invent state the wiki does not support, and you do not get to present
uncertain readings as certain ones.

Connection:
- Cruxible CLI transport: cruxible --server-url <URL> --instance-id <INSTANCE_ID> ...
  (or --server-socket <PATH>). The bearer token is already in
  CRUXIBLE_SERVER_BEARER_TOKEN; never print it.
- Import manifest: <PATH-TO-wiki-manifest.json>
- The instance runs the agent-operation ontology. Target types:
  WorkItem (open threads someone should act on), Decision (things the team
  decided, with status proposed/accepted/rejected/deferred), Risk (standing
  concerns), OpenQuestion (undecided questions), StateNote (reference
  context, corrections, and anything worth keeping that is not one of the
  above; kind is one of correction/field_note/rationale_update/
  implementation_note/review_note).

Step 1 — Read the wiki through its registered chunks.
The manifest lists every page's artifact_id and, for registered pages, its
chunks (chunk_id, heading_path, line ranges). Read sections with:

  cruxible ... source dereference --artifact <artifact_id> --chunk <chunk_id> --json

If a page shows status "skipped" in the manifest (registered on an earlier
run, so no chunk list), dereference by heading instead:

  cruxible ... source dereference --artifact <artifact_id> \
    --heading "<top heading>" --heading "<subheading>" --block-selector section --json

A dereference returns status available/drifted/unavailable plus the source
text. If a chunk comes back drifted, stop and report it — do not build state
on text that no longer matches its registered hash.

Step 2 — Inventory the claims before writing anything.
For each page, list the claims it makes and classify each one:
- plainly factual: the page states it flatly and nothing elsewhere in the
  wiki disagrees (service names, ownership tables, dated decision entries,
  concrete gotchas);
- judgment-shaped: anything requiring interpretation — whether a decision is
  actually still in force, whether a sprint note is stale, whether two pages
  contradict each other, what blocks what;
- unsure: the wiki is vague, self-contradictory, or visibly out of date.
  Do NOT resolve these yourself. They become proposals with an "unsure"
  signal, or OpenQuestion entities, so a human decides.

Step 3 — Create entities with batch-direct-write (plainly factual rows plus
the entity shells your proposals will point at). Write a YAML payload and run:

  cruxible ... batch-direct-write --payload-file payload.yaml --dry-run --json
  # fix validation errors, then re-run without --dry-run

Payload shape (entities, optional factual relationships, shared evidence):

  entities:
    - entity_type: Decision
      entity_id: dec_redis_idempotency
      properties:
        decision_id: dec_redis_idempotency
        title: Move delivery idempotency to Redis-based locks
        summary: Replace Postgres advisory locks with Redis SET NX PX plus fencing tokens.
        rationale: |
          Recorded from the team wiki (architecture-decisions.md, 2026-05 entry).
          Source: artifact wiki_architecture_decisions_md, chunk mdchunk_c51f343aac9e5ec8.
        status: accepted
        decided_at: "2026-05-01"
    - entity_type: StateNote
      entity_id: note_conventions_source
      properties:
        note_id: note_conventions_source
        kind: field_note
        title: Advisory-lock guidance in CLAUDE.md predates the May decision
        summary: CLAUDE.md still prescribes advisory locks; the May decision retires them once rollout completes.
        body: |
          CLAUDE.md ("Conventions") says idempotency uses Postgres advisory
          locks (artifact wiki_claude_md). architecture-decisions.md (2026-05
          entry, artifact wiki_architecture_decisions_md, chunk
          mdchunk_c51f343aac9e5ec8) moves idempotency to Redis locks. Both
          statements are live in the wiki today.
        created_at: "2026-07-05T00:00:00Z"
  relationships:
    - from_type: StateNote
      from_id: note_conventions_source
      relationship: state_note_about_decision
      to_type: Decision
      to_id: dec_redis_idempotency
      shared_evidence_keys: [wiki_passage]
      evidence_rationale: Note records where this decision text came from.
  shared_evidence:
    wiki_passage:
      source_evidence:
        - source_artifact_id: wiki_architecture_decisions_md
          chunk_id: mdchunk_c51f343aac9e5ec8

Rules for entities:
- Every entity body (rationale, StateNote body, WorkItem description) that
  restates wiki content must name its source inline the way the example
  does: artifact id + chunk id. Quote the load-bearing phrase, do not
  paraphrase it into something stronger.
- Direct writes are live but UNREVIEWED state. That is acceptable only for
  rows a reviewer could verify against the wiki in seconds. If you are
  weighing evidence, it is not a direct write.
- Do not create a Decision with status "accepted" unless the wiki says the
  decision was made. A decision whose rollout or status the wiki itself
  doubts still gets created (the decision WAS made), but the doubt becomes a
  StateNote or an unsure-signaled proposal, not silent omission.

Step 4 — Route every judgment-shaped claim through a proposal.
The governed relationships in this ontology (risk_blocks_work_item,
open_question_blocks_work_item, open_question_blocks_decision,
work_item_mitigates_risk, work_item_answers_open_question,
decision_answers_open_question, decision_supersedes_decision,
decision_constrains_work_item, work_item_supersedes_work_item,
work_item_depends_on_work_item) require review. Propose them:

  cruxible ... group propose --relationship <type> \
    --members-file members.json \
    --thesis "<one-sentence claim a reviewer can accept or reject>"

Each member must carry a source_evidence signal quoting the wiki passage:

  [
    {
      "from_type": "Risk",
      "from_id": "risk_pgbouncer_connection_ceiling",
      "relationship_type": "risk_blocks_work_item",
      "to_type": "WorkItem",
      "to_id": "wi_redis_idempotency_rollout",
      "properties": {"blocking_basis": "Connection ceiling blocks scaling delivery workers until Redis locks land."},
      "evidence_rationale": "Wiki risks page ties the pgbouncer ceiling to the idempotency migration.",
      "signals": [
        {
          "signal_source": "source_evidence",
          "signal": "support",
          "evidence": "\"we are ~30 replicas away from max_connections\" — risks-and-gotchas.md",
          "source_evidence": [
            {"source_artifact_id": "wiki_risks_and_gotchas_md", "chunk_id": "<chunk-id>"}
          ]
        }
      ]
    }
  ]

Signal honesty is the whole point:
- "support" only when the wiki plainly states the claim.
- "unsure" whenever the wiki hedges, contradicts itself, or is stale — and
  say why in the evidence string, quoting the hedge. Unsure signals force
  human review; that is correct behavior, not failure.
- Never fabricate a source_evidence locator. Every chunk_id you cite must be
  one you actually dereferenced.
- One thesis per group; do not bundle unrelated claims to save review effort.

Step 5 — Contradictions and staleness are findings, not noise. When two
pages disagree (a conventions page prescribing something a decisions page
retired, a sprint note contradicting a conventions rule), do all of:
(a) create an OpenQuestion or StateNote (kind: correction) naming both
passages with both artifact/chunk ids; (b) if a governed edge depends on the
answer, propose it with an "unsure" signal citing both chunks; (c) list it
in your final summary.

Step 6 — Report. End with: entities created (by type, with ids), groups
proposed (group ids + theses), every unsure signal and why, contradictions
found, and any chunk that dereferenced as drifted or unavailable. The
reviewer works from this report plus the proposal queue.
````

The demo fixture (`examples/wiki-import-demo/`) is seeded with the cases the
brief's rules exist for: a decision whose own text doubts its rollout, a
conventions rule the decisions page retired but did not delete, a standup
"decision" that contradicts the conventions page, and stale sprint items. A
stage-2 run that marks none of these unsure is a bad run.

## Stage 3: Review The Queue

Proposals from stage 2 land as pending candidate groups. Work the queue:

```bash
cruxible ... group list --status pending_review
cruxible ... group get --group <group-id> --json
```

Read the thesis, the member signals, and the quoted evidence; spot-check a
citation by dereferencing the cited chunk before deciding. Then resolve,
passing the pending version you reviewed:

```bash
cruxible ... group resolve --group <group-id> \
  --action approve \
  --expected-pending-version <pending-version> \
  --rationale "Verified the wiki passage; claim holds."
```

Rejecting is a first-class outcome — an unsure-signaled proposal you reject
is the pipeline working. How resolutions, signature buckets, trust grading,
and later maintenance behave is covered in
[State Resolution And Maintenance](../state-resolution-and-maintenance.md).

## Verify The Result

Spot-check that approved claims really trace back to the wiki. Evidence and
provenance for an edge live on the lineage surface:

```bash
cruxible ... relationship lineage \
  --from-type Decision --from-id <id> \
  --relationship decision_constrains_work_item \
  --to-type WorkItem --to-id <id> --json
```

Expect `provenance.source: group_resolve` with the group, resolution, and
receipt ids, and `evidence.evidence_refs` entries of the form
`{source: source_artifact, source_record_id: <chunk-id>, artifact_id: <artifact-id>}`
carrying the pinned content hashes. Dereference one with the pinned hash:

```bash
cruxible ... source dereference --artifact <artifact-id> --chunk <chunk-id> \
  --expected-content-hash <content_hash-from-the-evidence-ref>
```

`available` means the wiki text behind the claim is byte-identical to what
the agent cited; `drifted` means the page changed since — the claim is now
flagged, which is the guarantee you imported the wiki to get. Finally, list
what stage 2 built:

```bash
cruxible ... list entities --type Decision
cruxible ... list entities --type WorkItem --where status=active
cruxible ... list edges --relationship decision_constrains_work_item
```

## Try It On The Demo Fixture

`examples/wiki-import-demo/` is a small synthetic team wiki with a README
that runs stage 1 end-to-end against a scratch instance.
