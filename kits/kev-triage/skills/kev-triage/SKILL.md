---
name: kev-triage
description: Run the KEV local's daily triage loop, remediation verification, waiver intake, and control-effectiveness proposals against a KEV triage instance using governed proposal flows.
---

# KEV Security Triage

Agent skill for operating on a KEV triage world instance. Read this before
taking any action against the graph.

## What this skill does

This skill covers four agent tasks against a KEV triage instance:

1. **Daily triage pass** — refresh the reference layer, run the proposal
   chain, and produce an actionable summary enriched with posture,
   remediation, exception, control, and evidence-reference state.
2. **Exception / waiver intake** — propose a patch exception when a team has a
   legitimate reason to delay remediation.
3. **Control effectiveness review** — propose that a compensating control
   materially reduces exposure to a specific CVE class.
4. **Remediation verification** — record that an asset-vulnerability pair has
   been remediated or otherwise verified closed.

All four routes share one rule: **the agent proposes, a reviewer resolves.**
Nothing gets written to the graph as an accepted edge without going through
`group propose` → reviewer → `group resolve`. The reviewer may be a human or
another agent that has been given responsibility for resolving groups; the
recorded attribution comes from the `group resolve --source ...` value.

## Orient before acting

Every session, before making proposals:

1. `cruxible context show` — confirm which instance you're connected to.
2. `cruxible stats` — note the current entity and edge counts so you can
   detect accidental drift later.
3. `cruxible group list --status pending_review --json` — check what's already
   awaiting review. Don't re-propose something already in the queue.

When you need a specific read surface, use:

```
cruxible query describe --query <name> --json
```

to inspect required params and example IDs for that query only. Do not
enumerate the full query catalog unless you are debugging the kit.

If the reviewer is about to act on proposals you make, run the relevant
context queries for the specific owner or CVE involved so the decision lands
in queue and blast-radius context. Typical examples:

```
cruxible query --query owner_patch_queue --param owner_id=<owner>
cruxible query --query vulnerability_asset_context --param cve_id=<cve_id>
```

## Agent-mode constraints

This instance must run under `CRUXIBLE_AGENT_MODE=1`. In that mode:

- `cruxible add-relationship` is **blocked**. Agents cannot write accepted
  edges directly — only `group propose`.
- `cruxible ingest` is **blocked**. Bulk CSV import is an operator action,
  not an agent action.
- `cruxible add-entity` is allowed for durable operational entities such as
  `Exception` / `CompensatingControl` records when the user asks for them.
- Do not assign numeric confidence properties on governed relationship
  proposals. Relationship confidence is represented by declared tri-state
  signal-source evidence: `support`, `unsure`, or `contradict`. Put the reason
  in signal `evidence`, domain-specific basis fields, member
  `evidence_rationale`, or thesis text/facts instead.
- If a workflow provider exposes an internal numeric match score, treat it
  only as an input that the workflow maps into a tri-state signal. Do not copy
  scores into proposal member properties or accepted graph relationships.

If a command fails with `PermissionDeniedError: ... disabled in agent mode`,
do not retry or try to bypass. Surface the error to the user and stop.

## Evidence boundary

Scanner findings, EDR detections, SIEM alerts, reports, and postmortems are
evidence inputs in this 0.2 kit. Do not create graph entities for those source
records. Preserve them through named artifacts, provider output rows, workflow
traces, tri-state signal evidence, receipts, and proposal member
`evidence_refs` / `evidence_rationale`. Once a governed relationship is
accepted, supporting evidence lives under relationship metadata, not domain
properties.

When a report says a host was affected by a CVE, use that source to support or
challenge `asset_vulnerability_posture`, `asset_remediated_vulnerability`,
`asset_patch_exception_for`, or `vulnerability_classified_as`. Treat
`control_mitigates_class` as curated local state: inspect it as context, and
report a data/config authoring issue if the mapping is missing or wrong.

## Task 1 — Daily triage pass

Runs on a cadence (typically daily). The agent's job is to produce a
human-actionable summary. It may safely refresh the KEV reference layer, but it
does not approve or resolve governed proposals directly.

**Steps:**

1. Refresh the reference layer:
   ```
   cruxible world status
   cruxible world pull-preview
   ```
   Use `world pull-*` for the KEV daily refresh path. KEV reference releases
   are data-safe/additive, so the agent may pull them directly. If this
   instance is not tracking a published upstream KEV reference, stop and fix
   that first instead of rebuilding the reference layer locally.

   Inspect the pull preview before applying. If it reports "Already at latest
   pulled release", continue without applying. If it reports conflicts,
   breaking compatibility, or an unexpected delta, stop and ask. Otherwise
   apply the returned digest:
   ```
   cruxible world pull-apply --apply-digest <digest>
   ```

2. Run the local proposal chain:
   ```
   cruxible propose --workflow propose_asset_products
   cruxible propose --workflow propose_asset_exposure
   cruxible propose --workflow propose_exposure_reconciliation
   ```
   These are the standard workflow names in the stock `kev-triage` kit. If
   this instance uses a documented local variant, run the equivalent chain for
   that variant instead. Each stage produces governed groups that enter the
   review queue.

3. For each new `asset_vulnerability_posture` candidate, query the
   relevant context surfaces for:
   - ownership, service, and product blast-radius context
   - approved remediation and scoped exception state
   - active controls and vulnerability-class coverage
   - explicit remediation state on the asset

   In the stock `kev-triage` kit, the default queries are:
   ```
   cruxible query --query vulnerability_asset_context --param cve_id=<cve>
   cruxible query --query product_asset_context --param product_id=<product>
   cruxible query --query vendor_service_impact --param vendor_id=<vendor>
   ```

4. Before treating any query output as complete or final, check whether the
   same governed surface still has pending work:
   ```
   cruxible group list --status pending_review --json
   ```
   If relevant pending groups exist, tell the user that accepted query results
   may lag reviewable proposals and call out which relationships still need a
   reviewer. Use `group get` / `group status` for the specific bucket when you
   need to confirm whether a pending group explains an empty or incomplete
   query result.

5. Produce a summary that distinguishes:
   - **Elevated priority**: exposures on critical assets or services,
     internet-facing assets, or posture rows with high-priority evidence.
   - **Standard priority**: exposures with no prior history.
   - **Overdue**: exposures past `kev_due_date` with no exception on file.
   - **Waived**: exposures covered by an active exception.
   - **Remediated or conflict-state**: remediation has been recorded for the
     asset-vulnerability pair, but current triage still needs explanation
     (for example, remediation looks stale, evidence is weak, or exposure
     appears to have returned).

6. Unless you have been explicitly asked to resolve groups in this run, do not
   resolve the groups you just created. Hand the summary to the next reviewer
   step (human, ticket queue, or another agent responsible for resolution).

**Idempotence.** Re-running the same proposal chain rewrites one pending
bucket per signature instead of compounding the queue. Once a signature has
approved history, unchanged tuples suppress cleanly and only new delta tuples
remain reviewable.

## Task 2 — Exception / waiver intake

**When:** a team requests a patch exception for a specific CVE on a specific
asset, with an approver, rationale, and review date.

**Steps:**

1. Create or update the `Exception` entity:
   ```
   cruxible add-entity --type Exception --id EXC-2026-001 \
     --props '{"exception_id":"EXC-2026-001",
               "reason":"Billing month-end freeze delays Apache remediation on batch-worker-01",
               "status":"approved","review_due_at":"2026-05-03"}'
   ```

2. Propose `asset_patch_exception_for` linking the asset to the CVE being
   waived, with scoped exception evidence on the proposal member:
   ```
   cruxible group propose \
     --relationship asset_patch_exception_for \
     --members '[{"from_type":"Asset","from_id":"ASSET-5",
                   "to_type":"Vulnerability","to_id":"CVE-2024-38475",
                   "relationship_type":"asset_patch_exception_for",
                   "properties":{"exception_id":"EXC-2026-001",
                                 "review_due_at":"2026-05-03",
                                 "scope_basis":"Exception covers ASSET-5 Apache remediation for CVE-2024-38475 only"},
                   "evidence_rationale":"Billing freeze approval delays this specific patch",
                   "evidence_refs":[{"source":"servicenow","source_record_id":"CHG-40123"}],
                   "signals":[{"signal_source":"policy_review","signal":"support",
                                "evidence":"Approved by CFO per change ticket CHG-40123"}]}]' \
     --thesis "Billing asset ASSET-5 has an approved month-end freeze for CVE-2024-38475; review 2026-05-03" \
     --thesis-facts '{"exception_id":"EXC-2026-001","cve_id":"CVE-2024-38475"}' \
     --signal-source policy_review
   ```

The deterministic `asset_has_exception` edge is loaded from seed data or
added separately by an operator. The *governed* part is the judgment that a
specific CVE is covered by the exception.

## Task 3 — Control effectiveness review

**When:** a compensating control is already tracked (`CompensatingControl`
entity + `asset_has_control` edges from seed), and there is evidence that it
materially blocks a specific CVE class.

**Steps:**

1. Confirm the control exists:
   ```
   cruxible get-entity --type CompensatingControl --id CTRL-1
   ```

2. Inspect curated `control_mitigates_class` mappings for the vulnerability
   class the control is meant to cover:
   ```
   cruxible query --query vulnerability_class_context --param class_id=path_traversal
   cruxible query --query control_coverage_gap --param control_id=CTRL-1
   ```

   Do not propose `control_mitigates_class` as a governed relationship. It is
   deterministic local state loaded by `build_local_state`. If the mapping is
   missing, stale, or too broad, cite the evidence in the review summary and
   ask the operator to correct the local seed/config data.

## Task 4 — Remediation verification

**When:** a team says a patch, upgrade, config change, or decommissioning
action is complete, or scanner/manual validation shows that a specific
asset-vulnerability pair is now closed.

**Steps:**

1. Confirm the remediation claim with the user:
   - Which `Asset` and `Vulnerability` pair is being closed?
   - What remediation type applies (`patch`, `upgrade`, `config_change`,
     `decommission`, `vendor_fix`, etc.)?
   - What evidence supports closure right now?
   - Is there a ticket/change ID to record?

2. Propose `asset_remediated_vulnerability`:
   ```
   cruxible group propose \
     --relationship asset_remediated_vulnerability \
     --members '[{"from_type":"Asset","from_id":"ASSET-8",
                   "to_type":"Vulnerability","to_id":"CVE-2020-14882",
                   "relationship_type":"asset_remediated_vulnerability",
                   "properties":{"remediation_type":"patch",
                                 "verified_at":"2026-04-22",
                                 "verification_basis":"Post-patch scanner verification",
                                 "ticket_id":"CHG-40123"},
                   "evidence_rationale":"Scanner verification no longer detects CVE-2020-14882 on ASSET-8",
                   "evidence_refs":[{"source":"scanner","source_record_id":"scan-2026-04-22-ASSET-8-CVE-2020-14882"}],
                   "signals":[{"signal_source":"remediation_verification","signal":"support",
                                "evidence":"Post-patch scan no longer detects WebLogic admin console bypass"}]}]' \
     --thesis "ASSET-8 was patched and scanner verification on 2026-04-22 no longer detects CVE-2020-14882" \
     --thesis-facts '{"asset_id":"ASSET-8","cve_id":"CVE-2020-14882","remediation_type":"patch"}' \
     --signal-source remediation_verification
   ```

**Important boundary.** Remediation state should be explicit. Do not assume an
exposure disappeared just because a later proposal run did not reproduce it.
Use `asset_remediated_vulnerability` when the user or evidence actually
supports closure.

## Review feedback loop

After a reviewer resolves a group (`cruxible group resolve --group <id>
--action approve|reject --source human|agent --expected-pending-version <n>`),
the system records a
resolution. From there,
reviewers have two different follow-up tools:

- **Resolution trust** — if the reviewer wants to reopen doubt about a
  resolution, use:
  `cruxible group trust --resolution <resolution_id> --status watch|invalidated --reason "..."`
- **Receipt outcomes** — if later operational evidence shows a prior decision
  surface was right or wrong, record an anchored outcome on the relevant
  receipt:
  `cruxible outcome --receipt <receipt_id> --outcome correct|incorrect|partial|unknown --detail '{"reason":"..."}'`

This loop matters to agents too:

- Rejected proposals are signal that the thesis or evidence was insufficient.
  Before re-proposing a rejected relationship, read the resolution rationale
  (`cruxible group get --group <id>`) and the bucket view
  (`cruxible group status --group <id>`) and adjust.
- `watch` or `invalidated` trust on a resolution means the reviewer wants a
  second look. Treat those as "unconfirmed" when summarizing.

## Common read surfaces for context

These are the default query names shipped with the stock `kev-triage` kit. If
your instance has a documented local variant, use the equivalent read surfaces
there.

| When you need... | Default query |
|---|---|
| Asset context for a CVE, including candidate, exposure, remediation, owner, service, exception, and control state | `query --query vulnerability_asset_context --param cve_id=<cve>` |
| Strict action queue for an owner; excludes closed and scoped-exception pairs | `query --query owner_patch_queue --param owner_id=<owner>` |
| Asset context for a product, including product mapping, public affected vulnerabilities, and related posture state | `query --query product_asset_context --param product_id=<product>` |
| Broad vendor service-impact investigation with closure and exception context | `query --query vendor_service_impact --param vendor_id=<vendor>` |
| Broad control coverage investigation; inspect `control_mitigates_class.effect` on the path | `query --query control_coverage_gap --param control_id=<control>` |
| Vulnerability class context and mapped controls | `query --query vulnerability_class_context --param class_id=<class>` |

Use `owner_patch_queue` when the user wants action. Use
`vendor_service_impact` or `control_coverage_gap` when the user wants blast
radius or coverage investigation; those queries intentionally keep remediation,
scoped exception, and control context visible. For controls, treat
`blocks`/`compensates` as stronger mitigation coverage, `reduces` as risk
reduction, and `detects` as monitoring rather than blocking mitigation.

## When to stop and ask

Stop and ask the user (don't guess) when:

- A report, scanner row, SIEM alert, or postmortem names an asset, owner,
  vulnerability, or product that
  does not resolve to an existing entity ID. Proposing against a wrong ID
  corrupts the graph.
- A remediation claim exists, but the asset/CVE mapping or verification
  evidence is ambiguous. Confirm the closure scope with the user before
  proposing `asset_remediated_vulnerability`.
- A proposal needs a relationship or signal source not already used by this
  skill's documented default flows, and the instance does not have a
  documented local variant covering it. Stop and ask rather than guessing a
  new write surface.
- Review material conflicts with graph state (e.g., the report says an
  exception exists but no `Exception` entity is found).
- You hit `PermissionDeniedError` in agent mode. Retrying won't help.
- A workflow `propose` produces no reviewable group when you expected one.
  Either the upstream layer didn't populate, prerequisite approved edges are
  missing, or the proposal policy rejected everything — either way, surface it.
- A query result looks empty, complete, or final, but there are relevant
  `pending_review` groups on the same governed surface. Surface the accepted
  vs pending distinction instead of implying the query sees proposed state.

## Troubleshooting

| Symptom | Check |
|---|---|
| `cruxible group propose` rejects with "signal source not declared" | Use the signal source names documented in this skill for the task you are performing. If the instance still rejects the name, stop and ask rather than probing the schema directly. |
| `cruxible run` fails with "Artifact ... sha256 mismatch" | A seed file was edited without re-pinning. Operator needs to run `cruxible lock --force`. Do not retry as agent. |
| Query returns empty when you expected results | Likely the proposal chain ran but nothing has been approved yet. `group list --status pending_review` shows the backlog and `group status --group <id>` shows the accepted-vs-pending split for a specific bucket. |
| `add-entity` says "entity updated" when you expected "added" | ID collision — the entity already exists. Fetch it (`get-entity`) and decide whether to continue. |
| Thesis is accepted but proposal still blocks | `group propose` enforces signal-source evidence. A proposal with no `signals` array and no `--signal-source` flag will be rejected. |

## Relationship reference (local-governed)

Every governed relationship the agent can propose:

| Relationship | From → To | Required signal source |
|---|---|---|
| `asset_runs_product` | Asset → Product | `software_product_match` |
| `asset_vulnerability_posture` | Asset → Vulnerability | `product_version_evidence` + `exploitability_signal` + `control_effectiveness` |
| `asset_patch_exception_for` | Asset → Vulnerability | `policy_review` |
| `vulnerability_classified_as` | Vulnerability → VulnerabilityClass | `vulnerability_classification` |
| `asset_remediated_vulnerability` | Asset → Vulnerability | `remediation_verification` |

`asset_runs_product`, `asset_vulnerability_posture`, and
`asset_remediated_vulnerability` closure proposals are typically produced by
batch workflows. Classifications can be produced by agent-called workflows with
explicit input. Exception and remediation verification proposals are typically
one-off agent proposals from review material. `control_mitigates_class` is not
listed here because it is deterministic local state, not an agent-governed
proposal relationship.
