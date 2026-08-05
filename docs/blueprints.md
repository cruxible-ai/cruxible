# Procedure blueprints (format reference)

A **blueprint** is a portable, digest-addressed document that packages a
procedure library: its own contracts, the state reads it depends on, the
compute stages it wants swapped in, and the procedures themselves. No code ever
travels in a blueprint — only definitions, contracts, requirements, and
metadata.

!!! warning "What ships today"

    This release ships the **artifact**, not the installer.

    - `cruxible_core.blueprint` parses and validates a document, computes its
      content digest, and *lowers* it into the objects an installer would
      submit: a config-overlay fragment and a list of `ProcedureDefinition`s.
    - **There is no installer.** Nothing here applies an overlay, proposes a
      procedure, records a binding, or writes an install receipt.
    - **There is no trigger runtime.** `triggers:` and `pipelines:` blocks
      parse and validate so a publisher can author the whole artifact, but
      lowering refuses them with a typed error. Procedures start only through
      the explicit run service.
    - **`invocation: manual` is the executable slice** — agent-invoked
      procedure libraries.
    - **There is no binding registry.** Lowering resolves compute slots from a
      binding map *you* pass it.

## The document

```yaml
blueprint:                          # catalog identity
  id: kev-triage                    # lowercase catalog id
  version: 1.0.0                    # semver — quote it so YAML keeps a string
  publisher: cruxible-ai
  description: >-
    Verified KEV triage: blast-radius check, exposure decision, receipts
    throughout.
  provenance:                       # optional
    origin: agent-authored          # agent-authored | curated | hybrid
    evidence: [receipt:RCP-0001, eval:EVL-0002]

contracts:                          # blueprints declare their own types
  cruxible-ai.kev-triage.TriageRequestInput:
    fields:
      cve_id: {type: string}
      asset_scope: {type: string, optional: true}

dependencies:
  reference_states:
    - {state_ref: kev-reference@2026.30}
  entity_types: [Vulnerability, Asset]
  relationship_types: [asset_runs_product]
  enums:
    - {name: criticality, ordered: low_to_high}
  kits:
    - {kit_id: kev-triage, min_version: 0.2.8}

query_slots:                        # read sockets: pinned interface, config impl
  blast_radius_services:
    install_as: cruxible_ai__kev_triage__blast_radius_services
    param_contract: cruxible-ai.kev-triage.CveScopeInput
    result_contract: cruxible-ai.kev-triage.QueryEnvelope
    row_contract: cruxible-ai.kev-triage.ServiceRow   # documentation-only today
    default:                        # installs as the named config query
      explicit: true                # optional marker; accepted and stripped
      mode: collection
      returns: BusinessService
      result_shape: entity

slots:                              # swappable COMPUTE stages, never reads
  exposure_assessment:
    contract_in: cruxible-ai.kev-triage.ExposureAssessmentInput
    contract_out: cruxible-ai.kev-triage.ExposureAssessmentResult
    billing: [platform, byok]       # compatibility constraint, not a price
    capabilities: [deterministic, no_side_effects]
    required: true                  # default
    outcome_metric:                 # opt-in scoring hook; publishes nothing yet
      outcome_profile: asset_vulnerability_posture_resolution
      metric: precision_recall

invocation: manual                  # document-level default

procedures:
  - name: kev_exposure_rescore
    description: Rescore exposure for one CVE.
    contract_in: cruxible-ai.kev-triage.CveScopeInput
    contract_out: cruxible-ai.kev-triage.ExposureAssessmentResult
    declared_tier: governed_write
    precondition: {}                # required, even when empty
    budget: {wall_clock_s: 120, max_provider_calls: 2}
    steps:
      - {id: services, query: blast_radius_services,
         params: {cve_id: $input.cve_id}, as: services}
      - {id: guard, assert_not_truncated: {step: services}}
      - {id: score, provider: exposure_assessment,
         input: {rows: $steps.services.results}, as: score}
    returns: score
    evidence_outputs: [score]

install_checks: [contracts_load, all_required_slots_bindable]
```

### Blocks

| Block | Required | Meaning |
|---|---|---|
| `blueprint` | yes | Catalog identity: `id`, semver `version`, `publisher`, optional `description` and `provenance`. |
| `contracts` | no | Blueprint-owned payload contracts, keyed by **fully qualified** name. |
| `dependencies` | no | What the target instance must already have. |
| `query_slots` | no | Read sockets. Each ships a `default` installed as a named config query. |
| `slots` | no | Swappable compute stages. Declared by contract, bound to a provider at install. |
| `invocation` | no | `manual` (default) or `triggered`. |
| `triggers` | no | Entry points. Parsed; not executable. |
| `pipelines` | no | Trigger-invoked procedure bodies. Parsed; not executable. |
| `procedures` | yes\* | Agent-invoked procedure bodies. |
| `install_checks` | no | Named preflight checks recorded for the installer. Not executed here. |

\* A document must declare at least one `procedure` or one `pipeline`.

### Contract names must be fully qualified

Every key under `contracts:` must be `<publisher>.<id>.<LocalName>` — the same
publisher and id the `blueprint:` block declares. Composition is strictly
additive and refuses redefinition, so unqualified names would make collisions
routine and uninstall ownership ambiguous.

Contract *references* (`contract_in`, `contract_out`, `param_contract`,
`result_contract`, `row_contract`) may name a blueprint-declared contract or a
builtin (`cruxible.EmptyInput`, `cruxible.JsonObject`, `cruxible.JsonItems`,
`cruxible.ParsedTabularBundle`). Anything else is refused with the resolvable
set listed.

### Slots are named by procedure steps, never providers

Inside a blueprint procedure, `provider:` names a **compute slot**, and a
string-valued `query:` names a **query slot**. Both are rewritten to concrete
config names at lowering. A `provider:` step naming something that is not a
declared slot is refused — a blueprint cannot reach for a provider that happens
to exist on some instance.

Inline query bodies are legal and expected for internal plumbing reads (join an
edge set, enumerate a type). Query *slots* are for reads the customer is
expected to adapt; a blueprint's slot count is a meaningful signal of its
customization surface, so do not promote plumbing to a slot.

### Reference-state dependencies are exact

```yaml
reference_states:
  - {state_ref: kev-reference}              # tracks the catalog's latest release
  - {state_ref: kev-reference@2026.30}      # pinned release
  - {alias: kev-reference, version: 2026.30}  # equivalent
```

Version **ranges** (`>=2026.30`, `~1.2`, `^2`) are refused: core's state-ref
grammar accepts `alias` or `alias@release` only. The refusal says so and tells
you to pin.

### Query-slot defaults use the explicit engine schema

A default is a `NamedQuerySchema` body in the engine's explicit form. Compact
query grammar (`traverse`, `traverse_all`, `bound`, `order`, `as`, `max_depth`,
`direction`) is refused, because compact expansion resolves against the
*deployed* ontology index — entity primary keys, relationship directions —
which a portable document cannot carry. An `explicit: true` marker is accepted
and stripped, so a body can be copied verbatim out of a compact kit config.

## Digest

```python
from cruxible_core.blueprint import load_blueprint

loaded = load_blueprint("kev-triage.blueprint.yaml", attachments=["guide.md"])
loaded.digest        # 'sha256:...'
```

The digest covers a **canonical** form of the document plus an ordered
attachment manifest:

- Keys are sorted and every schema default is materialized, so writing a
  default explicitly does not move the digest, and comments and key order never
  do.
- Any semantic change moves the digest.
- Attachments are digested individually and folded in ordered by manifest path,
  so the caller's enumeration order is irrelevant. Attachments must live under
  the blueprint's directory.
- Deployment facts are excluded **by construction**: the document carries slot
  billing-mode constraints, never a payer, quota, account, or bound provider.

`canonical_yaml(blueprint)` returns the canonical document as key-sorted YAML
for review and diffing.

## Lowering

```python
from cruxible_core.blueprint import ProviderCandidate, lower_blueprint

lowered = lower_blueprint(
    loaded.blueprint,
    bindings={"exposure_assessment": "kev_exposure_scorer"},
    candidates=[
        ProviderCandidate(
            name="kev_exposure_scorer",
            contract_in="cruxible-ai.kev-triage.ExposureAssessmentInput",
            contract_out="cruxible-ai.kev-triage.ExposureAssessmentResult",
        )
    ],
    digest=loaded.digest,
)

lowered.overlay.as_config_dict()   # {'contracts': {...}, 'named_queries': {...}}
lowered.procedures                 # [ProcedureDefinition, ...]
lowered.slot_bindings              # [ResolvedSlotBinding(slot=..., provider=...)]
```

`bindings` maps slot name → provider name. `candidates` is the catalog you
could have bound from; it is used only to explain failures.

A required slot with no binding is refused, and the refusal lists the
candidates that nearly matched and why each failed:

```
Compute slot 'exposure_assessment' could not be bound: no binding was supplied
for a required slot. The slot requires
contract_in='cruxible-ai.kev-triage.ExposureAssessmentInput' and
contract_out='cruxible-ai.kev-triage.ExposureAssessmentResult'. Near matches:
'kev_exposure_scorer' (...) — contracts match exactly; bind it explicitly
```

Matching is **nominal** in this release: a bound provider's declared contract
names must equal the slot's. That never binds across mismatched types, but it
also means a structurally identical provider under a different contract name
cannot bind. Structural (width-subtyping) matching needs a
contract-compatibility relation core does not have yet.

## Errors

Every refusal is typed and field-pathed, and echoes the allowed values or
expected shape.

| Error | Raised when |
|---|---|
| `BlueprintValidationError` | Document shape or cross-references are wrong. Carries `issues` (`path`, `message`, `expected`). |
| `BlueprintDigestError` | A document or attachment could not be read, or an attachment escapes the blueprint root. |
| `BlueprintUnsupportedError` | Valid, but names machinery core cannot execute (triggers, pipelines, `invocation: triggered`). Names the work item. |
| `BlueprintBindingError` | A required compute slot has no usable provider. Carries `near_matches`. |

All four derive from `BlueprintError`, which derives from `CoreError`.

## Known limits

These are format-level facts, not bugs to work around:

- **Query-slot result contracts type the envelope, not the rows.** A query
  step's output is the engine envelope and `results` is an opaque list, so the
  only expressible result contract is envelope-shaped. `row_contract` is
  carried and validated as a reference, but nothing enforces it against the
  bound query's projection yet.
- **A query slot cannot bind to a query the instance already has.** Only
  `default:` exists; `install_as` avoids colliding with a kit's query by
  installing a namespaced copy, which then drifts from the kit's maintained
  version.
- **Procedures cannot write.** The procedure step subset stops at
  query/provider/assert/transform; there are no proposal or write steps, so a
  blueprint procedure returns rows to its caller rather than landing pending
  proposals.
- **Publisher-frozen configuration has nowhere to live.** Policy literals
  inside a procedure body are covered by the digest and frozen by acceptance;
  there is no install-time `parameters:` block yet.
- **Kit providers with `runtime: python` cannot back a slot**, because
  procedures refuse python-runtime providers.
