# Cruxible documentation

Cruxible is a state layer that gives your agents and teams a shared model of
what they believe to be true. It lets agents and people build on what's already
verified instead of starting from scratch or overwriting each other—recording
typed facts and reviewed judgments with citations, running autonomous pipelines
to pull from APIs, scanners, analyzers, and custom ML models, and drawing
attention only to what changed when inputs move.

Cruxible doesn't replace your code, docs, or tools—it points to them, records
what was concluded from them, and tracks when they change. No language model
runs inside Cruxible: people and agents supply the judgment, pinned providers
supply domain retrieval and computation, and Cruxible checks, records, and
serves it back with full history.

## Start here

- [Quickstart](quickstart.md): set Cruxible up on a project, read in its
  dependencies, and find out which carry a known vulnerability and which of
  those attackers are exploiting, with every answer traced to its source.
- [Concepts](concepts.md): the ideas behind Cruxible, and the words the rest
  of these docs use.
- [Security walkthrough](security-walkthrough.md): the same steps one at a
  time with their outputs, through to a judgment a second agent reviews.

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
  pipelines that run pinned providers—API fetches, analyzers, or custom ML
  models—and settle or propose what they find.
- [Review and governance](review-and-governance.md): who can change what, and
  how changes get reviewed.
- [Evolving your vocabulary](evolving-your-vocabulary.md): the vocabulary
  file, its format, and changing what you record without losing what you
  already know.
- [For AI agents](for-ai-agents.md): connecting an agent, the loop it runs,
  and the agent plugin's five skills, `survey` first for a project with no
  instance or kit.
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
