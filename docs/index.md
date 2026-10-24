# Cruxible documentation

Cruxible keeps what a team of people and AI agents knows about its world:
each fact structured, stored with the reason and the evidence behind it,
changed only through checked and reviewed changes, and kept with its full
history. Agents read what the team currently believes directly, instead of
re-reading every document, and can tell a checked fact from a guess.

No language model runs inside Cruxible. People and agents supply the
judgment; Cruxible checks it, records it and serves it back.

## Start here

- [Quickstart](quickstart.md): set Cruxible up on a project, let an agent
  pull in what you ship and run, and find out which of your dependencies carry
  a vulnerability attackers are exploiting now, with every answer traced to
  its source.
- [Concepts](concepts.md): the ideas behind Cruxible, and the words the rest
  of these docs use.
- [Security walkthrough](security-walkthrough.md): the same by hand, step by
  step, through to responding to an incident.

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
- [Lines and automation](lines-and-automation.md): recurring checks that run
  on a schedule and propose what they find.
- [Review and governance](review-and-governance.md): who can change what, and
  how changes get reviewed.
- [Evolving your vocabulary](evolving-your-vocabulary.md): changing what you
  record without losing what you already know.
- [For AI agents](for-ai-agents.md): connecting an agent, and the loop it
  runs.
- [Kits](kits.md): installing shared vocabularies built by others.

## Reference

- [CLI reference](cli-reference.md)
- [MCP tools](mcp-tools.md)
- [Python SDK](https://github.com/cruxible-ai/cruxible/blob/main/packages/cruxible-client/README.md)
  and its [Procedure source authoring](procedure-source.md) reference
- [Architecture](architecture.md)

## Operations

- [Upgrading](upgrading.md)
- [Isolated deployment](isolated-deployment.md)
- [Hosted runtime image](hosted-runtime-image.md)
