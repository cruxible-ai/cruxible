# Playbill TauBench arm recipe

The executable setup for the four §11.8 arms. A TauBench integrator needs this
directory and nothing else: `recipe.py` is every step, `seed-example/` is a small
real world to seed, and the smoke test in
`tests/test_cli/test_playbill_taubench_arms.py` drives the whole thing end to
end at miniature scale on every suite run.

Unlike `benchmarks/playbill_adoption_scale/`, this is not a measured gate and
takes seconds rather than minutes. It is the *setup* the measurement runs on.

## The four arms

| Arm | Label | Workspace |
|---|---|---|
| 1 | `files` | the task corpus, ordinary files and tools |
| 2 | `files+scratchpad` | the same, plus a real scratchpad directory (the notebook control) |
| 3 | `playbill-surface` | the same, plus the exported Playbill file surface; the coverage middleware is **constructed and never called** |
| 4 | `playbill-surface+coverage-delivery` | byte-identical to arm 3; the middleware's `after_tool` is called |

§11.8 requires that arms 3 and 4 "share the same model, harness loop, task
corpus, accepted ledger, Playbill state, and tool implementations; only the
coverage-delivery adapter changes." That is enforced rather than intended:
`build_arm` produces the two `ArmSetupV1` records through one code path and they
differ in exactly one field, the boolean `deliver_coverage`. `run_turn` reads
that boolean to decide whether to call `after_tool`, and there is no other
branch anywhere. The smoke test asserts both halves — one differing field, and
byte-identical workspaces.

## Running it

```bash
uv run python benchmarks/playbill_taubench/recipe.py \
    --root /path/to/scratch --server-url http://127.0.0.1:8121
```

It prints the arm-3 and arm-4 transcripts and writes `run-manifest.json` into
the scratch root. To drive the steps yourself, import them:

```python
from recipe import bootstrap, seed, export_arm_surface, build_arm, run_turn, run_manifest
```

## What each step does

1. **`bootstrap`** — `playbill host create` then `playbill init`, generating the
   `operator` creator and an independent `reviewer` in client custody. The server
   URL is required, never inferred, so two arms cannot silently seed against
   different worlds.
2. **`seed`** — reads the pure bundle plan, then drives ClaimTypes, Subjects and
   the QueryDefinition through their ordinary proposal surfaces and Claims and
   the Procedure through AuthoringIntent. The independent `reviewer` signs every
   proposal before the harness activates it. One write settles at a time because
   two proposals opened against one head cannot both activate.
3. **`export_arm_surface`** — `playbill floor export`, producing the pointer-model
   floor-v2 artifacts and §11.6.3 coverage boundary in one greppable tree. The
   unshipped native markdown projection is deliberately not part of either arm.
4. **`build_arm`** — materializes each arm's workspace from the bundle's
   committed corpus, and for arms 3 and 4 also copies the exported surface,
   writes `.playbill/coverage.json`, and constructs the middleware over the
   `_resolver` embedding (verbatim from PC-G-H2: observations in, one frozen
   coverage result out, over the ordinary served operation).
5. **`run_turn`** — a canned agent turn of Read, Grep, and Edit events in the
   four-kind vendor-neutral vocabulary, returning what the model would have seen.
6. **`run_manifest`** — pins everything §11.8 requires pinned per run.

## The seed bundle

`seed-example/` is a bundle directory: authoring JSONs under `claim-types/`,
`subjects/`, `claims/`, `query-definitions/`, and `procedures/`, plus a `bodies/`
subtree that is stored in CAS before anything cites it. It seeds three work-item
Subjects, one ClaimType, three status Claims — two of them citing spans of the
committed foreign corpus through PC-G-H1 foreign-source selections — one named
query, and one owner-carried query-only Procedure.

`bodies/` mirrors the working tree the Claims were authored against, so
`bodies/corpus/handbook.md` becomes `corpus/handbook.md` in every arm's
workspace. The Claim's `logical_source_identity` is `corpus.handbook.md`, and
the coverage config's prefix rule produces exactly that string from the working
path through the named non-lossy `playbill-coverage-path-identity-v1`
normalizer. Nothing on either side infers it; the two declarations agree because
they were written to.

The current ClaimInput bundle plans and settles as **nine** proposals:

```text
1. claim_type:project.work_item.status  [playbill_propose_claim_type]
2-4. subject:project.work_item/wi-10{1,2,3}  [playbill_authoring_submit]
5-7. claim_input:project.work_item/wi-10{1,2,3}#project.work_item.status
     [playbill_authoring_submit]
8. query_definition:project.work_items  [playbill_authoring_submit]
9. procedure:project.work_item.digest  [playbill_authoring_submit]
```

The old direct Claim batch and seed-apply adapter are retired. The recipe calls
the pure `plan_seed_directory` function to pin and render the offline plan, then
uses only the sanctioned writers. Coordinator writes retain their durable,
content-addressed retry behavior.

## What the two arms actually see

Identical raw tool output, and one addendum. Arm 3, verbatim:

```text
--- [Read] ---
5	The reviewer accepted the migration plan on the second reading.
--- [Grep] ---
corpus/handbook.md:5:The reviewer accepted the migration plan on the second reading.
--- [Edit] ---
The file corpus/handbook.md has been updated successfully.
```

Arm 4, same events, same edit, same turn:

```text
--- [Read] ---
5	The reviewer accepted the migration plan on the second reading.
exact  external:corpus.handbook.md  lines 5-5  commitment sha256:e0a5fa…  claims claims/83/CLM-…yaml  captures sha256:b42285…  at generation sha256:780279…  dependents 1
Playbill coverage: 1 exact, 0 drifted, 0 candidates, 0 none
…
--- [Edit] ---
The file corpus/handbook.md has been updated successfully.
drifted  external:corpus.handbook.md  expected sha256:e0a5fa…  observed sha256:808679…  claims claims/83/CLM-…yaml  captures sha256:b42285…  at generation sha256:780279…  dependents 1  [commitment_superseded]
Playbill coverage: 0 exact, 1 drifted, 0 candidates, 0 none
…
```

That is the §11.8 flagship outcome: the agent edited a governed span and the
affected Claim was named in the tool result it was already reading, within the
same turn, with nothing compiled, proposed, or accepted and the accepted
coordinate identical across the whole transcript. The original tool output is
preserved and annotated, never replaced — structurally, because the middleware
returns the original and the addendum as two separate strings and the caller
joins them.

## The run manifest

§11.8: "resolver, index, manifest, hook-adapter, and accepted-generation
versions are pinned per run." Each field is read off an artifact the run
produced rather than restated from configuration:

| Field | Source |
|---|---|
| `coverage.index_digest` / `overlay_digest` / `manifest_digest` / `epoch` | a coverage result the hooked arm received |
| `accepted.generation_root` / `semantic_root` / `compiler_digest` / `floor_digest` | the floor manifest's own coordinate |
| `accepted.format` | the floor manifest (`playbill-floor-export-v2`) |
| `hook_adapter.rule_set` / `rule_set_digest` | the declared bindings |
| `hook_adapter.envelope_version` | `null` — the owned-harness middleware has no vendor hook envelope, recorded as absent rather than omitted |
| `seed.plan_digest` | the bundle's bytes and grouping, excluding the invocation's proposal name |

`seed.applied_plan_digest` is what the run actually applied and must equal
`seed.plan_digest`; two arms printing the same digest seeded the same world.

## Determinism

No wall clock reaches projected content. The run's evaluation label has a fixed
default (`RUN_EVALUATION_TIME`), the bundle is committed bytes, and the plan
digest is a pure function of those bytes. Two runs of the same bundle at the
same generation produce the same manifest apart from the identifiers the daemon
allocates.
