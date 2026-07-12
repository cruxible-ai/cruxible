# Cruxible Kits

Kits are maintained Cruxible state models intended to be used, overlaid, and
iterated with agents. Each kit includes a YAML config and a README with generated
views for the ontology, governed relationships, workflows, and named queries.

## Maintained Kits

| Kit | Domain | Status | Purpose |
|---|---|---|---|
| [kev-reference](kev-reference/) | Cybersecurity | ready | Standalone public KEV, NVD, EPSS, vendor, product, and vulnerability reference state. |
| [kev-triage](kev-triage/) | Cybersecurity | ready | Overlay kit for local assets, exposure triage, remediation, incidents, and controls. |
| [agent-operation](agent-operation/) | Agent Operations | ready | Reusable operating-state overlay for agent/human work, reviews, decisions, blockers, dependencies, composition, and lineage. |
| [project-domain](project-domain/) | Project/Product | ready | Project/product domain overlay composed over agent-operation: roadmap, releases, milestones, areas, capabilities. |
| [supply-chain-blast-radius](supply-chain-blast-radius/) | Supply Chain | ready | Supplier, component, product, shipment, and incident blast-radius modeling. |
| [case-law-monitoring](case-law-monitoring/) | Legal | ready | Matter-centered case-law monitoring and authority impact modeling. |

*ready* kits ship working providers (KEV also ships public reference data).

## Working With A Kit

Use the generated README views as the review surface while drafting or fitting a
kit. Regenerate them after config changes:

```bash
uv run cruxible config views --config kits/<kit>/config.yaml --update-readme kits/<kit>/README.md
```

For layered kits such as KEV triage, include `--runtime` so generated views use
the composed runtime config.

Standalone kits can be initialized with `cruxible init --kit <kit>`. Overlay
kits are created with `cruxible state create-overlay --kit <kit>`.

## Skill Authoring Rules

Kits may ship agent skills (`skills/<name>/SKILL.md`), but a skill is the
*fragile* layer: prose duplicating what the system knows authoritatively will
drift and rot silently. Skills therefore carry **only judgment the agent could
not reconstruct from the live system**.

**The razor — apply per line.** If an agent could reconstruct the content from
`--help`, `cruxible query describe`, `cruxible config views`, the kit's
generated README blocks, or a daemon error message, delete it and point at
that surface instead. Duplication is the rot mechanism; pointers cannot rot.

What a skill MAY contain:

- **Judgment criteria** — what counts as sufficient evidence, when a closure
  claim is credible, where the evidence boundary sits (e.g. "scanner findings
  are evidence refs, never entities").
- **Stop-and-ask conditions** — the situations where proposing anyway would
  corrupt the graph (unresolvable IDs, ambiguous scope, report-vs-graph
  conflict).
- **Output contracts** — the shape of a good summary or hand-off (taxonomies
  like elevated / overdue / waived / remediated-but-conflicted).
- **Tailoring judgment** — keep/modify/remove reasoning when adapting the kit
  to user data, and the data-quality bars that gate onboarding.

What a skill MUST NOT contain:

- Inline CLI invocations or option syntax (name the command verb; the agent
  runs `--help`).
- Catalogs of queries, relationships, signal sources, or enum values — those
  are config-owned and already rendered into the generated README blocks.
- Workflow orderings that are already implied by config (a stage that reads
  accepted edges from an earlier stage documents its own dependency).
- Restatements of governance the daemon enforces (write policies, guards,
  proposal requirements). One line on how to *react* to a refusal is fine;
  describing the enforcement is not.

Prefer no skill at all: deterministic loops belong in workflows, kit facts
belong in config, and operating discipline that is not kit-specific belongs in
the product surface, not in every kit.

