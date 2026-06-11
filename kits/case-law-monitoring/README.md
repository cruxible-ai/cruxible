# Case Law Monitoring Demo

Single-layer Cruxible state model for legal monitoring alongside a specialized
law agent. The kit is not trying to replace legal reasoning. It gives the agent
and attorney a governed memory of what has been reviewed: holdings, citation
treatment, matter statutory scope, argument impact, filing obligations, matter
impact, and review obligations.

The canonical layer is intentionally practical: opinions, courts, judges,
statutes, legal issues, clients, firm matters, filings, deadlines, attorneys,
arguments, and case outcomes. The governed layer captures legal judgments that
should compound over time:

`opinion/filing -> holding/treatment/scope -> issue/statute -> argument -> matter/client/deadline -> review item/outcome`

The usage story is:

> A new opinion or filing arrives. A legal agent proposes the legal judgment
> edges it implies, attorney review converts accepted judgments into reusable
> matter-context state, and later outcomes tell the kit which proposal paths
> actually helped the firm.

Everything between `CRUXIBLE:BEGIN` / `CRUXIBLE:END` markers is regenerated
from `config.yaml` by `cruxible config-views`; treat those blocks as code-owned
structural truth. Everything outside those marker blocks is authored explanation
for humans and agents reading the kit.

## Modeling Notes

This first pass intentionally avoids a reference state. CourtListener, PACER, or
firm DMS providers can supply opinions and filings later, but this config is
about the local legal-workflow layer: which authorities matter to this firm,
which clients and matters they affect, which arguments they support or
undermine, what filing obligations they create, and what review work follows.

The important Cruxible distinction is that the agent can read broadly, but the
firm should only rely on reviewed legal-judgment edges downstream. A rejected
impact proposal still teaches the system: it narrows future matter-impact
matching, citation-treatment routing, filing-obligation classification, and
review-item generation.

## Ontology Map

Entity types and relationships, color-coded by layer. Solid blue lines are
deterministic canonical state. Dashed red lines are governed proposal/review
relationships.

<!-- CRUXIBLE:BEGIN ontology -->
```mermaid
flowchart LR
  classDef canonicalEntity fill:#4a90d9,stroke:#2c5f8a,color:#fff
  classDef governedEntity fill:#e67e22,stroke:#a0521c,color:#fff

  entity_Argument["Argument"]
  entity_Attorney["Attorney"]
  entity_CaseOutcome["Case Outcome"]
  entity_Client["Client"]
  entity_Court["Court"]
  entity_Deadline["Deadline"]
  entity_Filing["Filing"]
  entity_Holding["Holding"]
  entity_Judge["Judge"]
  entity_LegalIssue["Legal Issue"]
  entity_Matter["Matter"]
  entity_Opinion["Opinion"]
  entity_ReviewItem["Review Item"]
  entity_Statute["Statute"]
  class entity_Argument,entity_Attorney,entity_CaseOutcome,entity_Client,entity_Court,entity_Deadline,entity_Filing,entity_Judge,entity_LegalIssue,entity_Matter,entity_Opinion,entity_Statute canonicalEntity
  class entity_Holding,entity_ReviewItem governedEntity

  %% Deterministic canonical relationships
  entity_Argument -- "Argument Cites Opinion" --> entity_Opinion
  entity_Argument -- "Argument In Matter" --> entity_Matter
  entity_Argument -- "Argument Raises Issue" --> entity_LegalIssue
  entity_Filing -- "Filing In Matter" --> entity_Matter
  entity_Matter -- "Matter Assigned To Attorney" --> entity_Attorney
  entity_Matter -- "Matter For Client" --> entity_Client
  entity_Matter -- "Matter Has Deadline" --> entity_Deadline
  entity_Matter -- "Matter In Jurisdiction" --> entity_Court
  entity_Opinion -- "Opinion Cites Opinion" --> entity_Opinion
  entity_Opinion -- "Opinion Decided By Judge" --> entity_Judge
  entity_Opinion -- "Opinion From Court" --> entity_Court
  entity_CaseOutcome -- "Outcome Of Matter" --> entity_Matter
  entity_CaseOutcome -- "Outcome Resolved Argument" --> entity_Argument
  entity_Statute -- "Statute Governs Issue" --> entity_LegalIssue

  %% Governed proposal/review relationships
  entity_Filing -. "Filing Requires Response" .-> entity_Matter
  entity_Holding -. "Holding Addresses Issue" .-> entity_LegalIssue
  entity_Holding -. "Holding Interprets Statute" .-> entity_Statute
  entity_Holding -. "Holding Supports Argument" .-> entity_Argument
  entity_Holding -. "Holding Undermines Argument" .-> entity_Argument
  entity_Matter -. "Matter Turns On Statute" .-> entity_Statute
  entity_Opinion -. "Opinion Affects Matter" .-> entity_Matter
  entity_Opinion -. "Opinion Creates Review Item" .-> entity_ReviewItem
  entity_Opinion -. "Opinion Has Holding" .-> entity_Holding
  entity_Opinion -. "Opinion Treats Opinion" .-> entity_Opinion
  entity_ReviewItem -. "Review Item For Matter" .-> entity_Matter
  linkStyle 0,1,2,3,4,5,6,7,8,9,10,11,12,13 stroke:#2c5f8a,stroke-width:2px
  linkStyle 14,15,16,17,18,19,20,21,22,23,24 stroke:#e74c3c,stroke-width:2px
```
<!-- CRUXIBLE:END ontology -->

**Legend:** Blue = canonical/deterministic state | Orange = governed-only
judgment object | Solid blue lines = deterministic | Dashed red lines =
governed proposal/review.

## Workflow Summary

The generated pipeline gives the stage order. The generated stage blocks
underneath show which context each procedure reads, which relationship it
proposes, and which provider source implements the behavior.

<!-- CRUXIBLE:BEGIN workflow-pipeline -->
```mermaid
flowchart LR
  classDef canonicalWorkflow fill:#4a90d9,stroke:#2c5f8a,color:#fff
  classDef governedWorkflow fill:#e67e22,stroke:#a0521c,color:#fff

  workflow_pipeline_propose_filing_response["1. Filing Requires Response<br/>Governed proposal"]
  workflow_pipeline_propose_holdings_from_opinion["2. Opinion Has Holding<br/>Governed proposal"]
  workflow_pipeline_propose_matter_statutory_scope["3. Matter Turns On Statute<br/>Governed proposal"]
  workflow_pipeline_propose_opinion_treatment["4. Opinion Treats Opinion<br/>Governed proposal"]
  workflow_pipeline_propose_statute_interpretations["5. Holding Interprets Statute<br/>Governed proposal"]
  workflow_pipeline_propose_holding_issue_links["6. Holding Addresses Issue<br/>Governed proposal"]
  workflow_pipeline_propose_argument_risk["7. Holding Undermines Argument<br/>Governed proposal"]
  workflow_pipeline_propose_argument_support["8. Holding Supports Argument<br/>Governed proposal"]
  workflow_pipeline_propose_matter_impact["9. Opinion Affects Matter<br/>Governed proposal"]
  workflow_pipeline_propose_review_items["10. Opinion Creates Review Item<br/>Governed proposal"]
  workflow_pipeline_propose_review_item_matter_links["11. Review Item For Matter<br/>Governed proposal"]
  workflow_pipeline_propose_filing_response --> workflow_pipeline_propose_holdings_from_opinion
  workflow_pipeline_propose_holdings_from_opinion --> workflow_pipeline_propose_matter_statutory_scope
  workflow_pipeline_propose_matter_statutory_scope --> workflow_pipeline_propose_opinion_treatment
  workflow_pipeline_propose_opinion_treatment --> workflow_pipeline_propose_statute_interpretations
  workflow_pipeline_propose_statute_interpretations --> workflow_pipeline_propose_holding_issue_links
  workflow_pipeline_propose_holding_issue_links --> workflow_pipeline_propose_argument_risk
  workflow_pipeline_propose_argument_risk --> workflow_pipeline_propose_argument_support
  workflow_pipeline_propose_argument_support --> workflow_pipeline_propose_matter_impact
  workflow_pipeline_propose_matter_impact --> workflow_pipeline_propose_review_items
  workflow_pipeline_propose_review_items --> workflow_pipeline_propose_review_item_matter_links
  class workflow_pipeline_propose_filing_response,workflow_pipeline_propose_holdings_from_opinion,workflow_pipeline_propose_matter_statutory_scope,workflow_pipeline_propose_opinion_treatment,workflow_pipeline_propose_statute_interpretations,workflow_pipeline_propose_holding_issue_links,workflow_pipeline_propose_argument_risk,workflow_pipeline_propose_argument_support,workflow_pipeline_propose_matter_impact,workflow_pipeline_propose_review_items,workflow_pipeline_propose_review_item_matter_links governedWorkflow
```
<!-- CRUXIBLE:END workflow-pipeline -->

<!-- CRUXIBLE:BEGIN workflow-summary -->
### 1. Propose Filing Response

**Role:** Governed proposal

**Input context**
- Entity context: Deadline, Filing, Matter

**Result**
- Proposed relationships: Filing Requires Response

**Provider source**
- Assess Filing Response Obligations (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::assess_filing_response_obligations`; non-deterministic

### 2. Propose Holdings From Opinion

**Role:** Governed proposal

**Input context**
- Entity context: Opinion

**Result**
- Proposed relationships: Opinion Has Holding

**Provider source**
- Extract Holdings From Opinions (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::extract_holdings_from_opinions`; non-deterministic

### 3. Propose Matter Statutory Scope

**Role:** Governed proposal

**Input context**
- Entity context: Matter, Statute

**Result**
- Proposed relationships: Matter Turns On Statute

**Provider source**
- Scope Matters To Statutes (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::scope_matters_to_statutes`; non-deterministic

### 4. Propose Opinion Treatment

**Role:** Governed proposal

**Input context**
- Entity context: Opinion
- Relationship context: Opinion Cites Opinion

**Result**
- Proposed relationships: Opinion Treats Opinion

**Provider source**
- Classify Opinion Treatment (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::classify_opinion_treatment`; non-deterministic

### 5. Propose Statute Interpretations

**Role:** Governed proposal

**Input context**
- Entity context: Holding, Opinion, Statute
- Relationship context: Opinion Has Holding

**Result**
- Proposed relationships: Holding Interprets Statute

**Provider source**
- Link Holdings To Statutes (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::link_holdings_to_statutes`; non-deterministic

### 6. Propose Holding Issue Links

**Role:** Governed proposal

**Input context**
- Entity context: Holding, Legal Issue
- Relationship context: Holding Interprets Statute

**Result**
- Proposed relationships: Holding Addresses Issue

**Provider source**
- Map Holdings To Issues (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::map_holdings_to_issues`; non-deterministic

### 7. Propose Argument Risk

**Role:** Governed proposal

**Input context**
- Entity context: Argument, Holding
- Relationship context: Argument Raises Issue, Holding Addresses Issue

**Result**
- Proposed relationships: Holding Undermines Argument

**Provider source**
- Assess Argument Impact (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::assess_argument_impact`; non-deterministic

### 8. Propose Argument Support

**Role:** Governed proposal

**Input context**
- Entity context: Argument, Holding
- Relationship context: Argument Raises Issue, Holding Addresses Issue

**Result**
- Proposed relationships: Holding Supports Argument

**Provider source**
- Assess Argument Impact (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::assess_argument_impact`; non-deterministic

### 9. Propose Matter Impact

**Role:** Governed proposal

**Input context**
- Entity context: Matter, Opinion
- Relationship context: Holding Supports Argument, Holding Undermines Argument, Matter Turns On Statute, Opinion Treats Opinion

**Result**
- Proposed relationships: Opinion Affects Matter

**Provider source**
- Assess Matter Impact (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::assess_matter_impact`; non-deterministic

### 10. Propose Review Items

**Role:** Governed proposal

**Input context**
- Entity context: Matter, Opinion
- Relationship context: Opinion Affects Matter, Opinion Treats Opinion

**Result**
- Proposed relationships: Opinion Creates Review Item

**Provider source**
- Route Review Items (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::route_review_items`; non-deterministic

### 11. Propose Review Item Matter Links

**Role:** Governed proposal

**Input context**
- Entity context: Matter, Opinion
- Relationship context: Opinion Affects Matter, Opinion Creates Review Item, Opinion Treats Opinion

**Result**
- Proposed relationships: Review Item For Matter

**Provider source**
- Route Review Items (Python Function, v0.1.0); source: `kit://providers/case_law_monitoring.py::route_review_items`; non-deterministic
<!-- CRUXIBLE:END workflow-summary -->

## Governed Relationships

This table is generated from proposal policy, decision policy, feedback, and outcome
profile config. It shows the reviewed legal-judgment layer the agent can use as
durable context.

<!-- CRUXIBLE:BEGIN governance-table -->
| Relationship | Scope | Creation Path | Signals | Auto-resolve Gate | Review Policy | Feedback | Outcomes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Filing Requires Response | Filing -> Matter | Workflow: Propose Filing Response | Attorney Review, Docket Matter Match, Filing Obligation Assessor | All Support; prior trust: Trusted Only | Require Review: Filing Obligations Require Review | 3 reason codes | Filing Response Resolution |
| Holding Addresses Issue | Holding -> Legal Issue | Workflow: Propose Holding Issue Links | Attorney Review, Issue Mapper | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 2 reason codes | - |
| Holding Interprets Statute | Holding -> Statute | Workflow: Propose Statute Interpretations | Attorney Review, Statute Interpretation Extractor | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 3 reason codes | - |
| Holding Supports Argument | Holding -> Argument | Workflow: Propose Argument Support | Argument Impact Assessor, Attorney Review | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 2 reason codes | - |
| Holding Undermines Argument | Holding -> Argument | Workflow: Propose Argument Risk | Argument Impact Assessor, Attorney Review | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 3 reason codes | - |
| Matter Turns On Statute | Matter -> Statute | Workflow: Propose Matter Statutory Scope | Attorney Review, Matter Statute Match | All Support; prior trust: Trusted Only | Require Review: Matter Scope Requires Review | 3 reason codes | Matter Scope Resolution |
| Opinion Affects Matter | Opinion -> Matter | Workflow: Propose Matter Impact | Attorney Review, Jurisdiction Overlap, Matter Impact Assessor | All Support; prior trust: Trusted Only | Require Review: Matter Impacts Require Review | 3 reason codes | Matter Impact Resolution |
| Opinion Creates Review Item | Opinion -> Review Item | Workflow: Propose Review Items | Attorney Review, Review Item Router | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 2 reason codes | - |
| Opinion Has Holding | Opinion -> Holding | Workflow: Propose Holdings From Opinion | Attorney Review, Holding Extractor | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 3 reason codes | - |
| Opinion Treats Opinion | Opinion -> Opinion | Workflow: Propose Opinion Treatment | Attorney Review, Citation Treatment Classifier | All Support; prior trust: Trusted Only | Require Review: Negative Treatment Requires Review | 3 reason codes | Treatment Resolution |
| Review Item For Matter | Review Item -> Matter | Workflow: Propose Review Item Matter Links | Attorney Review, Review Item Router | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 2 reason codes | - |
<!-- CRUXIBLE:END governance-table -->

### Signal Policy Notes

This catalog is generated from relationship-local signal policy and the
governed relationships that consume each signal source.

<!-- CRUXIBLE:BEGIN signal-policy-catalog -->
| Signal Source | Role | Review Unsure | Used By | Notes |
| --- | --- | --- | --- | --- |
| `argument_impact_assessor` | required | yes | Holding Supports Argument, Holding Undermines Argument | - |
| `attorney_review` | advisory | yes | Filing Requires Response, Holding Addresses Issue, Holding Interprets Statute, Holding Supports Argument, Holding Undermines Argument, Matter Turns On Statute, Opinion Affects Matter, Opinion Creates Review Item, Opinion Has Holding, Opinion Treats Opinion, Review Item For Matter | - |
| `citation_treatment_classifier` | required | yes | Opinion Treats Opinion | - |
| `docket_matter_match` | required | yes | Filing Requires Response | - |
| `filing_obligation_assessor` | required | yes | Filing Requires Response | - |
| `holding_extractor` | required | yes | Opinion Has Holding | - |
| `issue_mapper` | required | yes | Holding Addresses Issue | - |
| `jurisdiction_overlap` | advisory | yes | Opinion Affects Matter | - |
| `matter_impact_assessor` | required | yes | Opinion Affects Matter | - |
| `matter_statute_match` | required | yes | Matter Turns On Statute | - |
| `review_item_router` | required | yes | Opinion Creates Review Item, Review Item For Matter | - |
| `statute_interpretation_extractor` | required | yes | Holding Interprets Statute | - |
<!-- CRUXIBLE:END signal-policy-catalog -->

## Query Map

Named queries are the main context surfaces for the legal agent. The map shows
entry/return affordances; the catalog gives the traversal and intended use.

<!-- CRUXIBLE:BEGIN query-map -->
```mermaid
flowchart LR
  classDef queryEntity fill:#ecfdf5,stroke:#047857,color:#064e3b

  query_entity_Argument["Argument"]
  query_entity_Attorney["Attorney"]
  query_entity_CaseOutcome["Case Outcome"]
  query_entity_Client["Client"]
  query_entity_Deadline["Deadline"]
  query_entity_Filing["Filing"]
  query_entity_LegalIssue["Legal Issue"]
  query_entity_Matter["Matter"]
  query_entity_Opinion["Opinion"]
  query_entity_ReviewItem["Review Item"]
  query_entity_Statute["Statute"]
  class query_entity_Argument,query_entity_Attorney,query_entity_CaseOutcome,query_entity_Client,query_entity_Deadline,query_entity_Filing,query_entity_LegalIssue,query_entity_Matter,query_entity_Opinion,query_entity_ReviewItem,query_entity_Statute queryEntity
  query_entity_Argument --> query_entity_CaseOutcome
  query_entity_Attorney --> query_entity_Deadline
  query_entity_Client --> query_entity_Opinion
  query_entity_LegalIssue --> query_entity_Opinion
  query_entity_LegalIssue --> query_entity_Statute
  query_entity_Matter --> query_entity_CaseOutcome
  query_entity_Matter --> query_entity_Filing
  query_entity_Matter --> query_entity_Opinion
  query_entity_Matter --> query_entity_ReviewItem
  query_entity_Matter --> query_entity_Statute
  query_entity_Opinion --> query_entity_Matter
  query_entity_Opinion --> query_entity_ReviewItem
```
<!-- CRUXIBLE:END query-map -->

<!-- CRUXIBLE:BEGIN query-catalog -->
### Argument

| Query | Returns | Traversal | Purpose |
| --- | --- | --- | --- |
| Argument Track Record | Case Outcome | Outcome Resolved Argument (Incoming) | Starting from an argument, find case outcomes where that argument was resolved or evaluated. |

### Attorney

| Query | Returns | Traversal | Purpose |
| --- | --- | --- | --- |
| Deadline Watch | Deadline | Matter Assigned To Attorney (Incoming) -> Matter Has Deadline (Outgoing) | Starting from an attorney, find deadlines across assigned matters. |

### Client

| Query | Returns | Traversal | Purpose |
| --- | --- | --- | --- |
| Client Impact Watch | Opinion | Matter For Client (Incoming) -> Opinion Affects Matter (Incoming) | Starting from a client, find opinions judged to affect that client's matters. |

### Legal Issue

| Query | Returns | Traversal | Purpose |
| --- | --- | --- | --- |
| Precedent Chain For Issue | Opinion | Holding Addresses Issue (Incoming) -> Opinion Has Holding (Incoming) | Starting from a legal issue, find opinions with reviewed holdings that address the issue. |
| Statutory Interpretations For Issue | Statute | Holding Addresses Issue (Incoming) -> Holding Interprets Statute (Outgoing) | Starting from a legal issue, find statutes interpreted by holdings addressing that issue. |

### Matter

| Query | Returns | Traversal | Purpose |
| --- | --- | --- | --- |
| Adverse Authority For Matter | Opinion | Argument In Matter (Incoming) -> Holding Undermines Argument (Incoming) -> Opinion Has Holding (Incoming) | Starting from a matter, find opinions whose holdings undermine arguments in that matter. |
| Case Outcomes For Matter | Case Outcome | Outcome Of Matter (Incoming) | Starting from a matter, find case outcomes recorded for that matter. |
| Filing Response Obligations For Matter | Filing | Filing Requires Response (Incoming) | Starting from a matter, find filings judged to require a response or deadline. |
| Matter Statutory Scope | Statute | Matter Turns On Statute (Outgoing) | Starting from a matter, find statutes and doctrines accepted as within the matter's legal scope. |
| Negative Treatment For Cited Authorities | Opinion | Argument In Matter (Incoming) -> Argument Cites Opinion (Outgoing) -> Opinion Treats Opinion (Incoming) | Starting from a matter, find new opinions that negatively treat authorities cited by matter arguments. |
| Review Items For Matter | Review Item | Review Item For Matter (Incoming) | Starting from a matter, find open or historical review items created by monitored opinions. |
| Supporting Authority For Matter | Opinion | Argument In Matter (Incoming) -> Holding Supports Argument (Incoming) -> Opinion Has Holding (Incoming) | Starting from a matter, find opinions whose holdings support arguments in that matter. |

### Opinion

| Query | Returns | Traversal | Purpose |
| --- | --- | --- | --- |
| Matters Impacted By Opinion | Matter | Opinion Affects Matter (Outgoing) | Starting from an opinion, find matters judged affected by that opinion. |
| Opinion Review Items | Review Item | Opinion Creates Review Item (Outgoing) | Starting from an opinion, find review items created by that opinion. |
<!-- CRUXIBLE:END query-catalog -->

## Schema Reference

This README keeps schema detail at the diagram and table level so the kit
remains usable as a drafting surface. The config remains the source of truth
for full entity, relationship, and contract properties. For a generated
Markdown schema catalog, run:

```bash
uv run cruxible config-views --config kits/case-law-monitoring/config.yaml --runtime --view schema-catalog
```

When the kit is loaded into a local instance, generate navigable reference
pages under `wiki/reference/` with:

```bash
uv run cruxible render-wiki --output wiki --scope local
```


## Compounding Knowledge Procedure

1. Register matter portfolio anchors: clients, matters, attorneys, arguments,
   deadlines, issues, cited authorities, courts, and statutes.
2. Register new monitored opinions and filings from alerts, docket feeds,
   research, or attorney review.
3. Propose matter statutory scope, holdings, citation treatment, statute
   interpretation, and issue mappings.
4. Propose support/risk links between reviewed holdings and matter arguments.
5. Propose matter-impact, filing-response, and review-item edges for attorney
   review.
6. Use named queries to supply the legal agent with reviewed adverse authority,
   supporting authority, impacted clients/matters, deadline obligations, and
   open review obligations.
7. Record case outcomes and argument results as canonical matter history.
8. Feed review feedback and later outcomes back into provider fixes, decision
   policies, constraints, and trust calibration.

## Usage Stories

- **New opinion triage:** determine whether a monitored opinion affects active
  matters or only belongs in background research.
- **Client impact watch:** start from a client and surface opinions judged to
  affect that client's matters.
- **Filing obligation review:** determine whether a new filing creates a
  response, deadline, or client-update obligation.
- **Adverse authority check:** surface opinions that undermine arguments in a
  matter before a filing or client update.
- **Cited-authority health:** identify new negative treatment of opinions a
  matter already relies on.
- **Argument track record:** use resolved case outcomes to show how similar
  arguments have performed across matters.
- **Matter review queue:** create attorney-review items when authority changes
  the risk posture of a matter.
- **Practice memory:** preserve reviewed impact decisions so future opinions are
  compared against firm-specific matter history, not generic legal memory.

## Rules And Learning Loops

These generated sections own the operational facts: constraints, quality
checks, feedback vocabularies, and outcome vocabularies. Authored prose should
explain how to use them, not restate the config.

<!-- CRUXIBLE:BEGIN quality-rules -->
### Constraints

No configured constraints.

### Quality Checks

| Name | Kind | Target | Severity | Rule |
| --- | --- | --- | --- | --- |
| `case_outcomes_have_matter` | Cardinality | Case Outcome -> Outcome Of Matter (out) | Error | min `1`, max `1` |
| `deadlines_have_matter` | Cardinality | Deadline -> Matter Has Deadline (in) | Warning | min `1` |
| `filing_responses_have_response_type` | Property | Filing Requires Response.response_type | Error | Required |
| `matter_impacts_have_level` | Property | Opinion Affects Matter.impact_level | Error | Required |
| `matters_have_one_client` | Cardinality | Matter -> Matter For Client (out) | Warning | min `1`, max `1` |
| `matters_have_responsible_attorney` | Cardinality | Matter -> Matter Assigned To Attorney (out) | Warning | min `1` |
| `opinion_treatments_have_treatment` | Property | Opinion Treats Opinion.treatment | Error | Required |
| `opinions_have_court` | Cardinality | Opinion -> Opinion From Court (out) | Warning | min `1` |
<!-- CRUXIBLE:END quality-rules -->

<!-- CRUXIBLE:BEGIN learning-loops -->
### Feedback Profiles (Loop 1)

#### `filing_requires_response`
- Version: `1`
- Reason codes:
  - `deadline_extraction_error` (`provider_fix`): The filing response deadline was incorrectly extracted or normalized.
  - `no_response_required` (`constraint`): This filing does not create a response obligation for the matter.
  - `wrong_matter_routing` (`quality_check`): The filing was routed to the wrong tracked matter.
- Scope keys:
  - `filing`: `FROM.filing_id`
  - `matter`: `TO.matter_id`
  - `response_type`: `EDGE.response_type`

#### `holding_addresses_issue`
- Version: `1`
- Reason codes:
  - `issue_too_broad` (`decision_policy`): Holding is related but does not directly address the tracked issue.
  - `wrong_practice_area` (`provider_fix`): Holding belongs to a different practice-area framing.
- Scope keys:
  - `holding`: `FROM.holding_id`
  - `issue`: `TO.issue_id`
  - `issue_fit`: `EDGE.issue_fit`

#### `holding_interprets_statute`
- Version: `1`
- Reason codes:
  - `scope_mismatch` (`constraint`): Interpretation applies only under a narrower procedural or factual scope.
  - `wrong_interpretation_type` (`provider_fix`): Interpretation type is wrong.
  - `wrong_statute` (`provider_fix`): Holding does not interpret this statute or rule.
- Scope keys:
  - `holding`: `FROM.holding_id`
  - `interpretation_type`: `EDGE.interpretation_type`
  - `statute`: `TO.statute_id`

#### `holding_supports_argument`
- Version: `1`
- Reason codes:
  - `factually_distinguishable` (`constraint`): Holding depends on facts too different from the matter argument.
  - `tangential_support` (`decision_policy`): Holding is related but does not materially support this argument.
- Scope keys:
  - `argument`: `TO.argument_id`
  - `holding`: `FROM.holding_id`
  - `support_strength`: `EDGE.support_strength`

#### `holding_undermines_argument`
- Version: `1`
- Reason codes:
  - `distinguishable_authority` (`constraint`): Holding is distinguishable and should not be treated as adverse.
  - `no_material_risk` (`decision_policy`): Holding does not materially undermine the argument.
  - `wrong_direction` (`provider_fix`): Holding supports or is neutral to the argument rather than undermining it.
- Scope keys:
  - `argument`: `TO.argument_id`
  - `holding`: `FROM.holding_id`
  - `risk_type`: `EDGE.risk_type`

#### `matter_turns_on_statute`
- Version: `1`
- Reason codes:
  - `duplicate_scope` (`constraint`): This scope link duplicates an already accepted matter-scope entry.
  - `issue_not_in_matter` (`quality_check`): The issue used to justify this statutory scope is not part of the matter.
  - `wrong_statute_link` (`provider_fix`): The statute, rule, or doctrine is not relevant to this matter's legal issues.
- Scope keys:
  - `matter`: `FROM.matter_id`
  - `scope_basis`: `EDGE.scope_basis`
  - `statute`: `TO.statute_id`

#### `opinion_affects_matter`
- Version: `1`
- Reason codes:
  - `matter_posture_mismatch` (`constraint`): Opinion affects a posture or claim type not present in this matter.
  - `no_material_impact` (`decision_policy`): Opinion does not materially affect this matter.
  - `wrong_impact_level` (`decision_policy`): Impact level is overstated or understated.
- Scope keys:
  - `impact_level`: `EDGE.impact_level`
  - `impact_type`: `EDGE.impact_type`
  - `matter`: `TO.matter_id`
  - `opinion`: `FROM.opinion_id`

#### `opinion_creates_review_item`
- Version: `1`
- Reason codes:
  - `unnecessary_review` (`decision_policy`): Opinion does not create a review obligation.
  - `wrong_obligation_type` (`provider_fix`): Review obligation type is incorrect.
- Scope keys:
  - `obligation_type`: `EDGE.obligation_type`
  - `opinion`: `FROM.opinion_id`
  - `review_item`: `TO.review_item_id`

#### `opinion_has_holding`
- Version: `1`
- Reason codes:
  - `duplicate_holding` (`decision_policy`): Holding duplicates an existing accepted holding.
  - `missed_scope_limitation` (`constraint`): Holding omits an important limitation or procedural posture.
  - `overbroad_holding` (`provider_fix`): Holding is broader than the opinion supports.
- Scope keys:
  - `holding`: `TO.holding_id`
  - `holding_type`: `TO.holding_type`
  - `opinion`: `FROM.opinion_id`

#### `opinion_treats_opinion`
- Version: `1`
- Reason codes:
  - `citation_context_mismatch` (`constraint`): Treatment is accurate only in a different doctrinal or procedural context.
  - `not_meaningful_treatment` (`provider_fix`): Citation does not meaningfully treat the cited authority.
  - `wrong_treatment_type` (`provider_fix`): Treatment type is incorrect.
- Scope keys:
  - `cited_opinion`: `TO.opinion_id`
  - `source_opinion`: `FROM.opinion_id`
  - `treatment`: `EDGE.treatment`

#### `review_item_for_matter`
- Version: `1`
- Reason codes:
  - `duplicate_review_item` (`decision_policy`): Review item duplicates existing matter work.
  - `wrong_matter` (`provider_fix`): Review item was routed to the wrong matter.
- Scope keys:
  - `assignment_basis`: `EDGE.assignment_basis`
  - `matter`: `TO.matter_id`
  - `review_item`: `FROM.review_item_id`

### Outcome Profiles (Loop 2)

#### Resolution-Anchored

##### `filing_response_resolution`
- Version: `1`
- Target: Relationship `filing_requires_response`
- Outcome codes:
  - `date_parse_error` (`provider_fix`): The filing date or response deadline was parsed incorrectly.
  - `false_deadline` (`trust_adjustment`): The accepted response obligation or deadline was incorrect.
  - `missed_deadline` (`require_review`): A real response obligation or deadline was missed.
  - `response_obligation_confirmed` (`unknown`): Later matter activity confirmed that the response obligation applied.
- Scope keys:
  - `relationship_type`: `RESOLUTION.relationship_type`

##### `matter_impact_resolution`
- Version: `1`
- Target: Relationship `opinion_affects_matter`
- Outcome codes:
  - `missed_matter` (`require_review`): A matter was later found to be affected but was missed by the workflow.
  - `no_actual_impact` (`trust_adjustment`): Later review found the accepted impact did not change the matter.
  - `strategy_updated` (`unknown`): Matter strategy, filing plan, or client advice changed because of this opinion.
- Scope keys:
  - `relationship_type`: `RESOLUTION.relationship_type`

##### `matter_scope_resolution`
- Version: `1`
- Target: Relationship `matter_turns_on_statute`
- Outcome codes:
  - `missed_core_statute` (`require_review`): A core statute, rule, or doctrine was missed during matter scoping.
  - `scope_confirmed` (`unknown`): Later review confirmed the matter-scope link.
  - `scope_too_broad` (`trust_adjustment`): The accepted matter-scope link was broader than the matter actually required.
- Scope keys:
  - `relationship_type`: `RESOLUTION.relationship_type`

##### `treatment_resolution`
- Version: `1`
- Target: Relationship `opinion_treats_opinion`
- Outcome codes:
  - `negative_treatment_missed` (`workflow_fix`): Negative treatment existed but was missed by the workflow.
  - `treatment_confirmed` (`unknown`): Attorney review or later citation history confirmed the treatment.
  - `treatment_overstated` (`trust_adjustment`): Later review showed the treatment was overstated.
- Scope keys:
  - `relationship_type`: `RESOLUTION.relationship_type`

#### Receipt-Anchored

##### `adverse_authority_query`
- Version: `1`
- Target: Query `adverse_authority_for_matter`
- Outcome codes:
  - `false_positive_authority` (`graph_fix`): Query returned authority later judged not adverse.
  - `missed_adverse_authority` (`graph_fix`): Query omitted authority later judged adverse to the matter.
- Scope keys:
  - `query`: `SURFACE.name`

##### `argument_track_record_query`
- Version: `1`
- Target: Query `argument_track_record`
- Outcome codes:
  - `misleading_track_record` (`graph_fix`): Query returned outcome context that overstated the argument's usefulness.
  - `missing_outcome_context` (`graph_fix`): Query omitted a relevant outcome for the argument pattern.
- Scope keys:
  - `query`: `SURFACE.name`

##### `client_impact_query`
- Version: `1`
- Target: Query `client_impact_watch`
- Outcome codes:
  - `false_client_impact` (`graph_fix`): Query returned an opinion later judged not material to the client's matters.
  - `missed_client_impact` (`graph_fix`): Query omitted an opinion later judged material to the client's matters.
- Scope keys:
  - `query`: `SURFACE.name`

##### `deadline_watch_query`
- Version: `1`
- Target: Query `deadline_watch`
- Outcome codes:
  - `missed_deadline` (`graph_fix`): Query omitted a real pending matter deadline.
  - `stale_deadline` (`graph_fix`): Query returned a deadline that should have been closed or superseded.
- Scope keys:
  - `query`: `SURFACE.name`

##### `filing_response_query`
- Version: `1`
- Target: Query `filing_response_obligations_for_matter`
- Outcome codes:
  - `missed_response_obligation` (`graph_fix`): Query omitted a filing that required response.
  - `stale_response_obligation` (`graph_fix`): Query returned a filing obligation that no longer applied.
- Scope keys:
  - `query`: `SURFACE.name`

##### `review_item_query`
- Version: `1`
- Target: Query `review_items_for_matter`
- Outcome codes:
  - `missed_review_item` (`graph_fix`): Query omitted a review item later needed for the matter.
  - `stale_review_item` (`graph_fix`): Query returned a review item that should have been closed or superseded.
- Scope keys:
  - `query`: `SURFACE.name`
<!-- CRUXIBLE:END learning-loops -->

## Open Design Questions

- Should `Holding` be registered as a lightweight entity by the agent before
  relationship proposals, or should Cruxible add first-class governed entity
  proposals?
- Which parts of opinion metadata should eventually live in a reference layer
  versus this local firm layer?
- Should review-item routing be one workflow with multiple proposed
  relationships, or remain split into explicit `opinion -> review item` and
  `review item -> matter` proposals?
- Which matter-impact edges should ever become trust-gated rather than always
  attorney-reviewed?

## Maintenance

Regenerate the structural sections after changing ontology, workflows, governed
relationships, or named queries:

```bash
uv run cruxible config-views --config kits/case-law-monitoring/config.yaml --update-readme kits/case-law-monitoring/README.md
```

To inspect the same generated bundle without editing the README:

```bash
uv run cruxible config-views --config kits/case-law-monitoring/config.yaml --view all
```

## Status

This is a first-pass config and README. Provider refs are placeholders, no seed
data is included, and the first real-usage iteration should focus on one
end-to-end story: new opinion or filing, proposed treatment/scope/impact edges,
attorney feedback, named-query context for the next agent recommendation, and a
later outcome that teaches the next pass.
