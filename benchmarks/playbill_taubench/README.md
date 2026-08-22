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

1. **`bootstrap`** — `playbill host create` then `playbill init`. The server URL
   is required, never inferred, so two arms cannot silently seed against
   different worlds.
2. **`seed`** — `playbill seed apply --plan`, then for each planned group:
   `apply --group`, `proposal approve`, `proposal activate`. The loop is the
   harness's, because approval and activation are separate governed acts and the
   seed command performs neither. One proposal per invocation, because a
   proposal settles against the base it was admitted at and two proposals opened
   against one head cannot both activate.
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
committed foreign corpus through PC-G-H1 foreign-source selections — one
ordinary `knowledge.brief` Claim, one named query, and one owner-carried
query-only Procedure.

`bodies/` mirrors the working tree the Claims were authored against, so
`bodies/corpus/handbook.md` becomes `corpus/handbook.md` in every arm's
workspace. The Claim's `logical_source_identity` is `corpus.handbook.md`, and
the coverage config's prefix rule produces exactly that string from the working
path through the named non-lossy `playbill-coverage-path-identity-v1`
normalizer. Nothing on either side infers it; the two declarations agree because
they were written to.

The bundle plans as **four** proposals:

```text
1. claims  [playbill_propose_claims]  3 Claim(s) and every dependency they carry
     settle as one generation through the batch operation
2. claim_input:...knowledge.brief...  [playbill_authoring_submit]
3. query_definition:project.work_items  [playbill_propose_query_definition]
4. procedure:project.work_item.digest  [playbill_authoring_submit]
carried  claim-types/project.work_item.status.json  admitted by claims/wi-101-status.json
carried  subjects/wi-101.json  admitted by claims/wi-101-status.json
carried  subjects/wi-102.json  admitted by claims/wi-102-status.json
carried  subjects/wi-103.json  admitted by claims/wi-103-status.json
```

The ClaimType and all three Subjects cost no proposal at all, because the Claim
authorings declare them as dependency closures and the batch admits them in the
same generation. The Brief and Procedure use one existing coordinator intent
each; the QueryDefinition uses its singular propose operation. Run
`cruxible playbill seed apply seed-example --name NAME --plan` to see it; the
plan is offline and reaches no daemon. `NAME` is a human run label. Applying a
direct-propose group uses a machine-owned `seed-<digest>` proposal ref derived
from the plan digest and group id. Coordinator groups reuse the same durable
intent by its content-addressed create fingerprint. In both cases a lost-response
retry converges on the same open proposal instead of opening a duplicate.

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
