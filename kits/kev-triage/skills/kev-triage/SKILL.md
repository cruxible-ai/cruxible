---
name: kev-triage
description: Run the KEV local's daily triage loop, incident intake, remediation verification, waiver intake, and control-effectiveness proposals against a KEV triage instance using governed proposal flows.
---

# KEV Security Triage

Agent skill for operating on a KEV triage world instance. Read this before
taking any action against the graph.

## What this skill does

This skill covers five agent tasks against a KEV triage instance:

1. **Daily triage pass** — refresh the reference layer, run the proposal
   chain, and produce an actionable summary enriched with incident history and
   remediation state.
2. **Incident intake and synthesis** — open or update an `Incident` with the
   user when a post-mortem, investigation, or triage evidence justifies it,
   then fold that incident into the graph as `Incident` + `Finding` entities
   and governed relationships.
3. **Exception / waiver intake** — propose a patch exception when a team has a
   legitimate reason to delay remediation.
4. **Control effectiveness review** — propose that a compensating control
   materially reduces exposure to a specific CVE class.
5. **Remediation verification** — record that an asset-vulnerability pair has
   been remediated or otherwise verified closed.

All five routes share one rule: **the agent proposes, a reviewer resolves.**
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
- `cruxible add-entity` is allowed. Creating `Incident` / `Finding` /
  `Exception` / `CompensatingControl` records is expected agent work.

If a command fails with `PermissionDeniedError: ... disabled in agent mode`,
do not retry or try to bypass. Surface the error to the user and stop.

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
   - prior exploitation history on the product
   - open findings on the asset
   - prior post-mortem context for the CVE
   - explicit remediation state on the asset

   In the stock `kev-triage` kit, the default queries are:
   ```
   cruxible query --query incident_history_for_product --param product_id=<product>
   cruxible query --query open_findings_for_asset --param asset_id=<asset>
   cruxible query --query prior_exploitation_context --param cve_id=<cve>
   cruxible query --query vulnerability_asset_context --param cve_id=<cve>
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
   - **Elevated priority**: exposures on products with prior exploitation
     history, or assets with open findings that match the new CVE class.
   - **Standard priority**: exposures with no prior history.
   - **Overdue**: exposures past `kev_due_date` with no exception on file.
   - **Waived**: exposures covered by an active exception.
   - **Remediated or conflict-state**: remediation has been recorded for the
     asset-vulnerability pair, but current triage still needs explanation
     (for example, remediation looks stale, evidence is weak, or exposure
     appears to have returned).
  - **Incident candidate**: clusters where the combined evidence suggests
    this should be opened or updated as an `Incident` rather than treated as
    routine triage only.

6. If the summary produces incident candidates, pause and work them directly
   with the user:
   - explain why each candidate looks incident-worthy
   - ask whether to open a new `Incident`, update an existing one, or keep it
     as elevated triage only
   - if the user wants an incident, switch into **Task 2** and create/update
     the `Incident` with `status=investigating` unless they provide a stronger
     status

7. Unless you have been explicitly asked to resolve groups in this run, do not
   resolve the groups you just created. Hand the summary to the next reviewer
   step (human, ticket queue, or another agent responsible for resolution).

**Idempotence.** Re-running the same proposal chain rewrites one pending
bucket per signature instead of compounding the queue. Once a signature has
approved history, unchanged tuples suppress cleanly and only new delta tuples
remain reviewable.

## Task 2 — Incident intake and synthesis

**When:** either:

- an incident report, post-mortem, or SIEM investigation references a
  vulnerability and asset(s) that are already tracked, or
- the daily triage pass surfaces incident-worthy candidates
  and the user wants to open or update an incident directly from that triage
  evidence.

Work those directly with the user instead of assuming a fully formed external
report already exists.

**Inputs you need from the user and available evidence:**
- New incident vs existing incident to update
- Incident ID, title, severity, status, occurred_at, resolved_at (if known),
  source system, free-text summary
- The asset(s) involved — match to existing `Asset` entity IDs
- The vulnerability exploited or suspected — match to existing
  `Vulnerability` entity IDs
- The owner accountable — match to existing `Owner` entity IDs
- Zero or more findings (root causes), if known

If the evidence is still provisional, that is fine. Create the `Incident` with
`status=investigating` or another in-progress state and keep the governed edge
theses explicit about uncertainty. Do not overstate a suspected cluster as a
confirmed compromise.

**Steps:**

1. Confirm the incident decision with the user:
   - Should this become a new `Incident`, or update an existing one?
   - What title, severity, and short summary should be recorded?
   - Which assets, vulnerability IDs, and owner are in scope right now?

2. Create or update the `Incident` entity:
   ```
   cruxible add-entity --type Incident --id INC-2025-003 \
     --props '{"incident_id":"INC-2025-003",
               "title":"WebLogic admin console compromise on partner-api-01",
               "severity":"high","status":"resolved",
               "occurred_at":"2026-01-11","resolved_at":"2026-01-12",
               "source":"pagerduty",
               "summary":"Partner routing change widened source access to the WebLogic admin path on partner-api-01; response restored the allowlist, rotated credentials, and rebuilt the middleware image."}'
   ```

3. Propose `incident_owned_by`:
   ```
   cruxible group propose \
     --relationship incident_owned_by \
     --members '[{"from_type":"Incident","from_id":"INC-2025-003",
                   "to_type":"Owner","to_id":"OWNER-3",
                   "relationship_type":"incident_owned_by",
                   "signals":[{"signal_source":"incident_attribution",
                                "signal":"support",
                                "evidence":"Report names Partner Integrations team as responders"}]}]' \
     --thesis "Partner Integrations team (OWNER-3) ran the response for INC-2025-003 per the on-call log" \
     --thesis-facts '{"incident_id":"INC-2025-003","owner_id":"OWNER-3"}' \
     --signal-source incident_attribution
   ```

4. Propose `incident_involved_asset` (one member per asset):
   ```
   cruxible group propose \
     --relationship incident_involved_asset \
     --members '[{"from_type":"Incident","from_id":"INC-2025-003",
                   "to_type":"Asset","to_id":"ASSET-8",
                   "relationship_type":"incident_involved_asset",
                   "properties":{"role":"target"},
                   "signals":[{"signal_source":"incident_attribution","signal":"support",
                                "evidence":"SIEM shows anomalous admin console access from ..."}]}]' \
     --thesis "partner-api-01 (ASSET-8) was the target host for INC-2025-003" \
     --thesis-facts '{"incident_id":"INC-2025-003","asset_id":"ASSET-8"}' \
     --signal-source incident_attribution
   ```

5. Propose `incident_exploited_vulnerability`:
   ```
   cruxible group propose \
     --relationship incident_exploited_vulnerability \
     --members '[{"from_type":"Incident","from_id":"INC-2025-003",
                   "to_type":"Vulnerability","to_id":"CVE-2020-14882",
                   "relationship_type":"incident_exploited_vulnerability",
                   "signals":[{"signal_source":"incident_attribution","signal":"support",
                                "evidence":"Exploit payload matches CVE-2020-14882 signature"}]}]' \
     --thesis "Attacker used CVE-2020-14882 admin console bypass to reach RCE on partner-api-01" \
     --thesis-facts '{"incident_id":"INC-2025-003","cve_id":"CVE-2020-14882"}' \
     --signal-source incident_attribution
   ```

6. Create `Finding` entities for each root cause identified in the
   post-mortem, then propose `finding_from_incident` linking them back:
   ```
   cruxible add-entity --type Finding --id FIND-2025-020 \
     --props '{"finding_id":"FIND-2025-020",
               "title":"Temporary partner route expansion exposed WebLogic admin path to a broader source range",
               "category":"exposure_gap","status":"remediated",
               "remediation_action":"Require security approval and automatic rollback timers for partner allowlist expansions"}'

   cruxible add-entity --type Finding --id FIND-2025-021 \
     --props '{"finding_id":"FIND-2025-021",
               "title":"Credential rotation for middleware administrators was not automated after emergency network changes",
               "category":"process_gap","status":"open",
               "remediation_action":"Automate post-change credential rotation for externally reachable administrative services"}'

   cruxible group propose \
     --relationship finding_from_incident \
     --members '[{"from_type":"Finding","from_id":"FIND-2025-020",
                   "to_type":"Incident","to_id":"INC-2025-003",
                   "relationship_type":"finding_from_incident",
                   "signals":[{"signal_source":"incident_attribution","signal":"support",
                                "evidence":"Post-mortem lists FIND-2025-020 as a root cause for INC-2025-003"}]},
                  {"from_type":"Finding","from_id":"FIND-2025-021",
                   "to_type":"Incident","to_id":"INC-2025-003",
                   "relationship_type":"finding_from_incident",
                   "signals":[{"signal_source":"incident_attribution","signal":"support",
                                "evidence":"Post-mortem lists FIND-2025-021 as a root cause for INC-2025-003"}]}]' \
     --thesis "The INC-2025-003 post-mortem identifies FIND-2025-020 and FIND-2025-021 as root-cause findings" \
     --thesis-facts '{"incident_id":"INC-2025-003","finding_ids":["FIND-2025-020","FIND-2025-021"]}' \
     --signal-source incident_attribution
   ```

**Thesis quality.** Every proposal must include a thesis that names the
evidence — the specific report line, triage clue, SIEM rule, or interview
note. Reviewers use the thesis to decide; a thesis that just restates the
relationship ("this incident involved this asset") is not useful.

**Triage-to-incident is a user-confirmed escalation.** If the daily triage
pass suggests a likely incident, pause, show the user the evidence cluster,
and ask whether to open or update an `Incident`. Do not silently create one
from elevated risk alone.

**One member per proposal group is fine.** The grouping matters when a single
judgment covers multiple edges (e.g., "this incident touched these five
assets"). Otherwise, a group with one member is correct and easier to review.

## Task 3 — Exception / waiver intake

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
   waived, with scoped exception evidence in edge properties:
   ```
   cruxible group propose \
     --relationship asset_patch_exception_for \
     --members '[{"from_type":"Asset","from_id":"ASSET-5",
                   "to_type":"Vulnerability","to_id":"CVE-2024-38475",
                   "relationship_type":"asset_patch_exception_for",
                   "properties":{"exception_id":"EXC-2026-001",
                                 "review_due_at":"2026-05-03",
                                 "scope_basis":"Exception covers ASSET-5 Apache remediation for CVE-2024-38475 only",
                                 "rationale":"Billing freeze approval delays this specific patch",
                                 "evidence_source":"change_ticket",
                                 "evidence_refs":[{"source":"servicenow","source_record_id":"CHG-40123"}]},
                   "signals":[{"signal_source":"policy_review","signal":"support",
                                "evidence":"Approved by CFO per change ticket CHG-40123"}]}]' \
     --thesis "Billing asset ASSET-5 has an approved month-end freeze for CVE-2024-38475; review 2026-05-03" \
     --thesis-facts '{"exception_id":"EXC-2026-001","cve_id":"CVE-2024-38475"}' \
     --signal-source policy_review
   ```

The deterministic `asset_has_exception` edge is loaded from seed data or
added separately by an operator. The *governed* part is the judgment that a
specific CVE is covered by the exception.

## Task 4 — Control effectiveness review

**When:** a compensating control is already tracked (`CompensatingControl`
entity + `asset_has_control` edges from seed), and there is evidence that it
materially blocks a specific CVE class.

**Steps:**

1. Confirm the control exists:
   ```
   cruxible get-entity --type CompensatingControl --id CTRL-1
   ```

2. Propose `control_mitigates_class` for the vulnerability class the control mitigates:
   ```
   cruxible group propose \
     --relationship control_mitigates_class \
     --members '[{"from_type":"CompensatingControl","from_id":"CTRL-1",
                   "to_type":"VulnerabilityClass","to_id":"path_traversal",
                   "relationship_type":"control_mitigates_class",
                   "properties":{"effect":"blocks",
                                 "validation_basis":"WAF rule set 941xx blocks path traversal payloads",
                                 "verified_at":"2025-10-15",
                                 "expires_at":"2026-04-15",
                                 "evidence_refs":[{"source":"waf_test","source_record_id":"CTRL-1-941xx-2025-10-15"}]},
                   "signals":[{"signal_source":"control_effectiveness","signal":"support",
                                "evidence":"WAF rule set 941xx blocks path traversal payloads; tested 2025-10-15"}]}]' \
     --thesis "Edge WAF ruleset 941xx blocks path traversal exploit strings" \
     --thesis-facts '{"control_id":"CTRL-1","class_id":"path_traversal"}' \
     --signal-source control_effectiveness
   ```

## Task 5 — Remediation verification

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
                                 "evidence_source":"scanner",
                                 "evidence_refs":[{"source":"scanner","source_record_id":"scan-2026-04-22-ASSET-8-CVE-2020-14882"}],
                                 "ticket_id":"CHG-40123"},
                   "signals":[{"signal_source":"remediation_verification","signal":"support",
                                "evidence":"Post-patch scan no longer detects WebLogic admin console bypass"}]}]' \
     --thesis "ASSET-8 was patched and scanner verification on 2026-04-22 no longer detects CVE-2020-14882" \
     --thesis-facts '{"asset_id":"ASSET-8","cve_id":"CVE-2020-14882","remediation_type":"patch"}' \
     --signal-source remediation_verification
   ```

3. If this also closes a root cause, update the related `Finding` entity:
   - set `status=remediated`
   - set `remediated_at`
   - set `remediation_action` if useful

4. If all findings for an incident are remediated, ask the user whether the
   `Incident` should also move to `resolved`.

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
| Patch queue for an owner | `query --query owner_patch_queue --param owner_id=<owner>` |
| Asset context for a product, including product mapping and related exposure state | `query --query product_asset_context --param product_id=<product>` |
| Vendor service impact | `query --query vendor_service_impact --param vendor_id=<vendor>` |
| Has this product ever been exploited? | `query --query incident_history_for_product --param product_id=<product>` |
| Vulnerability class context and mapped controls | `query --query vulnerability_class_context --param class_id=<class>` |
| What open findings for this asset? | `query --query open_findings_for_asset --param asset_id=<asset>` |
| Prior post-mortem for this CVE | `query --query prior_exploitation_context --param cve_id=<cve>` |

## When to stop and ask

Stop and ask the user (don't guess) when:

- An incident report names an asset, owner, vulnerability, or product that
  does not resolve to an existing entity ID. Proposing against a wrong ID
  corrupts the graph.
- A daily triage pass suggests an incident candidate, but the likely asset,
  vulnerability, owner, or incident boundary is still ambiguous. Confirm the
  scope with the user before creating or updating an `Incident`.
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
| `control_mitigates_class` | CompensatingControl → VulnerabilityClass | `control_effectiveness` |
| `asset_remediated_vulnerability` | Asset → Vulnerability | `remediation_verification` |
| `incident_owned_by` | Incident → Owner | `incident_attribution` |
| `incident_involved_asset` | Incident → Asset | `incident_attribution` |
| `incident_exploited_vulnerability` | Incident → Vulnerability | `incident_attribution` |
| `finding_from_incident` | Finding → Incident | `incident_attribution` |

`asset_runs_product`, `asset_vulnerability_posture`, and
`asset_remediated_vulnerability` closure proposals are typically produced by
batch workflows. Classifications and control-class mitigation proposals can be
produced by agent-called workflows with explicit input. Incident, finding,
exception, and remediation verification proposals are typically one-off agent
proposals from review material.
