# Cruxible agent plugin

One plugin supplies four general skills and a lean MCP client of the Cruxible
daemon. The instance's ClaimTypes, governed guidance Documents and Runbooks
describe the domain. The plugin contains no kit-specific audit method.

## Package and install

`plugins/release.json` is the release definition. **0.0.0 is an unpublished
version placeholder**: replace it with the chosen release version at the bump,
then run `python scripts/build_agent_plugin.py`. Do not publish the placeholder.
`--check` checks the generated manifests, catalogs, MCP configs, README and skill
copies without modifying them. Edit source skills under `skills/`, not the
generated `plugins/cruxible/skills/` copies.

Claude Code uses `.claude-plugin/marketplace.json` at this repository's root
and `.claude-plugin/plugin.json` plus `.mcp.json` inside `plugins/cruxible`.
Add the repository as a marketplace, then install
`cruxible@cruxible-plugins`. Codex uses the repository's
`.agents/plugins/marketplace.json`, portable `plugin.json` and `mcp.json`.
Add the repo marketplace through `codex plugin marketplace add /path/to/repo`
and install from the supported client's plugin directory. The two catalogs
have host-specific formats; the skill content and runtime selection are shared.

The released stdio command is `uvx --from cruxible==VERSION cruxible mcp`.
Install uv first. Keep the matching CLI available for `authoring submit`, kit
pulls and attestations omitted from the lean MCP catalog; the same pinned
`uvx --from cruxible==VERSION cruxible ...` prefix runs those commands.

## Local use and pre-release testing

With no explicit daemon transport, the adapter finds or auto-starts the local
daemon. The default tier is **admin**; the pinned 20-tool allowlist still hides
the rest of the catalog. Admin permits endpoints, not unlawful state: evidence,
principals, Mandates and approval policy still apply. A second agent reviews
the exact candidate; never self-approve, including with auth off. The single
plugin does not enforce separate identities while auth is off.

The plugin catalog measures 15,083 estimated model-visible tokens (input schemas
and descriptions, JSON characters/4), pinned below 15,400. The existing MCP
default profile retains its separate 10,400 ceiling; full-catalog registration
is not what this plugin advertises. Instructions remain below 300 estimated
tokens, without exhaustive tool lists. Output-schema wire size is separate
from this estimate; actual host tokenization/tool deferral can differ.

Before there is an instance, `cruxible_server_info` can auto-start its daemon.
Initialize using that daemon's socket and the matching CLI, for example
`cruxible --server-socket SOCKET init --principal-id owner --key-dir /absolute/private/owner-keys`.
Run from the intended project and configure `CRUXIBLE_MCP_WORKSPACE_ROOT` to
that absolute project root if the host starts MCP elsewhere. Select the
instance using its workspace binding or `CRUXIBLE_INSTANCE_ID`; configure the
registered principal and approval key directory outside the plugin. The
adapter does not use remembered CLI context. Never store keys or tokens here.

For a pre-release checkout, override the plugin's server command in a local
host configuration. Use an existing environment with the checkout's matching
dependencies; no published package or new virtual environment is required:

```json
{
  "mcpServers": {
    "cruxible": {
      "type": "stdio",
      "command": "/absolute/existing-env/bin/python",
      "args": ["-m", "cruxible_core.mcp"],
      "env": {
        "PYTHONPATH": "/absolute/checkout/src:/absolute/checkout/packages/cruxible-client/src",
        "CRUXIBLE_STATE_ROOT": "/absolute/scratch/state",
        "CRUXIBLE_MCP_WORKSPACE_ROOT": "/absolute/scratch/project",
        "CRUXIBLE_MODE": "admin",
        "CRUXIBLE_MCP_PROFILE": "full",
        "CRUXIBLE_MCP_TOOLS": "COPY THE GENERATED CONFIG'S EXACT ALLOWLIST"
      }
    }
  }
}
```

Copy the generated allowlist rather than replacing it with the full catalog.
Set a scratch HOME/XDG configuration when testing initialization. The smoke
test loads each generated MCP configuration, substitutes this local checkout
launcher for the unpublished distribution, auto-starts only a fresh scratch
daemon, initializes an instance, lists tools and calls orient. It stops its own
daemon afterward. `uv run --project /absolute/checkout --locked cruxible mcp`
is another launch option when that checkout already has a synced environment.

## Kits and teams

Use adopt-kit to preview and install a digest-pinned release with `kit add`.
A kit's manifest names its inert automation template: tailor, submit and obtain
second-agent approval/acceptance, then preview and run its start Runbook.
Enabling scheduled Lines is a separate decision. A review-stopped run leaves a
proposal to resolve before an explicit rerun. Report provider/ordering refusals
as served; do not manually pre-seed Contracts as an onboarding workaround.

Teams provision a daemon and authenticated credentials separately. Override
the adapter's tier to governed_write for authors, graph_write for reviewers,
and admin for operators; retain the allowlist and let tiers narrow it. Each
principal owns its key. Configure transport, credentials, principal and
`CRUXIBLE_MCP_KEY_DIR` through private host settings. An explicitly configured
unreachable transport refuses rather than auto-starting a replacement. Do not
silently upgrade another running daemon.

Public OpenAI local-MCP directory distribution has its own partner approval
and desktop requirements; a repo marketplace is the development path, not a
claim of public-directory availability. See the official
[Claude plugin format](https://code.claude.com/docs/en/plugins-reference),
[Claude marketplaces](https://code.claude.com/docs/en/plugin-marketplaces),
[OpenAI packaging](https://developers.openai.com/plugins/build/plugins) and
[local-MCP submission requirements](https://developers.openai.com/plugins/deploy/desktop-only).

Maintainer skill review and the fresh kit-start gate precede benchmark use.
Record the plugin/runtime/kit versions, initial coordinate, source fixtures,
model budget and reviewer identities. The package adds no session hooks and
does not inject an instance dump.
