# Agent memory change series

This harness measures how answer quality and cost per question change as the
same repository changes over several sessions. It has **no real target
selected and no live model integration**: every shipped executable run is a
synthetic dry run.

| Arm | Persistent memory | Tools the agent gets |
| --- | --- | --- |
| A | Cloudflare skill's findings.json and reports | Pinned Cloudflare security-audit skill |
| B | Fresh Cruxible state from the security kit | Every Cruxible surface: MCP (full catalog), CLI, Python SDK; general skills only |
| C, optional | Fresh Cruxible state, no kit | Same as B, no kit |

B's surface set is broader than the shipped plugin's default (20-tool
allowlist): the agent chooses among the full MCP catalog, the matching
`cruxible` CLI and the SDK, and the harness logs which surface each Cruxible
action used.

Same model (Claude Code, Opus) for every arm, same session prompts, source
snapshots, question order and change script. Each invocation is a fresh
conversation: only the configured external memory persists. Subagents are
allowed in both arms, as in real use.

## Maintainer decisions this harness implements (2026-11-09)

| Decision | Where |
| --- | --- |
| No token/turn budget: usage is recorded, never capped | `Usage`; no budget field (a `budget` key is refused) |
| Runaway guard far above normal, hit = outcome, series continues | `guard`; `Record.runaway`; `manifest.runaway_hits` |
| Setup off the clock for both arms, recorded separately | `Arm.setup`; `setup.csv`; excluded from session metrics |
| B's reviewer subagent approves as its own principal | B setup runs `cruxible init --reviewer-key-dir`; see [Reviewer principal](#reviewer-principal-arm-b) |
| Eye-test review per session, not a rubric | `review/session-NN.csv`; `Review` flags + notes |
| Quantitative metrics and trend 1 -> N | `summary.csv`, `trend.csv` |
| Claude Code, fresh config dir per invocation, no `--bare`, no resume | `CLIAdapterStub.plan` / `CLIInvocation` |
| B gets MCP full catalog + CLI + SDK; log surface per action | `Arm.cruxible`; `Reply.actions`; `surfaces.csv` |
| Fallback MCP catalog if eager loading skews tokens | `cruxible.mcp_tools` allowlist (commented in the example) |
| 3 repetitions per repo for reported runs (1 for the pilot); report spread | `repetitions`; `spread.csv` |

## Run a fixture now

From the repository root, in an environment with Pydantic 2 and PyYAML:

```sh
python -m benchmarks.agent_memory_series.demo --root /absolute/new/scratch/memory-demo
```

This builds a tiny local Git repository and runs six sessions (baseline, new
dependency, fix, revert, refactor that removes the reachable call, KEV feed
update) across three arms and two repetitions, with off-the-clock setup steps
for each arm. It uses a no-op adapter and reports zero usage. The integration
test (`tests/test_benchmarks/`) drives the same loop with scripted replies,
usage, surface actions, persistent memory and review sheets. Neither invokes a
model, starts a real daemon or touches the network.

```sh
python -m benchmarks.agent_memory_series run \
  --config /absolute/series.yaml --output /absolute/new/scratch/run \
  --script benchmarks/agent_memory_series/examples/scripted-replies.json
python -m benchmarks.agent_memory_series schema > /absolute/scratch/schema.json
```

Without `--script`, every adapter reply is a marked no-op. With it, the file is
a list of `ScriptedTurn` records keyed by phase, session index, question id and
setup step; an optional arm key overrides the common reply.

## Configuration

Start from [examples/series.yaml](examples/series.yaml) (seven sessions) and
[examples/questions.yaml](examples/questions.yaml). YAML and JSON are accepted.

| Field | Meaning |
| --- | --- |
| `target` | Git URL/path and exact 40/64-hex base commit; `null` refuses to run |
| `model` | One model for all arms |
| `guard` | `max_seconds_per_invocation` (default 4 h), `max_cost_usd_per_invocation` (default 100) |
| `repetitions` | 1 for the pilot, 3 per repo for reported runs |
| `strip_from_target` | Path names removed from every checkout identically (default `CLAUDE.md`, `CLAUDE.local.md`, `.claude`, `AGENTS.md`, `.codex`) |
| `sessions[].changes` | `patch` (path or text), `command` (argv, no shell), `feed` (frozen advisory snapshot) |
| `arms[].setup` | Off-the-clock steps: `command` (harness runs it; `background` + `wait_for` for a daemon) or `agent` (one setup prompt) |
| `arms[].environment` | Host settings a normal setup gives the tools (e.g. `CRUXIBLE_MCP_KEY_DIR`); isolation variables are refused |
| `arms[].cruxible` | B/C: `mcp_tools: null` (full catalog) or an allowlist; `bin_dir` with the matching CLI and an SDK-capable Python, prepended to PATH |
| `arms[].memory_paths` / `tooling_paths` | Workspace paths that are memory (counted in state size) or installed tooling; both excluded from source identity |
| `questions[]` | `kind` (recall, change, contradiction, coverage, control), `prompt`, `after_sessions`, optional `reviewer_context_by_session` |

Setup argv and environment values may use `{run_root}`, `{workspace}`,
`{state_root}`, `{keys}`, `{feeds}`, `{tooling}`, `{socket}` and `{python}`,
which the harness substitutes with this run's isolated paths.

Setup prompts are the only arm-specific prompts. They perform what the tool's
own documentation tells a new user to do (install the skill; add the kit and
adopt its automation template) and never coach the audit. Session and question
prompts are shared.

### Feeds: threat-intelligence sessions

A `feed` change writes a frozen file to `{feeds}/<name>`, exposed to agents as
`AGENT_MEMORY_FEEDS_DIR`, identical for every arm and digested into the
manifest (`feed_snapshots`). B's setup can point its Lines' sources there
while tailoring the template. If the pilot finds an arm cannot read frozen
feeds, run that session on live feeds and record fetch times: the session then
changes the answers, not the code.

## Setup and the reviewer principal (arm B)

Setup is off the clock for both arms. Each `agent` setup step is recorded as a
`setup` record (usage kept); `command` steps are logged in
`runs/ARM/rep-NNN/setup.json`. Session metrics start at session 1.

| Arm | Setup steps in the example |
| --- | --- |
| A | Fetch the pinned skill commit into `{tooling}`; the live adapter copies it into each fresh client config |
| B | Start this run's scratch daemon (stopped at close); `cruxible init --principal-id owner --key-dir {keys}/owner --reviewer-key-dir {keys}/reviewer`; hard-link both `<principal>.ed25519` keys into `{keys}/mcp`; agent step: add the kit, tailor + submit the automation template, reviewer subagent approves, preview the start Runbook |

### Reviewer principal (arm B)

How the agent learns the reviewer identity, using only what a normal local
setup provides:

1. The MCP server's environment (host settings) carries
   `CRUXIBLE_MCP_KEY_DIR={keys}/mcp`, holding `owner.ed25519` and
   `reviewer.ed25519`, and `CRUXIBLE_PRINCIPAL_ID=owner`.
2. `cruxible_proposal_approve`'s `signer_id` description says the key is
   `<signer_id>.ed25519` in that directory. Approving without `signer_id` is
   refused and the refusal names both signers (`owner, reviewer`).
3. The plugin's governance reference tells agents a separate session (its own
   principal) reviews. A reviewer subagent therefore approves with
   `signer_id: reviewer`; on the CLI, the init-written `{keys}/reviewer/cruxible.env`
   or `--signer-id reviewer --key PATH` does the same (docs/review-and-governance.md).

No benchmark-specific instruction tells the agent this. Auth is off on the
scratch socket daemon, so identities are taken at their word (honor system).
Pilot check: `init` writes keys as `<principal>.ed25519` and the hard links pass
the no-symlink key check.

## Live adapter (Claude Code)

`CLIAdapterStub.plan()` returns the reviewable `CLIInvocation`; `run()` raises
`NotImplementedError`. The recipe:

| Rule | Why |
| --- | --- |
| Not `--bare` | It skips subagents and MCP |
| Fresh `CLAUDE_CONFIG_DIR` per invocation, holding only the arm's skill (A) or plugin/MCP config (B) | Auto memory has no off switch and is keyed by config dir |
| Strip `CLAUDE.md`/`.claude/` from the target, same for both arms | No inherited project instructions |
| Fresh `claude -p` per invocation; `--continue`/`--resume` refused | Only declared external memory persists |
| Same network and session prompts for all arms | Comparability |
| Record model ID, Claude Code version, skill commit, plugin/kit versions | `adapter.versions()` -> `manifest.runs`; `Reply.model_id` |

Still to implement in a live adapter: copy `tooling_dir` into the config dir;
register B's MCP server with the configured catalog; parse stream-json usage
(tokens, cache, cost, turns, model calls, subagents); map every Cruxible action
to `Reply.actions` (`mcp__cruxible__*` tool calls -> `mcp`, `cruxible <sub>`
Bash commands -> `cli`, `cruxible_client` calls -> `sdk`; cross-check against
the daemon request log); stop at the runaway guard and set `stopped_by_guard`;
report served Claim counts without reading projection databases; stop daemons
in `close()`.

### Pilot check: MCP catalog size

The full MCP catalog is about 131k tokens of schemas, against about 15k for
the plugin allowlist. Before reported runs, measure how Claude Code loads it:
first-turn input tokens of a B session with the full catalog versus the
fallback allowlist (commented in the example: plugin allowlist + authoring +
remaining kit tools). If the host loads tool schemas eagerly rather than
deferring them, use the fallback for B and record which catalog ran
(`frozen-config.json`).

## Outputs

| File | Contents |
| --- | --- |
| `frozen-config.json`, `manifest.json` | Inputs, fingerprint, per-run versions, source and feed digests, runaway hits, status |
| `results.jsonl` | Setup/session/question records: answers, usage, wall time, runaway, model ID, surface actions, reviews |
| `review/session-NN.csv` | One review sheet per session, blinded, without metrics |
| `summary.csv`, `summary.md` | Per arm/session: review flags, question and audit usage, amortized cost, surface use, size |
| `setup.csv` | Setup usage per arm, summed over steps |
| `trend.csv` | Session 1 -> N per arm and metric: first, last, change %, least-squares slope |
| `spread.csv` | Across repetitions: n, mean, min, max, stdev |
| `surfaces.csv` | Cruxible actions by arm, session, phase, surface and name |
| `amortization.png` | Tokens/question, cost/question, audit tokens; only with matplotlib (`plot-status.json`) |
| `runs/ARM/rep-NNN/` | Isolated workspace, state, keys, feeds, tooling, setup and change logs |

## Review (eye test)

Each answer and audit session gets free-text notes and three fault flags:

| Flag | Meaning |
| --- | --- |
| `stale_as_current` | States something no longer true as current |
| `wrong` | States something false |
| `missing` | Leaves out something the session made relevant |
| `needs_validation_with_blocker` | Says what is unverified and what blocks verifying it: a valid answer, not a fault |

Fill `true`/`false` (blank = not reviewed). Rows with no flag and no note stay
unreviewed, so sheets can be reviewed one session at a time. Blinding hides the
arm and mixes response order; an answer may still reveal its memory method.

```sh
python -m benchmarks.agent_memory_series review \
  --results /absolute/run/results.jsonl --sheets /absolute/run/review \
  --output /absolute/new/reviewed
python -m benchmarks.agent_memory_series summarize \
  --results /absolute/new/reviewed/results.jsonl --output /absolute/new/summary
```

Import preserves the raw run, and refuses unknown responses, duplicate rows,
changed prompts/answers and edited reviewer context.

| Metric | Definition |
| --- | --- |
| Flagged answers | Reviewed answers with any fault flag |
| Tokens, total tokens, turns, cost, model calls, subagent calls, wall time | Means per question and per audit session; unknown stays missing |
| Amortized tokens/cost per question | Cumulative audit + answering usage (setup excluded) divided by questions asked so far |
| First useful state | Session-1 audit tokens/turns until the adapter reports useful state |
| Upkeep between questions | Audit tokens and compute since the previous question batch |
| Surface actions | Mean Cruxible actions per repetition by surface (MCP, CLI, SDK) |
| Compression | Memory bytes / source bytes; Claims per source file; copy-in failure share (assessed, never inferred) |

No conclusion follows from the shipped fixture. Report the measured curve
whichever way it falls. Runs with failed manifests, missing usage or
unimplemented integrations are not evidence of a real comparison.
