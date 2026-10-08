# Cruxible documentation

Cruxible is hard state for AI agents: typed, governed, durable state that
humans and agents share. Values are Claims about Subjects under ClaimTypes
that say what a value may be and what evidence backs it. Changes are proposed,
checked deterministically, accepted under an approval policy, and recorded in
a signed Git ledger, so every answer names the accepted coordinate it came
from. No LLM runs inside Cruxible.

## Start here

- [Quickstart](quickstart.md): start a daemon, define vocabulary, write and
  read values, cite a file, review a change, render a table into a page.
- [Concepts](concepts.md): the model behind the commands.
- [Modeling state](modeling-state.md): what to make typed state and what to
  keep as prose, and how to shape Subjects, ClaimTypes and Procedures.
- [For AI agents](for-ai-agents.md): operating rules and the read and write
  loops over MCP, the CLI and the Python SDK.
- [Kits](kits.md): install, upgrade and build releases of definitions.
- [Projection blocks](declared-blocks.md): keep pages in step with state.

## Reference

- [CLI reference](cli-reference.md)
- [MCP tools](mcp-tools.md)
- [Python SDK](https://github.com/cruxible-ai/cruxible/blob/main/packages/cruxible-client/README.md)
  and its [Procedure source authoring](sdk-v2-reference.md) reference
- [Architecture](architecture.md)

## Operations

- [Upgrading](upgrading.md)
- [Isolated deployment](isolated-deployment.md)
- [Hosted runtime image](hosted-runtime-image.md)
