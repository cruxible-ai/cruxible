# Agent memory change series

This harness measures how answer quality and cost per question change as the
same repository changes over several sessions. It implements the v1 benchmark
loop. It has **no real target selected and no live model integration**. All
shipped executable runs are marked synthetic dry runs.

| Arm | Persistent memory | Integration supplied to the adapter |
| --- | --- | --- |
| A | Cloudflare skill's findings.json and reports | Pinned Cloudflare security-audit skill |
| B | Fresh Cruxible state plus configured workspace memory | Shipped general plugin and security kit |
| C, optional | Fresh Cruxible state, without a kit | Same shipped general plugin |

The model, budgets, setup/session prompts, source snapshots, question order,
rubrics and change script are shared. Arm configuration has no custom prompt
field. Rubrics and session-dependent expected facts go only to the grader;
agent invocations receive the question prompt, never the rubric. Each invocation
is a fresh conversation: only the configured external memory persists.

## Run a fixture now

Use an existing Python environment with Pydantic 2 and PyYAML. No install is
needed in an existing Cruxible development environment. From the repository root:

```sh
python -m benchmarks.agent_memory_series.demo --root /absolute/new/scratch/memory-demo
```

This creates its own tiny local Git repository and performs a baseline, fix and
revert, across three arms and two repetitions. It uses a no-op adapter, reports
zero model tokens, and leaves quality ungraded. The integration test exercises
the same loop with scripted, source-dependent replies, usage, persistent memory
and criterion grades. Neither path invokes a model, starts a daemon or fetches
a network target.

The general command is:

```sh
python -m benchmarks.agent_memory_series run \
  --config /absolute/series.yaml --output /absolute/new/scratch/run \
  --script benchmarks/agent_memory_series/examples/scripted-replies.json
python -m benchmarks.agent_memory_series schema > /absolute/scratch/schema.json
```

Without `--script`, every adapter reply is a marked no-op. With it, the file is
a list of `ScriptedTurn` records keyed by phase, session index and question id;
an optional arm key overrides the common reply. Synthetic token counts are
plumbing fixtures, not cost evidence. The CLI permits local fixture Git repos
only. `offline=False` is an explicit Python runner option for a future real
adapter. Change commands are trusted programs: the offline guard restricts the
clone URL and shipped adapter, not arbitrary commands the operator writes.

## Fill the experiment slots

Start with [examples/series.yaml](examples/series.yaml) and
[examples/questions.yaml](examples/questions.yaml). YAML and JSON are accepted.
`questions_file` resolves relative to the configuration; alternatively supply an
inline `questions` list. Each patch/command needs a description. A patch has exactly one of `path` (relative to the
configuration) or inline `text`. A command is an argv list, never an implicit
shell, with its own timeout and the target workspace as cwd.

Before any measured run, the maintainer must supply:

| Slot | Required choice |
| --- | --- |
| Target | One Git URL/local path and exact 40/64-hex base commit; no branch names |
| Changes | Fixed deterministic patches/commands for every session; real series usually 6–8 sessions |
| Integrations | Exact Cloudflare skill, shipped plugin and security-kit versions/host installations |
| Model and budget | Same model and per-session token/turn/time limits for all arms; choose repetitions against the budget |
| Questions | Fixed schedules and criteria, including current evidence, change, contradiction/dependents, coverage and one-file controls |
| Expected facts | Session-specific rubric answers grounded in the frozen changes, written before running |
| Grader | Manual, an implemented model grader, or both; blind grading where feasible |

`target: null` is a valid planning slot and refuses execution. A and B are
required; C is optional. Repetitions start from independent clones, while memory
persists across all sessions/questions within one repetition. Every file used
as agent memory must be declared in `memory_paths`; these paths cannot overlap
the target's tracked source. B/C use the shipped plugin, without kit-specific
audit coaching. A real B setup must adopt the kit through that plugin/general
skills; scripted runs do not pretend to install it.

## Runner and adapter boundary

`run_benchmark(config, output=..., adapter=..., grader=...)` creates fresh clones
at the pinned base, applies session changes in order, invokes audit work, then
asks only that session's scheduled questions. It freezes the full resolved
configuration, rubrics and patch bytes before the first invocation, fingerprints
them, and records each source snapshot. Later changes to patch files cannot
change the experiment. Snapshot disagreement across arms/repetitions, an agent
editing source, a command error or an exceeded budget fails the run explicitly.
Failed runs retain captured records and a failed manifest.

Every run has private config/cache/temp paths and a fresh `CRUXIBLE_STATE_ROOT`.
Remembered sockets, endpoints, instances, credentials and CLI contexts are not
inherited. No host HOME is changed. The scratch output must be new and outside
`.cruxible` state; existing runs are never overwritten. `adapter.close(context)`
runs in `finally` after each arm/repetition, including errors, so an implemented
adapter can stop exactly the daemons it started before another arm runs.

| Adapter | Status |
| --- | --- |
| `ScriptedAdapter` | Executable offline, fixed replies or injected Python fixture callable |
| `CLIAdapterStub("claude-code"/"codex")` | Typed invocation plan with executable, model, budget, isolated env and skill/plugin/kit configuration; execution raises `NotImplementedError` |
| `ManualGrader` | Produces pending criterion rows, exportable/importable as CSV |
| `ModelGraderStub` | Rubric → per-criterion pass/fail interface; no provider or model call implemented |

Live adapters still need host-specific CLI flags/plugin activation, real usage
parsing, in-flight budget enforcement, fresh-conversation behavior, kit setup,
served Claim counts, copy-in assessments and daemon cleanup. The harness checks
reported budgets after each invocation. Audit work and all questions after that
session share one budget; the adapter receives the remaining allowance. Setup
is session 0 with its own same-sized budget. Missing usage is recorded and
marks `usage_complete: false`, so the run cannot verify its token/turn ceiling.
This cannot interrupt an arbitrary Python adapter while it runs. Claim counts must come from the public served
surface, not direct projection-database inspection. D-R44 copy-in failures are
grader/adapter assessments, never inferred from state size.

## Grade and inspect outputs

Each run writes:

| File | Contents |
| --- | --- |
| `frozen-config.json`, `manifest.json` | Inputs, fingerprint, target pin, mode, source digests and run status |
| `results.jsonl` | Typed setup/session/question records, raw answers, separate usage and wall time, criterion grades and wrong-move flags |
| `grading.csv` | Blinded response ids; one row per criterion plus audit-session wrong-move rows |
| `summary.csv`, `summary.md` | Per-arm/session aggregation across repetitions |
| `amortization.csv` | Plot data, including direct and amortized tokens per question |
| `amortization.png` | Quality and tokens/question panels, one line per arm, only if matplotlib is available |
| `plot-status.json` | Whether the optional plot was generated; CSV fallback otherwise |
| `runs/ARM/rep-NNN/` | Independent workspace/state and scripted-change logs |

Fill `passed`, `stale_answer_as_current` and `lost_finding` with `true`/`false`
(or leave blank for ungraded). Wrong-move flags must agree on all criterion rows
for the same answer. Grading sheets include the captured answer and expected
facts; keep them outside agent workspaces. Blinding hides the arm column and
mixes response order; an answer itself may reveal its memory method.

```sh
python -m benchmarks.agent_memory_series grade \
  --results /absolute/run/results.jsonl --sheet /absolute/run/grading.csv \
  --output /absolute/new/graded
python -m benchmarks.agent_memory_series summarize \
  --results /absolute/new/graded/results.jsonl --output /absolute/new/summary
```

Grading preserves the raw run and writes a separate graded result set. The
import rejects unknown responses, changed prompts/answers, duplicate/missing
criteria and inconsistent wrong-move flags.

| Metric | Definition |
| --- | --- |
| Criteria met % | Passed / total fixed rubric criteria; blank until the whole arm/session group is graded |
| Tokens/turns per question | Adapter-reported answering usage; unknown values stay missing, with usage coverage counts |
| Amortized tokens/question | Cumulative setup + audit + answering tokens divided by questions asked so far, then mean across repetitions with complete usage |
| Up-front friction | Setup plus audit tokens/turns through the first useful state reported in session 0/1; blank if not reported |
| Upkeep between questions | Sum of audit tokens and reported upkeep compute since the previous question batch, recorded once per batch |
| Compression | Logical regular-file bytes of declared workspace memory plus B/C state root, divided by source bytes; Claims/source file and assessed copy-in failure share are separate |
| Wrong moves | Explicit grading flags on audit sessions and answers, with grading coverage |

Source measurement covers tracked and non-ignored new source files (including
scripted deletions). Track any ignored evidence fixture that should count in
snapshot identity. Memory size is logical bytes, not allocated disk blocks;
Git metadata and unconfigured caches are excluded. Source/size traversals are
intentional offline measurement and validity checks, outside agent token/compute
usage. They cost the files/bytes measured, not a Cruxible service request.

No conclusion about amortization follows from the shipped fixture. Report the
measured curve whichever way it falls. Results with failed manifests, missing
usage/grades, budget violations or unimplemented integrations are not evidence
of a real comparison.
