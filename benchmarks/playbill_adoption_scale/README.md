# Playbill adoption-scale benchmark

The measured gate from §12.3 of the Playbill convergence program. It builds a
real accepted ledger — signed generations, live acceptance laws, real dependency
closure — and measures what reopening one costs.

It is **not** part of the test suite. It lives outside `tests/`, carries no
`test_` prefix, and a full Tier-1 run takes tens of minutes. CI never runs it.

## Tier 1 — the PC-F filing gate

| Fixture property | Value |
|---|---|
| Active members | 5,001 (+2 principal records) |
| Artifact-bearing members | all of them; 4,398 Claims carry three pins each |
| Subjects / ClaimTypes | 500 / 64 |
| Documents / QueryDefinitions | 20 / 16 |
| Claim shards | derived from Claim identity, spread across the shard space |
| Accepted generations | 1,000, each a three-member Claim closure |

Budgets on the documented reference VM (4 vCPU, 16 GiB, NVMe-class storage):

| Measurement | Budget |
|---|---|
| Valid-checkpoint reopen, ≤100-generation suffix, p95 over ten clean starts | ≤ 5 s |
| Disposable projection deletion and rebuild, p95 | ≤ 30 s |
| Forced genesis recovery, reproducing identical digests | ≤ 48 s |
| Peak RSS, files read, bytes read, per-phase timings | recorded, not bounded |

Tier 2 (50,000 Claims / 10,000 generations) is a separate recorded milestone and
is not this benchmark's gate.

## Running it

```bash
ROOT=/some/scratch/space

# Build the ledger once. Report includes per-phase construction time.
uv run python benchmarks/playbill_adoption_scale/benchmark.py build \
    --root "$ROOT" --profile tier-1

# Measure it. Each sample is a fresh process.
uv run python benchmarks/playbill_adoption_scale/benchmark.py run \
    --root "$ROOT" --suffix 100 --samples 10 --rebuild-samples 10 \
    --genesis-samples 2 --output "$ROOT/tier-1-results.json"

# If a budget is missed, decompose one checkpointed reopen rather than guess.
uv run python benchmarks/playbill_adoption_scale/benchmark.py profile \
    --root "$ROOT" --suffix 100
```

A smaller shape, for checking the harness itself:

```bash
uv run python benchmarks/playbill_adoption_scale/benchmark.py build \
    --root "$ROOT" --profile smoke --subjects 6 --claim-types 3 \
    --documents 2 --query-definitions 2 --seed-claims 6 --generations 8
```

## What the numbers mean

Every sample runs in a **fresh process**. A single long-lived harness process
would carry warm parse memos and resident objects from one sample into the next
and report a reopen cost no operator would ever see.

`run` derives the bounded-suffix checkpoint once and each sample restores those
exact bytes, because a successful recovery advances the checkpoint to the head
it just served. Without the restore, the second sample onward would measure a
zero-generation suffix; deriving it per sample would instead cost a full extra
recovery per sample.

`projection_rebuild_only_p95_seconds` is the difference between reopening with
the disposable projection deleted and reopening with it present. The rebuild is
not separately timed inside recovery, so this difference is the honest figure.

`reproduced_identical_digests` is the correctness gate that makes the timings
meaningful: checkpointed reopen, projection rebuild, and forced genesis recovery
must all land on the same generation, semantic, and logical projection digests.
A `false` there invalidates the run regardless of how fast it was.
