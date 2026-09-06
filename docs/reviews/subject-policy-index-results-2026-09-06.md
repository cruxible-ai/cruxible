# Subject-targeted freeze policy reads — 2026-09-06

## Outcome

Freeze policy evaluation reads complete Claim value views for each affected Subject, without parsing unrelated Subjects' Claims. A rebuildable index of actual Claim statement Subjects is maintained alongside the existing semantic manifest and dependency index. Arbitrary corroboration queries still use their complete accepted query facts. The ledger remains authoritative; no stored digest, signed receipt, checkpoint wire format or public contract changes.

## Measurements

Means of the two later warm writes on the **matched freeze-policy workload**:

| Operation | Before | After |
|---|---:|---:|
| Warm prepare | 0.642 s | 0.438 s |
| Warm submit | 2.759 s | 2.357 s |
| Warm accept | 5.457 s | 5.261 s |
| Warm complete loop | 10.638 s | 9.827 s |
| First prepare | 0.705 s | 0.709 s |
| First complete loop | 11.283 s | 10.937 s |
| Fresh-process reopen | 4.482 s | 4.144 s |

Warm submission improved by **14.6%**, preparation by **31.8%**, and the complete warm write loop by **7.6%**. Both reopens reproduced all 18 generations and the exact final accepted coordinate.

Before source: `52a4558289dadbe3da99df2409c520a2d5cd9296`. After source: `0b2f1d769bb9063c85ca784f0f2bde3300933950`.

The matched freeze workload uses 1,000 seed Claims, 100 Subjects, eight ClaimTypes, eight fixture history generations, 48 unsigned orphan proposals and three successive two-Claim writes in one fresh daemon/SDK connection. Each write revises one Claim and creates one observation. A new portable harness flag installs inactive but applicable freeze requirements on every ClaimType before vocabulary, Claim and query pins are constructed. All seed/history/measured values are ready; freeze conditions look for frozen. Thus legitimate writes exercise full freeze input construction on both implementations without activating a freeze or producing ambiguous parent values. The original fixture hooks are restored in finally. No production policy/compiler function is patched.

```sh
python docs/benchmarks/write-loop-served.py --repo /path/to/worktree \
  --population 1000 --history 8 --repeats 3 --claims-per-write 2 \
  --orphan-proposals 48 --world --freeze-policy --no-server-profile \
  --reopen-after --output /tmp/subject-policy.json
```

Both source versions are driven by this same final harness. Setup, daemon startup, SDK connect/orient and initial World acquisition are excluded from write totals and reported separately. Totals include lazy typed drafting, prepare, submit, status, approval challenge/sign/submit, acceptance, full Claim readback pinned to receipt coordinate and a fresh typed World snapshot. Prepare can fill the dependency/Subject cache before the first submit; the first acceptance still fills the static projection cache. Later two writes are warm samples, not a p95/p99 distribution. Human review time is excluded; no profiler or concurrent tests ran during measurement. OS/filesystem caches are uncontrolled.

Fixture state and keys are disposable. Assertions verify readback values/counts/coordinates and workspace advertisement. The attached workspace has no floor or ledger mirror. Self-sourced Claims are current/uncovered under policy; this is a lawful performance diagnostic, not a supported-evidence customer proof. Reopen runs in a fresh interpreter after daemon shutdown, excludes imports/trust parsing, and verifies the exact final accepted coordinate. It is one sample, not a worst-case restart bound. Adjacent JSON retains complete timings, source heads, grades, setup and recovery observations.

The ordinary **no-freeze** smoke measurement used the same command without `--freeze-policy`. Warm prepare averaged 0.416s, submit 2.351s, acceptance 4.815s and complete writes 9.341s. The previous pass’s ordinary run measured 9.603s complete writes. That earlier ordinary baseline was not rerun in this pass, so this is a regression smoke check, not another matched improvement claim. First prepare was 0.696s versus 0.539s in that prior run; the new index adds cold Claim parsing, which the no-freeze path previously did not need. Fresh-process reopen took 3.838s and reproduced all 18 generations and the exact final coordinate. The ordinary output is recorded separately as `subject-policy-ordinary-after-2026-09-06.json`; its reference is `submission-dependencies-after-2026-09-06.json`.

## Design

`ClaimSubjectIndex` stores Claim path → actual statement Subject path and Subject path → frozen set of Claim paths. It retains no Claim models, policy decisions or time-filtered values. All Claim lifecycle/time states are indexed, so policy evaluation still applies effective-from/until and retirement filtering at the request's timestamp. It deliberately does not infer Subjects from pins: correspondence between pins and statements is a separate law check occurring later.

The cold evaluator builds the index after its existing dependency parsing. The incremental evaluator removes old memberships and adds changed exact Claim statements after the existing scoped-format checks and dependency update. Retargeting, retirement, deletion and rewinding to an older tree reproduce cold membership. Both top-level maps are copied; only touched Subject buckets are reconstructed. The bounded instance evaluation cache retains and deep-copies this index with the rest of its derivations. Failed parsing never mutates the predecessor.

Checkpoint seeding reconstructs the index from the verified checkpoint tree after the existing signature/root checks. No index is trusted from serialized checkpoint data, and the wire shape is unchanged. Recovery and explicit refresh retain their existing behavior.

On first freeze demand for a Subject, parent and candidate indexes separately select all relevant Claim paths. Values are computed once for that Subject and shared across governing ClaimTypes. This preserves unchanged cross-type inputs, missing-versus-empty predicates, canonical value deduplication/order and time boundaries. Both indexes are required to use the targeted path; low-level calls without indexes retain the full-scan oracle. Corroboration remains separate and coordinate-bound.

## Verification and review guide

Source, tests and portable freeze harness were committed as `0b2f1d76`. Independent review approved the final change with no findings; standard report: `subject-policy-index-2026-09-06.md`.

39 unique targeted tests passed in the isolated worktree: 9 Subject index/policy cases, 4 demand-driven policy cases, 12 evaluation-state cache cases, 2 served preflight/submit/accept cases, 10 corroboration integration cases, and 2 checkpoint/stride recovery cases. Mypy passed for all three changed source files; Ruff check/format passed for all six changed source/test/harness files, and diff checks passed. No full suite, golden corpus or canonical-checkout tests ran.

Coverage includes strict statement-vs-pin membership; V2 and attributed V3 retirement; retarget/remove/rewind parity; start/end time boundaries; absence of unrelated Claim parsing; active and inactive cross-type freeze result parity; multiple changed Subjects; failed update preserving the predecessor; shared-cache return mutation isolation; real corroboration queries, refusals and replay; checkpoint-derived index equality; and checkpoint versus genesis-rooted recovery parity. Existing served tests compare exact candidates with cold evaluation and reopen through both SHA-1 and SHA-256 ledgers.

Read index build/update first, then the three EvaluatedTreeState constructors, then per-Subject freeze demand, then the parity tests and portable benchmark flag.

## Remaining limits and next work

The index adds one cold strict Claim parse per Claim and O(Claims + Subject memberships) retained primitive references. Its maps are covered by the existing evaluation-cache input-size/count bounds, which are not a Python RSS bound. Warm advancement still copies top-level maps; shared cache returns still deep-copy derived state. Tree comparisons and manifest traversal also remain proportional to world size.

Policy value work now follows the full Claim population of affected Subjects, not only edited Claims; changing a highly connected Subject can legitimately remain expensive. This does not narrow arbitrary corroboration query dependencies or change policy meaning.

The next broad cost is accepted-history/proof projection construction and full SQLite rebuilding. Separating stable evidence from snapshot context remains necessary before ordinary small changes can avoid rebuilding unrelated proof rows. No claim of fully incremental acceptance is made by this pass.
