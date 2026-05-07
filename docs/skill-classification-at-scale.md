# Skill: Classification at Scale

Classify entities from an internal catalog against a standard taxonomy using deterministic providers, relationship-local signal sources, batch group review, and a trust flywheel. LLM reasoning is limited to writing configs, handling ambiguous tails, and critiquing results.

## When to use this skill

You have two datasets:
- An **internal catalog** with free-text descriptions and informal categories
- A **standard taxonomy** with structured types/categories

You need to create classification edges between them — at scale, with receipts, and with minimal ongoing human review.

## Architecture boundary

```
You (the agent):
  - Write the config (entity types, providers, proposal policy)
  - Build or invoke deterministic providers (regex, lookup tables, rules)
  - Run workflows against entities to produce structured extractions
  - Convert extraction results to tri-state signals (support/unsure/contradict)
  - Propose groups with thesis_facts derived from extractions
  - Run the review loop (sample, critique, refine rules, regroup)

Core:
  - Stores provider identity, contracts, and workflow traces
  - Validates signals against relationship proposal policy
  - Derives review priority from signal + trust state
  - Manages group lifecycle (propose → resolve → trust)
  - Produces receipts for every mutation
  - Gates auto-resolve on thesis-scoped trust

Core executes declared providers inside workflows, but it does not own the
domain-specific classification logic. The provider implementation and contract
are the spec of record; relationship `proposal_policy.signals` decides which
signal-source labels govern review.
```

## Phase 1: Understand the data

Before writing any config, profile both datasets.

```
1. Read headers, row counts, sample rows from both files
2. Identify primary keys (catalog part number, taxonomy type ID)
3. Profile the description fields — look for shorthand patterns, abbreviations
4. Count distinct categories/subcategories on both sides
5. Look for existing classification columns (may be partially populated)
6. Identify junk rows (discontinued, dropbox, test data)
```

The description profiling is critical. Catalog shorthand has structure:

```
"65-66 MUST FB RR QTR TRIM 5PC"
 ^^^^  ^^^^ ^^ ^^ ^^^ ^^^^ ^^^
 year  car  body pos part type count

Extract the part noun and qualifiers — these drive both matching and grouping.
```

## Phase 2: Write the config

### Entity types

One entity type per dataset. Properties should capture everything needed for matching — descriptions, categories, subcategories.

```yaml
entity_types:
  CatalogPart:
    properties:
      part_number: {primary_key: true}
      short_desc: {}
      long_desc: {}
      category: {}
      sub_category: {}

  TaxonomyType:
    properties:
      type_id: {primary_key: true}
      type_name: {}
      category_name:
        type: string
      sub_category_name:
        type: string
```

### Providers And Signal Sources

Declare deterministic providers with contracts that fully specify their rules. A provider can emit rows that workflow `map_signals` steps convert into relationship-local signal sources. Re-running a deterministic provider against the same entity should produce the same signal.

```yaml
contracts:
  CatalogRows:
    fields:
      items: {type: json}

providers:
  keyword_extract_v1:
    contract_out: CatalogRows
    ref: kit://providers/classification.py::keyword_extract_v1
    version: 1.0.0
    deterministic: true
```

### Relationship With Proposal Policy

```yaml
relationships:
  - name: classified_as
    from: CatalogPart
    to: TaxonomyType
    properties:
      confidence_basis: {}
      match_detail: {}
    proposal_policy:
      signals:
        keyword_extract_v1:
          role: required
        category_map_v1:
          role: required
        llm_review_v1:
          role: advisory
          always_review_on_unsure: true
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_only
      max_group_size: 200
```

This policy means:
- Every group member must have signals from `keyword_extract_v1` and `category_map_v1`
- `llm_review_v1` is advisory — its signal is recorded but doesn't gate approval
- If the LLM returns `unsure`, the group gets `always_review_on_unsure` escalation
- Auto-resolve requires all signals `support` AND a prior `trusted` resolution for the same signature

### Source Loading Workflow

```yaml
workflows:
  refresh_catalog_state:
    canonical: true
    steps:
      - id: parsed
        provider: catalog_csv_loader
        input: {}
        as: parsed
      - id: catalog_rows
        shape_items:
          items: $steps.parsed.tables.catalog.rows
          rename:
            ShortDesc (Max 30): short_desc
          fields:
            part_number: $item.Detroit P/N
            short_desc: $item.short_desc
            long_desc: $item.LongDesc
        as: catalog_rows
      - id: catalog_entities
        make_entities:
          entity_type: CatalogPart
          items: $steps.catalog_rows.items
          entity_id: $item.part_number
          properties:
            short_desc: $item.short_desc
            long_desc: $item.long_desc
        as: catalog_entities
      - id: apply_catalog
        apply_entities:
          entities_from: catalog_entities
        as: apply_catalog
```

## Phase 3: Refresh Canonical State

```
cruxible_validate(config_path="config.yaml")
cruxible_init(root_dir=".", config_path="config.yaml")
cruxible_lock_workflow(instance_id, workflow_name="refresh_catalog_state")
cruxible_run_workflow(instance_id, workflow_name="refresh_catalog_state", input_payload={})
cruxible_apply_workflow(instance_id, workflow_name="refresh_catalog_state", apply_digest="...")
```

The workflow preview and apply path produces receipts, provider traces, and a canonical state transition.

## Phase 4: Run Providers And Build Signals

Read the provider contracts from the config. Execute provider-backed workflows deterministically against the catalog entities, then use `map_signals` steps to create tri-state evidence.

### Step 1: Run keyword extraction

Read `keyword_extract_v1.contract.rules`. For each CatalogPart, apply patterns against `short_desc` and `long_desc`:

```
Part 31112C: "65-66 MUST FB RR QTR TRIM 5PC"
  → matches rule "quarter_panel"
  → extraction: {part_noun: "trim_panel", qualifier: "quarter"}
  → signal: support

Part B1803P42: "77-78 2DR MODELS LGHT GRN C/P"
  → no rule matches
  → signal: unsure
```

### Step 2: Run category mapping

Read `category_map_v1.contract.mappings`. For each CatalogPart, look up its category/sub_category:

```
Part 31112C: category="Interior Soft Goods", sub_category="Door Panels & Components"
  → candidates: [10006, 12730, 11409]
  → cross-reference with extraction qualifier "quarter" → narrows to 12730
  → signal: support

Part with category="#Dropbox"
  → no mapping
  → signal: unsure
```

### Step 3: LLM only for unsure cases

Check the `llm_review_v1.contract.use_when` condition. Only invoke LLM for parts where keyword extraction returned `unsure` OR category mapping left >3 candidates.

For the rest — the parts where both deterministic signal sources returned `support` — no LLM call is needed.

### Step 4: Build member signal lists

Each member needs signals from every required signal source:

```python
member = {
    "from_type": "CatalogPart",
    "from_id": "31112C",
    "to_type": "TaxonomyType",
    "to_id": "12730",
    "relationship_type": "classified_as",
    "signals": [
        {"signal_source": "keyword_extract_v1", "signal": "support",
         "evidence": "matched rule quarter_panel: part_noun=trim_panel qualifier=quarter"},
        {"signal_source": "category_map_v1", "signal": "support",
         "evidence": "Interior Soft Goods/Door Panels → [10006,12730,11409] narrowed by qualifier=quarter"},
    ],
    "properties": {"confidence_basis": "keyword+category", "match_detail": "quarter_panel→12730"}
}
```

## Phase 5: Group and propose

Group members by their **extracted features** — these become `thesis_facts` and determine the signature. Same thesis_facts = same signature = same trust track.

### Choosing the grouping key

Group by the combination of fields that represents a repeatable matching pattern:

```
thesis_facts: {
    "part_noun": "trim_panel",
    "qualifier": "quarter",
    "target_type_id": "12730"
}
```

All "quarter trim panels classified as taxonomy type 12730" share a signature. This means:
- They get reviewed as one batch (not individually)
- Trust earned on this group applies to future batches with the same pattern
- If trust is invalidated, all future batches of this pattern go back to review

### Seed-then-fan-out

If a signature has more members than `max_group_size`, you'll need multiple
chunks. **Do not propose all chunks at once.** Core checks for trusted prior
resolutions at proposal time. If no trust exists yet, every chunk lands as
`pending_review` and you'd have to manually resolve each one — completely
defeating the flywheel.

The right pattern:

1. **Propose a few seed chunks** per unique signature — enough to get
   diversity for spot-checking (2-3 chunks, not all of them).
2. **Resolve the seeds** — review the thesis, inspect sample members, approve.
3. **Spot-check edges across the resolved seeds**, promote to trusted
   (see Phase 7).
4. **Then propose the remaining chunks** for that signature. Core finds the
   prior trusted resolution → `auto_resolved`. You just call resolve to
   write the edges.

This makes the first run dramatically cheaper. Instead of reviewing 65
identical floor-carpet groups, you review ~15-20 unique signatures and the
rest auto-resolve.

### Propose a group

```
cruxible_propose_group(
    instance_id,
    relationship_type="classified_as",
    members=[...list of members with signals...],
    thesis_text="Quarter trim panel parts classified as TaxonomyType 12730",
    thesis_facts={"part_noun": "trim_panel", "qualifier": "quarter", "target_type_id": "12730"},
    analysis_state={
        "extraction_version": "keyword_extract_v1",
        "category_map_version": "category_map_v1",
        "member_count": 45,
        "unsure_count": 2,
        "sample_parts": ["31112C", "K1960", "62118E"]
    },
    signal_sources_used=["keyword_extract_v1", "category_map_v1"]
)
```

`analysis_state` is opaque to Core — it's NOT hashed into the signature. Use
it to stash extraction context, LLM reasoning, and anything an agent might
need when revisiting this group later. It's returned by `cruxible_get_group`
and `cruxible_list_resolutions`.

### What Core does with the proposal

Core checks:
1. All required signal-source signals present on every member
2. `max_group_size` not exceeded
3. No duplicate members
4. Signal values from relationship-local signal sources only

Then derives `review_priority`:
- Blocking signal source contradicts → `critical`
- Prior trust invalidated → `critical`
- Unsure on required signal source → `review`
- No prior resolution → `review`
- Everything clean + prior trusted → auto-resolve eligible

If auto-resolve conditions are met (all support + prior trusted), group status
is set to `auto_resolved`. Otherwise `pending_review`.

## Phase 6: Resolve seed groups

```
cruxible_list_groups(instance_id, relationship_type="classified_as", status="pending_review")
```

Groups are sorted by review_priority (critical first). For each seed group:

```
cruxible_get_group(instance_id, group_id)
```

Returns the thesis, members with signals, and review_priority. Review the
thesis and sample members, then resolve:

```
cruxible_resolve_group(instance_id, group_id, action="approve",
    rationale="Quarter trim panel mapping verified against PIES taxonomy")
```

This creates classification edges for all valid members, with receipts and
provenance.

## Phase 7: Review, trust, and fan out

After resolving seed groups, verify the edges before promoting to trusted.
The flywheel works *within* the first run — not just on future refreshes.

### Spot-check edges

Scale the check count to the blast radius — trusting a signature that covers
3,000 edges based on 10 checks is reckless. Pull samples **across multiple
resolved groups** for the same signature, not just from one seed.

```
| Total edges for signature | Minimum spot-checks | Pull from          |
|---------------------------|---------------------|--------------------|
| < 50                      | 5                   | single group is ok |
| 50 – 500                  | 10-15               | 2+ groups          |
| 500 – 5,000               | 20-30               | 3+ groups          |
| > 5,000                   | 40-50               | all resolved seeds |
```

For each sampled member:

```
cruxible_get_relationship(instance_id,
    from_type="CatalogPart", from_id="31112C",
    relationship="classified_as",
    to_type="TaxonomyType", to_id="12730")

cruxible_get_entity(instance_id, "CatalogPart", "31112C")
```

### Critique with LLM

Send the sample to an LLM with the thesis and ask: "Are these classifications
correct given the part descriptions and the taxonomy type?"

### If correct — promote to trusted and fan out

```
cruxible_update_trust_status(instance_id, resolution_id, "trusted",
    reason="Spot-checked 25 edges across 3 groups, all correct")
```

Now propose the remaining chunks for this signature. Core finds the prior
trusted resolution → `auto_resolved`:

```
# Propose remaining chunks — they auto-resolve
for chunk in remaining_chunks_for_signature:
    cruxible_propose_group(instance_id, "classified_as",
        members=chunk, thesis_facts=same_thesis_facts, ...)

# Write the edges
cruxible_list_groups(instance_id, status="auto_resolved")
# For each:
cruxible_resolve_group(instance_id, group_id, action="approve",
    rationale="Auto-resolved: prior trusted pattern")
```

### If errors found — fix and regroup

```
# 1. Reject misclassified edges
cruxible_feedback(
    instance_id,
    receipt_id=<resolve_receipt>,
    action="reject",
    source="agent",
    from_type="CatalogPart",
    from_id="31112C",
    relationship="classified_as",
    to_type="TaxonomyType",
    to_id="10006",
    reason="Quarter panel, not door panel — description says QTR TRIM",
)

# 2. Invalidate trust on the pattern
cruxible_update_trust_status(instance_id, resolution_id, "invalidated",
    reason="Mixed quarter/door panels in group — need finer keyword split")
```

Refine the provider contract:

```yaml
# Add to keyword_extract_v1 (or create keyword_extract_v2):
rules:
  - name: quarter_panel
    patterns: ["QTR TRIM", "QUARTER TRIM", "QTR PNL"]
    extracts: {part_noun: "trim_panel", qualifier: "quarter"}
  - name: door_panel
    patterns: ["DR PNL", "DOOR PANEL", "INT DR TRIM"]
    not_patterns: ["QTR", "QUARTER"]     # exclude quarter panels
    extracts: {part_noun: "door_panel", qualifier: "interior"}
```

Re-run the extraction with the updated rules. Propose new groups with finer
thesis_facts:

```
cruxible_propose_group(instance_id, "classified_as",
    members=[...formerly misclassified parts...],
    thesis_facts={"part_noun": "trim_panel", "qualifier": "quarter", "target_type_id": "12730"})
```

Each iteration of this loop:
1. Shrinks the ambiguous tail
2. Adds rules to the provider contract
3. Produces more specific thesis signatures
4. Builds trust on verified patterns

## Phase 8: Ongoing trust flywheel

The same flywheel that works within the first run also works across data
refreshes:

```
New catalog refresh arrives → refresh canonical state
Agent runs same providers (same contract) → same extraction
Agent proposes group with same thesis_facts → same signature
Core finds prior trusted resolution → auto_resolved
Agent calls resolve → edges created, no human review needed
```

The flywheel breaks only when trust is invalidated — which sends the pattern
back to review with `critical` priority.

## Signal Source Versioning

When you change provider rules, declare a new provider and signal-source version:

```yaml
providers:
  keyword_extract_v1:
    # ... original rules (kept for audit trail)
  keyword_extract_v2:
    ref: kit://providers/classification.py::keyword_extract_v2
    version: 2.0.0
```

Update the relationship's `proposal_policy.signals` section and workflow `map_signals.signal_source` to reference v2. Old groups still reference v1 in their `signal_sources_used`, and old resolutions preserve the thesis, trust status, and analysis_state produced under the earlier run.

The signature is stable across provider versions when `thesis_facts` stay stable. Trust earned under v1 carries forward to v2 proposals with the same thesis_facts. If v2 changes the extraction logic enough to produce different thesis_facts, that's a new signature with its own trust track — which is correct behavior.

## Anti-patterns

- **Running LLM classification on every part** — Build deterministic rules first. LLM is for the tail, not the bulk.
- **Putting domain extraction logic inside Core primitives** — Core governs signals and executes providers, but the classification logic should live in provider code or external services.
- **Grouping too coarsely** — "All interior parts → PIES Body" is too broad. Group by extracted part noun + qualifier for meaningful trust boundaries.
- **Grouping too finely** — One part per group defeats batch review. Group by the repeatable pattern, not the individual part.
- **Skipping `analysis_state`** — Stash your extraction context, LLM reasoning, and candidate scores. Future agents (or your future self) will need it when revisiting resolutions.
- **Trusting immediately** — Start at `watch`. Promote to `trusted` after spot-checking edges from the resolved group. The trust flywheel earns speed through verified accuracy.
- **Changing contract without versioning** — Modifying `keyword_extract_v1.contract` in place breaks the audit trail. Declare v2 instead.
