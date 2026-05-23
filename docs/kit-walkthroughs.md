# Kit Walkthroughs

This page shows the two common authoring paths for `0.2`: create a standalone
kit from scratch, and customize an overlay kit on top of an existing reference
world.

For manifest rules and distribution behavior, see
[Kit Authoring And Distribution](kit-authoring.md).

## Walkthrough 1: Create A Standalone Kit

Use a standalone kit when the domain can initialize a world model by itself.

### 1. Create The Kit Directory

```text
my-risk-kit/
  cruxible-kit.yaml
  config.yaml
  cruxible.lock.yaml
  providers/
    risk_seed.py
  data/
    assets.csv
```

Minimal manifest:

```yaml
schema_version: cruxible.kit.v1
kit_id: my-risk-kit
version: 0.2.0
role: standalone
entry_config: config.yaml
provider_paths:
  - providers
copy_paths:
  - data
  - README.md
requires_extras: []
```

### 2. Define A Small World Model

Start with one entity, one relationship, and one query. Add contracts,
artifacts, providers, and workflows only after the graph shape is clear.

```yaml
schema_version: cruxible.config.v1
name: my_risk_kit
kind: world_model
version: "0.2.0"

entity_types:
  Asset:
    properties:
      asset_id: {primary_key: true}
      hostname: {}
  Owner:
    properties:
      owner_id: {primary_key: true}
      name: {}

relationships:
  - name: asset_owned_by
    from: Asset
    to: Owner
    cardinality: many_to_one

named_queries:
  asset_owner:
    entry_point: Asset
    returns: Owner
    result_shape: path
    traversal:
      - relationship: asset_owned_by
        direction: outgoing
```

For operational queries, keep the traversal focused on the primary question and
use `include` for bounded side context that should travel with each result row,
such as owners, services, controls, exceptions, or patch windows. Use
`required: false` only for optional follow-on traversal where a matched neighbor
should become the next `$result`.

### 3. Add A Provider Only For Source Adaptation

Provider refs should be kit-relative:

```yaml
providers:
  normalize_assets:
    kind: function
    contract_in: RawAssetRows
    contract_out: AssetOwnerRows
    ref: kit://providers/risk_seed.py::normalize_assets
    version: "1.0.0"
    deterministic: true
    runtime: python
```

Use built-in workflow step types for generic mechanics such as row shaping,
joins, filtering, dedupe, entity creation, relationship creation, and canonical
apply. Keep providers focused on messy source formats or domain policy.

### 4. Validate, Initialize, Lock, And Run

```bash
cruxible validate --config my-risk-kit/config.yaml
cruxible init --kit file://./my-risk-kit
cruxible lock
cruxible run --workflow build_local_state --save-preview preview.json
cruxible apply --preview-file preview.json
cruxible query --query asset_owner --param asset_id=ASSET-1
```

Inspect the returned receipt:

```bash
cruxible explain --receipt <receipt-id> --format markdown
```

### 5. Refresh Generated Docs

```bash
cruxible config-views --config my-risk-kit/config.yaml --runtime \
  --update-readme my-risk-kit/README.md
```

The generated blocks are structural truth. Keep authored prose outside
`CRUXIBLE:BEGIN` / `CRUXIBLE:END` markers.

## Walkthrough 2: Customize An Overlay Kit

Use an overlay kit when you want local state on top of a published reference
world. KEV triage is the canonical example.

### 1. Create The Overlay

```bash
cruxible world create-overlay \
  --world-ref kev-reference \
  --kit kev-triage \
  --root-dir "$PWD/kev-triage-workspace"
```

The resulting instance tracks the KEV reference world and materializes local
triage config, providers, source data, skills, and lock state.

If you are testing from a source checkout without published OCI reference
worlds, publish a local `kev-reference` release to `file://...` first and use
`--transport-ref file://...` instead of `--world-ref kev-reference`.

### 2. Add Local State

In your customized kit copy, add customer-owned source data under `source_data/`
or `data/`, then model it in the overlay config:

```yaml
entity_types:
  MaintenanceTeam:
    properties:
      team_id: {primary_key: true}
      name: {}

relationships:
  - name: asset_supported_by_team
    from: Asset
    to: MaintenanceTeam
    cardinality: many_to_one
```

### 3. Add A Proposal Workflow For Judgment

If the relationship is inferred, matched, classified, or reviewable, do not
write it directly as accepted state. Add a provider or workflow step that emits
proposal members, then use a `propose_relationship_group` workflow step so the
result enters pending review.

For KEV-style workflows, the path is:

```text
source artifact
  -> parse/shape/filter/join/dedupe
  -> domain evidence provider if needed
  -> make_candidates
  -> map_signals with support/unsure/contradict evidence
  -> propose_relationship_group
  -> candidate group
  -> human or agent-assisted resolution
```

### 4. Lock, Preview, Propose, And Resolve

```bash
cruxible lock
cruxible run --workflow build_local_state --save-preview preview.json
cruxible apply --preview-file preview.json
cruxible propose --workflow propose_asset_products
cruxible group list --status pending_review
cruxible group get --group <group-id>
cruxible group resolve \
  --group <group-id> \
  --action approve \
  --expected-pending-version <pending-version> \
  --rationale "Reviewed evidence and accepted the proposal"
```

### 5. Query The Accepted Result

```bash
cruxible query \
  --query vulnerability_asset_context \
  --param cve_id=CVE-2020-1472
```

Use the receipt to explain how public vulnerability-product state connected to
local asset-product state.

### 6. Ship The Customized Kit

For local testing, use a `file://` ref:

```bash
cruxible init --kit file://./my-custom-kit
```

For distribution, publish the versioned bundle as an OCI kit ref and update the
catalog or deployment configuration that resolves the kit alias. Before
publishing, refresh the bundled `cruxible.lock.yaml`; until `cruxible kit lock`
exists, materialize the kit to a temp workspace, run `cruxible lock`, and copy
the lock back into the kit directory.
