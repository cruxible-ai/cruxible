# Checkpoint latency diagnosis — 2026-09-06

Read-only diagnosis of code at **2215f370756e86af41814474333ee9a0e87c33ae**. No source edits, deployment, live instance writes, or network publication. Profiling used `/private/tmp/playbill-state-loop-instance`, a public-key-only copy of program generation 26 (27 history entries including genesis). Derived review refs/notes and generated CAS bodies were written only in that disposable copy. The sole live request was the public SDK's GET for existing intent `AIT-2f0836b76bb4590c4b2fabd6b2fa750c`; credentials were neither printed nor saved.

## Observed versus attributed

Live parent observations: 18-Claim checkpoint (11 replacements, 7 new Claims, 4 new Subjects), prepare **15.736 s**, submit **29.831 s**, review **0.858 s**. Its shared self-source capsule is 42,747 bytes. The previous 162-new-Claim synthetic benchmark is not an equivalent workload or world size and provides no valid regression baseline.

The following are **cProfile diagnostic times**, not matched end-to-end benchmarks. Preparation profiles exclude coordinator history persistence, original reference assertions, SDK compilation, transport, and live daemon contention. Workspace profiles exclude the attached workspace's actual Git fetch. The measurements therefore locate costs; they do not sum to an explanation of every live second.

| Copied phase | Profiled time | Attribution |
|---|---:|---|
| Warm historical review reconciliation | **8.830 s** | **449 Git subprocesses, 8.620 s** cumulative |
| Check/publish 82 note groups, contained in preceding row | 4.916 s | 164 note reads: 3.355 s; 82 object existence checks: 1.475 s |
| Materialize 40 historical review commits, contained above | 2.999 s | Git commit-tree, identity normalization, tree and parent verification |
| Build review-note index, contained above | 0.840 s | Compact candidate summary reads total only **0.035 s** |
| Cold checkpoint compute_preflight | **6.129 s** | Evaluation 4.597 s; lowering 0.940 s; initial tree read ~0.416 s |
| Warm checkpoint compute_preflight | **4.586 s** | Evaluation **4.416 s**; prepared lowering reuse is effective |
| Two whole-world effective Claim scans within warm evaluation | 1.572 s | Reparse effective policy values from accepted/candidate trees |
| 22 accepted referent-coordinate computations within warm evaluation | 1.416 s | Repeated tree-wide derivation per member |
| Rebuild parent dependency state within warm evaluation | 1.017 s | Cold build of immutable accepted tree state |

The first review reconciliation was **23.564 s**: this older copy had 82 missing projected notes, so that pass included repair writes and cold candidate parsing. It is **not** a comparable live cold timing. Recovered-copy setup was separately excluded (30.057 s for reconciliation run, 17.316 s for preflight run). No claim is made that request-time recovery caused the live delay.

Both preflight computations passed and reproduced all **22 members** and exact candidate digest **sha256:479ab7252fedd09f4d629674257f096b778a9103551a58c8f25d7b344b22ea12**. This proves semantic candidate equivalence for the copied payload, not complete request/certificate equivalence: the GET response omitted the original reference assertions, so the diagnostic supplied an explicitly empty assertion tuple. Their original validation cost and meaning are excluded, not assumed irrelevant.

## Remaining design costs and recommended order

1. **Remove full historical workspace reconciliation from foreground writes.** `ProposalService.submit` finishes durable evidence and notes, requests asynchronous mirror publication, then synchronously calls `advertise_workspace`; that instance method reconciles every historical review alias before fetching into the workspace. Use a coalesced publication request/status/barrier model for this derived surface, and retain a strict explicit rebuild/verification path. While implementing, batch Git note/object reads and verify already-existing exact review commits without recreating each one. Preserve collision-group completeness, corruption refusal, original/advisory aliases, and settled archives. The warm copy demonstrates an approximately nine-second avoidable foreground workload before the workspace fetch itself.
2. **Make evaluation's accepted-state derivations reusable at a verified coordinate.** Within a single evaluation, compute accepted referent coordinates once rather than once per member. Share immutable parsed Claim/policy indexes and the accepted dependency state, then advance only changed candidate members. Keep timestamp-dependent effective-value filtering explicit, candidate overlays isolated, fresh receipt/authority checks, bounded caches, and exact frozen-replay parity. These measured costs dominate the remaining warm preflight.
3. **Then investigate repeated evaluation across preflight and submit.** The existing call graph evaluates during prepare, again during submit's `_compute_and_bind_preflight`, and once more inside `ProposalService.submit`. Reusing deterministic inputs/derivations is safer and more immediately bounded than treating the preflight certificate as blanket authorization. Any later evaluation reuse must bind all mutable evidence and authority inputs; no checks should be skipped merely because a certificate matches.
4. **Instrument the complete served prepare/submit path before claiming end-to-end savings.** SDK `changes.prepare()` includes compilation plus served create/preflight. Coordinator store transitions, original reference validation, serialization, transport, daemon concurrency, actual workspace fetch and publication-lock wait remain outside these isolated measurements.

## Separate confirmed contract issue

The existing public GET response tags its intent **playbill-authoring-intent-v2** but omits **reference_expectations**, a required field of `AuthoringIntentV2`; direct V2 validation fails. The public outer response stores `intent` as a dictionary, so the SDK accepts this incomplete inner document. The internal `AuthoringIntentViewV1.intent` is V1-typed while holding V2 instances, consistent with subtype fields being dropped by default Pydantic serialization. This issue merits a focused wire round-trip fix and regression test separately from performance work. No repair was made during this diagnosis.

## Reproduction artifacts

- `/private/tmp/playbill-checkpoint-latency.py`: isolated review reconciliation harness.
- `/private/tmp/playbill-checkpoint-preflight.py`: copied semantic payload preflight harness, explicitly empty reference assertions.
- `/private/tmp/playbill-checkpoint-latency/{reconcile_cold,reconcile_warm,preflight_cold,preflight_warm}.{txt,pstats}`: full diagnostic profiles.
- `/private/tmp/playbill-checkpoint-intent.json`: private temporary public intent GET response (no credentials).

These scripts reuse the existing public-key-copy loader `/private/tmp/playbill-profile-next.py`. They do not attach a workspace or initiate mirror publication. No implementation change or commit belongs to this diagnostic.
