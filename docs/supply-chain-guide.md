# Supply Chain: Blast Radius On Hard State

The supply-chain-blast-radius kit is a seeded incident blast-radius world:
suppliers, components, assemblies (a recursive BOM from a real open-hardware
design), products, and shipments as deterministic fact; incident impact as
governed judgment. Impact judgments stop at components and assemblies —
everything past them is traversal. Approve the staged impact edges once, and
`incident_exposed_shipments` walks them up an eight-level BOM into finished
products and out to shipments, with a receipt naming every edge walked. The
[README walkthrough](https://github.com/cruxible-ai/cruxible#what-a-governed-domain-looks-like)
explains why the judged/derived line sits where it does; this guide runs the
kit end to end (`jq` is the one prerequisite beyond `pip install cruxible`).

## 1. Start a daemon and create the instance

```bash
pip install cruxible
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/sandbox" cruxible server start   # shell 1

# shell 2 — kit bundles are fetched from the release and digest-verified
cruxible --server-url http://127.0.0.1:8100 init \
  --kit agent-operation --kit supply-chain-blast-radius
cruxible context connect --server-url http://127.0.0.1:8100 --instance-id <instance-id>
```

The domain composes over the agent-operation base, so response work and
supplier risks land as base WorkItems and Risks, not a parallel vocabulary.

## 2. Build the deterministic world

Canonical workflows are preview-first: `run` executes against a clone and
saves a digest-pinned preview; `apply` re-verifies it before committing.

```bash
cruxible run --workflow build_seed_state
cruxible apply --workflow build_seed_state --from-last-preview

cruxible run --workflow ingest_incidents
cruxible apply --workflow ingest_incidents --from-last-preview
```

The seed is the base world; incidents arrive as entities through their own
canonical feed. The impact edges are not written by either: every live
direct write is refused, at any permission tier, and this kit mints them
only through proposal and review. `write_policy: proposal_only` in the config
is an invariant enforced at the write chokepoint, not an instruction a
model follows with some probability.

## 3. Judge the cascade, stage by stage

The cascade is deliberately staged — incident → supplier → component /
direct assembly — and each stage's candidates traverse the *accepted* edges
of the previous one, so propose and review in order:

```bash
cruxible propose --workflow propose_incident_impacts_supplier
cruxible group list --status pending_review
cruxible group get --group <group-id>
cruxible group resolve --group <group-id> --action approve \
  --rationale "Verified: incident scope matches supplier geography" \
  --expected-pending-version <n>

cruxible propose --workflow propose_incident_impacts_component
# Review and approve the incident_impacts_component group, then:
cruxible propose --workflow propose_incident_impacts_assembly
# Review and approve the incident_impacts_assembly group.
```

Each member carries its cascade signal and evidence; `--expected-pending-version`
pins your decision to the pending state you reviewed. Bucket signatures are
rule-centric — they carry the cascade rule, not the incident — so trust
accumulates on the rule across incidents, and clean all-support cascades
auto-resolve once the rule has prior trusted resolutions. First runs always
stop for review.

## 4. Walk the blast radius

The accepted edges are now the standing work queue and the entry into the
derived exposure surfaces. The critical Guangdong incident in the seed
exposes three products and six shipments:

```bash
cruxible query run open_incident_impacts --json
cruxible query run incident_component_exposed_products \
  --param incident_id=INC-GD-STEPPER-2026-07 --json
cruxible query run incident_exposed_shipments \
  --param incident_id=INC-GD-STEPPER-2026-07 --json
```

Traversal results keep distinct BOM paths (hundreds, for this incident);
deduplicate on `result.entity_id` when the question is "which unique
shipments?". No incident→product or incident→shipment edge exists anywhere —
the exposure is derived per query from the judged impacts and the BOM. Every
result carries a receipt; render it to see each edge the traversal walked:

```bash
cruxible explain --receipt <RCP-id> --format markdown
```

Then narrow to what needs action first — impacted components with no viable
alternate supplier:

```bash
cruxible query run single_source_components_for_incident \
  --param incident_id=INC-GD-STEPPER-2026-07 --json
```

## Inventory, buffers, and response work

Inventory evidence, buffer coverage, and operations routing are separate
compute/apply seams: a utility workflow computes rows for review, and its
`output` object (just that object, not the full result envelope) feeds the
canonical sync workflow:

```bash
cruxible run --workflow refresh_inventory_positions --json \
  | jq '.output' > inventory-output.json
cruxible run --workflow sync_inventory_positions \
  --input-file inventory-output.json --save-preview inventory-preview.json
cruxible apply --preview-file inventory-preview.json
```

`refresh_buffer_assessments` → `sync_product_buffer_assessments` and
`analyze_operations_routing` → `apply_operations_routing` follow the same
pattern; the latter creates response WorkItems addressing each incident, and
`cruxible propose --workflow propose_risk_attaches_to_supplier` routes
supplier risk through review so "this supplier is shaky" is a judgment on
the record, not a vibe. The full workflow, query, and governance catalog is
in the [kit README](https://github.com/cruxible-ai/cruxible/tree/main/kits/supply-chain-blast-radius/).
