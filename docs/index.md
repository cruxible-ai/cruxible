# Cruxible documentation

Cruxible is a knowledge layer worth building. It aligns teams, agent swarms,
and future runs on shared, grounded state by letting agents treat domain
knowledge like code. Your code, docs, and databases remain the sources of truth:
typed state holds the structured claims and decisions pointing to them,
reasoning stays in plain Markdown to read and grep, and deterministic Python
pipelines seed and refresh observations from reality (APIs, sensors,
experiments, scanners, or custom models) at zero LLM tokens.

Cruxible marries typed state with your Markdown notes, code, and databases:
prose and source records stay where they live, while Cruxible keeps reviewed
judgments pinned to live observations and cited passages so they are retrieved
minimally and re-open only when reality or a premise moves. No language model
runs inside Cruxible. Your team and agents supply the judgment, pinned
providers supply domain retrieval and computation, and Cruxible checks,
records, and serves it back with full history.

## Start here

- [Quickstart](quickstart.md): attach Cruxible to any Git repo in five minutes,
  pick a starting vocabulary (`/survey` or `project-state`), write a cited
  Claim, watch `cruxible next` catch drift, and open the Console.
- [Concepts](concepts.md): the ideas behind Cruxible, and the words the rest
  of these docs use.
- [For AI agents](for-ai-agents.md): connecting an agent, the loop it runs,
  and the five agent skills (`npx skills add cruxible-ai/cruxible`), `survey`
  first for a project with no instance or kit.

## Walkthroughs

- [Project state walkthrough](project-state-walkthrough.md): seed a cited
  decision and two roadmap items across coding-agent sessions, edit the cited
  doc to trigger `citation_drifted`, supersede the decision so only dependent
  work wakes up, and install the `project-state` skill.
- [Security walkthrough](security-walkthrough.md): import an SBOM, pull OSV,
  CISA KEV, and FIRST EPSS automatically, and run the kit's `security-audit`
  skill under two-principal cosign review.
- [IoT fleet walkthrough](iot-fleet-walkthrough.md): run a deterministic EWMA
  telemetry Line over a camera hub and three cameras, absorb a transient spike
  at zero LLM tokens, and wake `cruxible next` on a sustained thermal fault and
  flatlined sensor.

## Guides

- [Building team knowledge](building-team-knowledge.md): what to record as
  structured facts, what to leave as prose, and habits that keep it useful.
- [Evidence and admission](evidence-and-admission.md): what a fact can rest
  on, and how to tell a checked fact from a guess.
- [Keeping knowledge current](keeping-knowledge-current.md): finding what went
  stale or changed underneath you, and dealing with it.
- [Asking questions](asking-questions.md): searching, saved questions, and
  what was true in the past.
- [Documents and notes](documents-and-notes.md): writing notes together with
  the facts they support, and pages that stay in step.
- [Lines and automation](lines-and-automation.md): scheduled and event-driven
  pipelines that run pinned providers (API fetches, analyzers, or custom ML
  models) and settle or propose what they find.
- [Review and governance](review-and-governance.md): who can change what, and
  how changes get reviewed.
- [Evolving your vocabulary](evolving-your-vocabulary.md): the vocabulary
  file, its format, and changing what you record without losing what you
  already know.
- [Kits](kits.md): the base kit every instance starts with, installing
  shared vocabularies built by others, capabilities and provider packages.

## Reference

- [CLI reference](cli-reference.md)
- [MCP tools](mcp-tools.md)
- [Python SDK](https://github.com/cruxible-ai/cruxible/blob/main/packages/cruxible-client/README.md)
- [Architecture](architecture.md)

## Operations

- [Upgrading](upgrading.md)
- [Isolated deployment](isolated-deployment.md)
- [Hosted runtime image](hosted-runtime-image.md)
- [Data and network](data-and-network.md)
