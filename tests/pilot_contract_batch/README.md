# Pilot scenarios for the contract batch

End-to-end scenarios for the batch that makes Procedure slots name capabilities
(design note `design-contract-interface-1112.md`). Each one prints a short
transcript a person reads, records a few cost numbers, and is rerun on every
merge into `rc/0.5.0`. The maintainer approves transcripts, not test code.

## Running

From a core checkout, with the shared CPU lock:

```sh
pytest-lock.sh nice -n 15 env \
  PILOT_OUT=~/tmp-workspaces/coordinator-scratch/pilot-results/<commit>/ \
  CRUXIBLE_KITS_CHECKOUT=/path/to/cruxible-kits \
  CRUXIBLE_PROVIDERS_CHECKOUT=/path/to/cruxible-providers \
  PYTHON -m tests.pilot_contract_batch            # all
  PYTHON -m tests.pilot_contract_batch s4         # one (baseline, s1 ... s8)
```

`python -m tests.pilot_contract_batch` runs pytest on the chosen files with a
scratch `HOME`, `CRUXIBLE_STATE_ROOT` and temporary tree (deleted afterwards;
`--keep` keeps them), four workers (`--serial` for one), `PILOT_REQUIRED=1`
(a missing checkout fails rather than skips), and prints every transcript.

Plain pytest works too; then set `PYTHONPATH` yourself:

```sh
PYTHONPATH=src:packages/cruxible-client/src:.:$CRUXIBLE_PROVIDERS_CHECKOUT/packages/cruxible-provider-runtime/src:$CRUXIBLE_KITS_CHECKOUT/kits/security/source/pilot \
  PYTHON -m pytest -p no:cacheprovider -n 4 --dist loadfile tests/pilot_contract_batch
```

The providers checkout's own runtime must come before any installed copy: the
web adapter at that checkout imports names an older installed runtime lacks.
The kit gate's directory is on the path because its modules import each other
by name (committed tests may not change `sys.path` themselves).

| Variable | Needed by | Meaning |
|---|---|---|
| `CRUXIBLE_KITS_CHECKOUT` | baseline, s6 | a cruxible-kits checkout (`kits/security` at the release under test) |
| `CRUXIBLE_PROVIDERS_CHECKOUT` | baseline, s6 | a cruxible-providers checkout; the kit's recorded commit is `build_release.PROVIDERS_COMMIT` |
| `PILOT_OUT` | all | where transcripts and `results.tsv` go (default: a new temporary directory) |
| `PILOT_COMMIT` | all | the commit written in each row (default: this checkout's `HEAD`) |
| `PILOT_REQUIRED` | all | set: a missing checkout is a failure, not a skip |

Nothing reads or writes `~/.cruxible*`, and nothing touches the network: the
baseline and scenario 6 refuse every Internet socket and answer every provider
call from recordings already in the kits and providers checkouts. The kits
checkout is read with bytecode writing off.

## Output

- `$PILOT_OUT/<scenario>.txt`: the transcript. A scenario that is not green yet
  shows what ran, the `STOPPED:` line naming the missing call and its owner,
  and the transcript expected when green.
- `$PILOT_OUT/results.tsv`: `commit  scenario  status  measure  value`. One row
  per scenario (measure `-`) and one per cost measure. `status` is `passed`,
  `xfailed` (still an expected failure), `failed` or `skipped`.

## Scenarios and owners

| Scenario | What it shows | Owner | Helper the owner implements |
|---|---|---|---|
| `baseline` | security kit installed into a fresh instance, `security.start` run offline; OSV `matched`/`affected` as authored today; `affected_by` Claims written; costs | none: passes today | none |
| `s1-base-kit` | a new instance knows the base kit's seven capabilities and no providers; init opened no socket | base kit (slice 9) | `list_capabilities` |
| `s2-capability-slots` | a slot naming a capability is stored as one pin; an unknown capability is refused at authoring | slots in authoring (slice 2) | `author_capability_procedure` |
| `s3-provider-install` | a provider installs with no kit present and lists what it serves, with descriptions and sources | registration and install (slice 3) | `install_provider_package` |
| `s4-binding` | a fitting operation binds; three that do not fit get the note's §2.6 messages; binding cost with 1 and 50 other providers | binding (slice 4) | `accept_line` |
| `s5-swap` | swapping one fitting provider for another keeps the Claims and evidence; receipts record the capability pin and `fit` | binding (slice 4) with Capture-as-field (slice 6) | `run_slot_records` |
| `s6-thin-extract` | the OSV Procedure with a thin EXTRACT over the kind's layout writes the baseline's Claims in the baseline's time | layout and thin EXTRACT (slice 5) | `adopt_layout_and_thin_extract` |
| `s7-coverage` | a derive whose layout lacks the kind's record set is refused at binding; nothing retires | coverage (slice 7) | `layout_coverage_gap` |
| `s8-kit-requires` | a kit requiring `base` installs only beside it; `kit remove base` refused under it | kit requires kit (slice 10) | `kit_requires` |

Every helper lives in `surfaces.py` and raises `SurfaceMissing` until its owner
replaces the body. Scenarios 2, 4, 5, 7 and 8 start from the base kit
(`list_capabilities`); 4, 5 and 7 also use `author_capability_procedure`,
`install_provider_package` and `accept_line`. Until those land, they stop at
the first missing one, and the transcript says which.

The test providers for scenarios 3, 4, 5 and 7 are in `fixtures/providers.py`:
plain code plus a schema-3 registration each. Copies of base-kit shapes are read
from `src/cruxible_core/kits/base/` byte for byte, as a provider build copies
them. `examples/` belongs to the shared-models slice, not to this directory's
scenarios.

## Rules for owners

1. Implement your helper in `surfaces.py`, remove the `xfail` marker from your
   scenario, make it pass, and put its transcript in your report.
2. **A scenario is never weakened to pass.** Do not edit, loosen or delete an
   assertion, a refusal text check or a cost bound. Mechanics that only get the
   scenario to its assertions (how an instance is created, how a definition is
   authored) may change where the shipped surface differs from what the scenario
   assumed. If a scenario itself is wrong, say so in your report and leave it.
3. Cost measures are recorded when a scenario runs to its end; they are the
   numbers later merges compare.
