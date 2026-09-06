# Exact generation-27 checkpoint diagnosis

**The dominant prepare cost is cold historical AuthoringIntent fingerprint lookup, not the eight new Claims.** On the copied real instance, the first lookup took **11.741 s unprofiled**, then **0.109 s warm**. It validated **24 historical intent streams / 173 complete event snapshots**. This is large enough to account for most of the scale of the live **12.786 s prepare** observation after daemon restart. It is a separately measured component, not a traced decomposition of that original live request, so those numbers must not be arithmetically subtracted as exact live attribution.

## Exact workload and isolation

Code: `283ca306cfeebabc84becb271079a02266059ba7`, in `/private/tmp/playbill-write-loop-latency`.

Fresh source was copied before activation; source main before and after copying and copied main all equaled **bc46256433c8bcfd68c6bcbc17c4879a55185123** (program generation 27). Copy: `/private/tmp/playbill-gen27-exact-checkpoint`. It includes ledger, CAS, exhaust and checkpoint data, plus only `credentials/allowed_signers`; **no private keys were copied**. The existing public trust file was reused after checking matching genesis. Derived projections started empty in the copy. All computations and temporary artifacts stayed in `/private/tmp`; no live mutation, source change, or publication occurred.

The existing intent **AIT-7b76f71e8b6d9a846727a7794aff0662** was fetched with a single public SDK GET. The deployed V2 fix now preserves all **12 original reference assertions**, and the full response validates as `AuthoringIntentV2`. Its 359,324-byte response is stored in a private temporary file without credentials.

Both copied preflight computations passed and reproduced the exact live candidate **sha256:bf54cc803e21f5c7489802f25f816d0f3b8b8f3448b121d03dddca16f771a178**, with **12 members: eight Claims and four Subjects**. Unlike the previous generation-26 diagnosis, no reference assertions were omitted or replaced.

## Measured phases

| Phase | Cold | Warm | Instrumentation |
|---|---:|---:|---|
| Historical fingerprint lookup | **11.741 s** | **0.109 s** | Unprofiled, fresh process for cold |
| Same fingerprint lookup diagnostic | 28.692 s | 0.170 s | cProfile; substantial Python-call overhead |
| Exact compute_preflight | 3.672 s | 2.318 s | cProfile |
| Warm matching intent GET from store | — | 0.003 s | cProfile, after historical validation |

Copy recovery **13.609 s** was excluded from request timings; its initially empty derived projections differ from the live daemon. The live parent measurements were prepare12.786 s, submit9.904 s. This diagnosis does **not** profile submit, actual HTTP/SDK compilation, or coordinator transitions in that original live request.

Cold fingerprint lookup spent **28.362/28.692 profiled seconds** decoding 173 events. These events repeat full historical payload snapshots. Major overlapping cumulative costs were intent binding validation **11.533 s**, event digest verification **9.441 s**, and member identity calculations **5.547 s**, with canonical normalization dominating the work overall. Event input JSON loading itself was only **0.596 s**. The warm compact stream proof still reads/hashes event bytes and preserves corruption detection, but avoids these repeated model validations.

Warm compute_preflight spent **2.150/2.318 s** in proposal evaluation. The remaining accepted dependency rebuild cost **0.991 s**, and the two effective policy-value views cost **0.799 s**. Cold lowering was **0.820 s**; subsequent prepared lowering reuse works. The twelve original reference assertions are present in these measurements and did not become the dominant phase.

## Recommended next scope

1. **Address the first-write-after-restart intent lookup tax.** Current compact fingerprint proofs live only in process memory. A daemon restart discards them, and the first create/compile reconstructs them by validating every historical full event, even though retry lookup needs only terminal state, actor and fingerprint. The small synthetic workload lacked this historical exhaust, explaining why its prepare result did not predict the real project instance.
2. **Choose the ownership/validation design before persisting a faster index.** A disposable index cannot simply be trusted after restart because event integrity, changed historical bytes, duplicate pending fingerprints and first-refusal ordering still matter. Startup/background warming can move the cold validation off the first write, but must expose readiness and avoid falsely claiming the computation disappeared. A durable verified summary needs a precise trust/proof boundary rather than mtime-based skipping. No implementation is included here.
3. **Continue accepted-state evaluator reuse separately.** Approximately one profiled second still rebuilds the dependency state on each evaluation, with another ~0.8 s for full-world policy views. These are meaningful steady-state costs, but they are not the primary reason this first prepare took12.8 s.

A next useful product benchmark should include representative historical AuthoringIntent streams/events and measure the **first create after restart** separately from subsequent prepares. Repeat this eight-Claim operation only in a disposable instance if comparing end-to-end paths; do not create redundant live state merely to warm or time it.

## Reproduction artifacts

- `/private/tmp/copy-playbill-gen27.py`: explicit public-key-only copy, before/after main verification.
- `/private/tmp/playbill-gen27-exact-intent.json`: public GET response, mode0600, no credentials.
- `/private/tmp/playbill-gen27-exact-profile.py`: exact-assertion preflight and fingerprint diagnostics.
- `/private/tmp/playbill-gen27-exact-profile/summary.json`: stages, exact candidate identity, reference count.
- Same directory: `compute_preflight_{cold,warm}.{txt,pstats}`, `fingerprint_lookup_{cold,warm}.{txt,pstats}`, `store_get_warm.{txt,pstats}`.
- `/private/tmp/playbill-gen27-exact-profile/fingerprint-unprofiled.json`: separately measured11.741/0.109 s cold/warm lookup.

No tests or full suite were run; this was bounded performance diagnosis only.
