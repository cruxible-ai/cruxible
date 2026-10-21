# Cruxible documentation

Cruxible is hard state for AI agents: typed, governed, durable state that
humans and agents share. Values are Claims about Subjects under ClaimTypes
that say what a value may be and what evidence backs it. Changes are proposed,
checked deterministically, accepted under an approval policy, and recorded in
a signed Git ledger, so every answer names the accepted coordinate it came
from. No LLM runs inside Cruxible.

## Start here

- [Quickstart](quickstart.md): record typed state, review a change from the
  outside world, see a page flagged out of date, and ask what the change
  reaches and what was true before.
- [Concepts](concepts.md): the model behind the commands.
- [Building team knowledge](building-team-knowledge.md): what to make typed state and what to
  keep as prose, how to connect them, and modeling habits that keep state useful.
- [For AI agents](for-ai-agents.md): operating rules and the read and write
  loops over MCP, the CLI and the Python SDK.
- [Kits](kits.md): install, upgrade and build releases of definitions.
- [Projection blocks](declared-blocks.md): keep pages in step with state.

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
