# Kit Authoring And Distribution

A Cruxible kit is a versioned bundle with a `cruxible-kit.yaml` manifest,
an entry config, provider code, optional data, and a bundled
`cruxible.lock.yaml`.

For runnable examples, see [Kit Walkthroughs](kit-walkthroughs.md).

## Operation-Style Relationship Axes

Kits that model work, reviews, investigations, remediation, agent operations,
or project execution should not collapse every relationship into one generic
`related_to` or `blocks` edge. Start with explicit axes so readiness, critical
path, review, and roll-up queries can keep their meanings separate.

Use these defaults when the kit has work-like entities:

| Axis | Relationship Shape | Meaning |
| --- | --- | --- |
| Sequencing | `work_item_depends_on_work_item` | Direction: from depends on to. The target must land, be decided, or stabilize first. |
| Impediment | `risk_blocks_work_item`, `open_question_blocks_work_item` | A durable unresolved threat or uncertainty blocks or materially delays work. |
| Resolution | `work_item_mitigates_risk`, `work_item_answers_open_question`, `decision_answers_open_question` | Work or a decision resolves the impediment without pretending the impediment was sequencing. |
| Composition | `work_item_part_of_work_item` | Child work is part of a larger scope. This is roll-up, not order. |
| Lineage | `work_item_spawned_from_work_item` | A follow-up came out of earlier work. This is provenance, not a prerequisite. |
| Replacement | `work_item_supersedes_work_item`, `decision_supersedes_decision` | The source replaces the target. This is not general lineage. |
| Review gate | `review_request_for_work_item` plus a mutation guard | A work item cannot move to a guarded lifecycle value until the review request is approved. |
| Interpretation history | `StateNote` plus typed note-about relationships | Durable corrections, field notes, implementation notes, and review notes without bloating current entity fields. |

Do not make these hidden Cruxible defaults. Put the relationships in the kit
config with domain-specific names when needed, because the ontology is the
contract agents and reviewers inspect.

Use direct relationships for deterministic placement and roll-up. Use governed
proposal policies for interpretive claims such as dependencies, blockers,
mitigations, answers, supersession, and decision impact. Documents, chats,
review reports, and source sections should be evidence references for those
claims, not modeled entities.

Minimal operation-axis scaffold:

```yaml
enums:
  work_status:
    values: [planned, active, blocked, watching, closed, deferred, superseded]
  work_priority:
    values: [critical, high, medium, low]
  note_kind:
    values: [correction, field_note, rationale_update, implementation_note, review_note]

entity_types:
  WorkItem:
    properties:
      work_item_id: {primary_key: true}
      title: {required: true}
      status: {enum_ref: work_status, required: true}
      priority: {enum_ref: work_priority}
      summary: {}
  Risk:
    properties:
      risk_id: {primary_key: true}
      title: {required: true}
      status: {}
  OpenQuestion:
    properties:
      question_id: {primary_key: true}
      title: {required: true}
      status: {}
  Decision:
    properties:
      decision_id: {primary_key: true}
      title: {required: true}
      status: {}
  ReviewRequest:
    properties:
      review_request_id: {primary_key: true}
      title: {required: true}
      status: {required: true}
      summary: {}
      review_notes: {}
  StateNote:
    properties:
      state_note_id: {primary_key: true}
      note_kind: {enum_ref: note_kind, required: true}
      noted_at: {required: true}
      body: {required: true}

relationships:
  - name: work_item_depends_on_work_item
    description: "Sequencing: from depends on to."
    from: WorkItem
    to: WorkItem
    proposal_policy:
      signals:
        source_evidence: {role: required, always_review_on_unsure: true}
        maintainer_judgment: {role: advisory, always_review_on_unsure: true}

  - name: risk_blocks_work_item
    description: "Impediment: a risk blocks or materially delays work."
    from: Risk
    to: WorkItem
    proposal_policy:
      signals:
        source_evidence: {role: required, always_review_on_unsure: true}
        maintainer_judgment: {role: advisory, always_review_on_unsure: true}

  - name: open_question_blocks_work_item
    description: "Impediment: an unresolved question blocks or delays work."
    from: OpenQuestion
    to: WorkItem
    proposal_policy:
      signals:
        source_evidence: {role: required, always_review_on_unsure: true}
        maintainer_judgment: {role: advisory, always_review_on_unsure: true}

  - name: work_item_part_of_work_item
    description: "Composition and roll-up, not sequencing."
    from: WorkItem
    to: WorkItem

  - name: work_item_spawned_from_work_item
    description: "Lineage and follow-up provenance, not sequencing."
    from: WorkItem
    to: WorkItem

  - name: work_item_supersedes_work_item
    description: "Replacement: source supersedes target."
    from: WorkItem
    to: WorkItem
    proposal_policy:
      signals:
        source_evidence: {role: required, always_review_on_unsure: true}
        maintainer_judgment: {role: advisory, always_review_on_unsure: true}

  - name: review_request_for_work_item
    from: ReviewRequest
    to: WorkItem

  - name: state_note_about_work_item
    from: StateNote
    to: WorkItem
```

Add named queries around the axes rather than broad text search: active work
queue, blocked work with blocker context, work-item change context, roll-up
context, lineage context, pending reviews, and recent state notes. Add quality
checks or mutation guards only where they protect real operating discipline,
such as one composition parent, review requests attached to work, and closing
work only after an approved review.

Minimal manifest:

```yaml
schema_version: cruxible.kit.v1
kit_id: kev-triage
version: 0.2.0
role: overlay
target_state: kev-reference
entry_config: config.yaml
provider_paths:
  - providers
copy_paths:
  - data
  - skills
  - README.md
requires_extras: []
```

Rules:

- `role` is `standalone` or `overlay`.
- `role: overlay` requires `target_state`.
- `role: standalone` must not set `target_state`.
- 0.2 supports one `entry_config` per kit.
- `requires_extras` is metadata only. Cruxible does not install kit
  dependencies automatically.

Provider refs use `kit://`:

```yaml
ref: kit://providers/reference.py::normalize_public_kev_reference
```

`kit://` paths are relative to the materialized kit root. Absolute paths,
`..`, symlinks, and paths outside declared `provider_paths` are rejected.
Python providers run in the current Cruxible Python environment and may import
stdlib, `cruxible_core`, installed Cruxible dependencies or extras, and files
under declared provider paths.

Bundle behavior:

- The bundle digest covers every non-junk regular file in sorted POSIX-relative
  order, including path and bytes.
- Junk such as `__pycache__/`, `*.pyc`, `.DS_Store`, `.ruff_cache/`, and
  `.pytest_cache/` is ignored.
- Symlinks are rejected.
- Bundles are cached under `CRUXIBLE_KIT_CACHE_DIR` or
  `${XDG_CACHE_HOME:-~/.cache}/cruxible/kits`.
- Cache installs are locked and atomic by bundle digest.
- Materialization copies the cached kit into the instance root.
- Kit bundles carry `cruxible.lock.yaml` at the kit root as a portable bundle
  artifact.
- Initialized Cruxible instances execute workflows from
  `.cruxible/cruxible.lock.yaml`; kit-backed initialization imports the bundled
  lock there when it matches the active config, or regenerates the instance-local
  lock when an active runtime config has been composed from the bundle.
- Runtime workflow execution does not fall back to arbitrary config-root locks.
- Consumers should not silently regenerate published bundled locks. Rebuild the
  kit lock before publishing or distributing a changed kit.

Built-in aliases such as `kev-reference` resolve to versioned OCI kit refs in
installed packages, with local source-checkout kits overriding those aliases
during development. Publishing the matching OCI bundles is a 0.2 release
precondition.

Refresh a bundled lock directly from the kit root before publishing:

```bash
cruxible lock --kit-dir path/to/kit
```

The lock pins the kit's own config layer only — its providers and artifacts,
with URIs kept relative to the kit root — so an overlay kit locks without its
`target_state` base present. Base-layer content is pinned by the base kit's
own lock. CI asserts every bundled kit's committed lock matches a fresh regen,
so run this after any config, provider, or seed-data change.

Vocabulary:

- Use **overlay** for a local instance tracking a published upstream state.
- Use **clone** for a point-in-time state copy from a snapshot.
- Use **local** for customer-owned seeded or runtime state.
- Do not use clone for kit distribution. Use pull, cache, materialize, or
  install.
