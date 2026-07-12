# Wiki Import Demo

A small synthetic memory-bank for "Relay", an invented webhook-delivery
team: the kind of Markdown wiki teams grow around their coding agents. Six
pages:

| Page | What it is |
| --- | --- |
| `CLAUDE.md` | Agent conventions and known sharp edges |
| `architecture-decisions.md` | Dated decision log |
| `onboarding-map.md` | Services, data stores, environments, owners |
| `open-questions.md` | Undecided questions |
| `risks-and-gotchas.md` | Standing risks and operational gotchas |
| `sprint-notes.md` | Rolling sprint notes, partly stale on purpose |

The content is deliberately imperfect in realistic ways: `CLAUDE.md` still
prescribes Postgres advisory locks that the May decision retired (rollout
stuck at 5%, the wiki itself doubts its status), a standup "decision" in the
sprint notes contradicts the conventions page, and an open question may
already be half-answered by a decision. Stage 2 of the
[wiki-to-state skill](../../skills/wiki-to-state/SKILL.md) is
supposed to flag these as unsure rather than resolve them — that is the
teaching point.

## Run The Import

From the repo root, with a daemon up and an agent-operation instance to
import into (see the [Quickstart](../../docs/quickstart.md) for daemon +
auth setup; registration needs a `governed_write` or higher token in
`CRUXIBLE_SERVER_BEARER_TOKEN`).

If the daemon's instance root does not contain this directory, start the
daemon with `CRUXIBLE_ALLOWED_ROOTS=$PWD/examples` so `source register`
accepts the paths.

Dry-run first:

```bash
python scripts/import_markdown.py \
  --dir examples/wiki-import-demo \
  --exclude "README.md" \
  --manifest wiki-manifest.json
```

Expected: six `planned` lines with deterministic ids (`wiki_claude_md`,
`wiki_architecture_decisions_md`, `wiki_onboarding_map_md`,
`wiki_open_questions_md`, `wiki_risks_and_gotchas_md`,
`wiki_sprint_notes_md`). Then register:

```bash
python scripts/import_markdown.py \
  --dir examples/wiki-import-demo \
  --exclude "README.md" \
  --server-url http://127.0.0.1:8100 \
  --instance-id <instance-id> \
  --manifest wiki-manifest.json
```

(Use `--socket /path/to.sock` for a socket daemon, and
`--cruxible-bin "uv run cruxible"` when running from a source checkout.)

Re-run the same command: every page reports `skipped` — ids are
deterministic and registered artifacts are pinned, so re-import is a no-op.

Read a chunk back (chunk ids are in `wiki-manifest.json`):

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> \
  source dereference --artifact wiki_architecture_decisions_md --chunk <chunk-id>
```

From here, continue with Stage 2 (the agent brief) in the
[wiki-to-state skill](../../skills/wiki-to-state/SKILL.md).
