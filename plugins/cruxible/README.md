# Cruxible agent plugin

One plugin supplies four general skills and a lean MCP client of the Cruxible
daemon. The instance's ClaimTypes, governed guidance Documents and Runbooks
describe the domain. The plugin contains no kit-specific audit method.

## Package and install

`plugins/release.json` is the release definition. **0.0.0 is an unpublished
version placeholder**: replace it with the chosen release version at the bump,
then run `python scripts/build_agent_plugin.py`. Do not publish the placeholder.
`--check` checks the generated manifests, catalogs, MCP configs, README, skill
copies and bundled docs without modifying them. Edit source skills under
`skills/`, not the generated `plugins/cruxible/skills/` copies. The core docs the
skills link are copied into `plugins/cruxible/docs/` from the release's own
checkout, so they match the pinned runtime; a link to a doc outside the bundle
keeps only its text.

Claude Code uses `.claude-plugin/marketplace.json` at this repository's root
and `.claude-plugin/plugin.json` plus `.mcp.json` inside `plugins/cruxible`.
Add the repository as a marketplace, then install
`cruxible@cruxible-plugins`. Codex uses the repository's
`.agents/plugins/marketplace.json`, portable `plugin.json` and `mcp.json`.
Add the repo marketplace through `codex plugin marketplace add /path/to/repo`
and install from the supported client's plugin directory. The two catalogs
have host-specific formats; the skill content and runtime selection are shared.

The released stdio command is `uvx --from cruxible==VERSION cruxible mcp`.
Install uv first. Keep the matching CLI available for setup (`init`,
`workspace attach`, `principal add`), `authoring submit`, kit pulls and local
kit directories, which the lean MCP catalog omits; the same pinned
`uvx --from cruxible==VERSION cruxible ...` prefix runs those commands.

## First run, step by step

One linear path from an empty project to an installed kit on one machine. The
CLI does the setup; the plugin's MCP tools do the everyday work afterwards. An
agent with no shell calls `cruxible_init` instead of steps 1 to 4 (a single
owner, `self_approval_allowed`) and `cruxible_authoring_submit_file` to submit a
file it tailored.
`SOCKET` below is the daemon socket the MCP server auto-starts:
`$CRUXIBLE_STATE_ROOT/run/daemon.sock`, or `~/.cruxible/run/daemon.sock` when
`CRUXIBLE_STATE_ROOT` is unset. Use absolute paths throughout, and keep key
directories outside the project and outside the state root.

**Server mode.** Every Cruxible command runs on a daemon, a long-running
process that owns the instance's state; the CLI is only its client. "Use
server mode" in a refusal means the CLI was not told which daemon to talk to.
Until a project is bound to a daemon (step 3 or 4 writes that binding), pass
`--server-socket SOCKET` or export `CRUXIBLE_SERVER_SOCKET=SOCKET`; the CLI
does not yet find the daemon the MCP server started on its own. A socket
daemon runs with auth off, so do not carry a `CRUXIBLE_SERVER_BEARER_TOKEN`
from another daemon into this shell: `unset CRUXIBLE_SERVER_BEARER_TOKEN`.

| # | Do | Notes |
|---|---|---|
| 1 | `cd /abs/project && git init` | The project must be a Git worktree before `init` for `init` to attach it. |
| 2 | Start the daemon: call `cruxible_server_info` from the agent once, or run `cruxible server start --socket SOCKET` in its own terminal | MCP auto-start runs the same command detached; `SOCKET`'s directory must be owner-only (the daemon creates it that way). |
| 3 | `cruxible --server-socket SOCKET init --principal-id owner --key-dir /abs/keys/owner --reviewer-key-dir /abs/keys/reviewer` | Creates the instance with two principals, `owner` and `reviewer`, under the default `self_approval_allowed` policy (`--require-independent-approval` makes approval need the second principal). Each key directory gets `<id>.ed25519` and a `cruxible.env` settings file; the result prints `owner_settings_path` and `reviewer_settings_path`. The CLI remembers the owner and acts as it from then on, and `.cruxible/identity.json` records the owner as the folder's default principal (a principal ID and the key directory's path; no key, no token). |
| 4 | Check `workspace_advertisement.status` in the init result. `updated`: done. `not_attached`: `git init` if needed, then `cruxible workspace attach` | Attaching writes `.cruxible/coverage.json`, which binds the project to the daemon and instance. From then on CLI commands in the project need no flags, and the MCP server finds the daemon and instance from it. |
| 5 | Call `cruxible_whoami` from the agent started in the project | `identity_source: folder`, `actor_id` the owner and `can_author: true`: the MCP server acts as the folder's owner with no config edit, for writes, approvals and attestations. `identity_source: none` with `cruxible.identity.principal_unconfigured` means the folder has no record: run `init` there. A session that should be another principal sets the environment below. |
| 6 | `cruxible principal add verifier --key-dir /abs/keys/verifier` | Optional third principal, for guidance that asks for a reporter, a verifier and an approver. The CLI signs the registration with the owner's remembered key (`--signer-key /abs/keys/owner/owner.ed25519` otherwise); the verifier's settings land in `/abs/keys/verifier/cruxible.env`. |
| 7 | `cruxible kit add /abs/kit-dir`, then `cruxible kit add /abs/kit-dir --commit --at OID` | `OID` is the coordinate the preview printed. `cruxible_kit_add` takes the same local kit directory, layout or registry release (`NAME:VERSION`). |
| 8 | Tailor the kit's automation template, submit it, have a separate session approve it, then preview and run its start Runbook | See [Kits and teams](#kits-and-teams). |

**MCP environment per principal.** Solo on one machine, the folder record is
enough. A session that acts as another principal (a reviewer, a teammate's
agent) puts the values in the `env` block of the host's MCP config (a private,
local override, never the plugin); any of the three identity variables takes
precedence over the folder record. The MCP server never reads remembered CLI
context or `cruxible.env` itself.

| Variable | Value | Used for |
|---|---|---|
| `CRUXIBLE_PRINCIPAL_ID` | the principal ID (`CRUXIBLE_PRINCIPAL_ID` in its `cruxible.env`) | every attributed write |
| `CRUXIBLE_MCP_KEY_DIR` | the principal's key directory, the folder holding `<id>.ed25519` and `cruxible.env` | `cruxible_proposal_approve` signing |
| `CRUXIBLE_PRINCIPAL_KEY` | the key file (`CRUXIBLE_PRINCIPAL_KEY` in its `cruxible.env`) | `cruxible_claim_attest` signing |
| `CRUXIBLE_MCP_WORKSPACE_ROOT` | the absolute project root | only when the host starts MCP outside the project |
| `CRUXIBLE_INSTANCE_ID`, `CRUXIBLE_SERVER_SOCKET` | from `cruxible.env` | only when the project has no `.cruxible/coverage.json` binding |

To stop the daemon: `cruxible --server-socket SOCKET server stop`.

**Two writes when a Claim cites a fresh note.** A section a write produces
counts as its author's own words. A field that requires captured evidence
refuses it with `cruxible.write.evidence_required`. Write the note first, then
cite the existing section by file and heading path in a later write. The
bundled `docs/documents-and-notes.md` ("Writing notes with the Claims they
back") covers it.

## Local use and pre-release testing

With no explicit daemon transport, the adapter finds or auto-starts the local
daemon. The default tier is **admin**; the pinned 25-tool allowlist still hides
the rest of the catalog. Admin permits endpoints, not unlawful state: evidence,
principals, Mandates and approval policy still apply. A separate agent session
(its own principal) reviews the exact candidate; never self-approve, including with auth off. The single
plugin does not enforce separate identities while auth is off.

The plugin catalog measures 18,287 estimated model-visible tokens (input schemas
and descriptions, JSON characters/4), pinned below 18,300. The existing MCP
default profile retains its separate 10,400 ceiling; full-catalog registration
is not what this plugin advertises. Instructions remain below 300 estimated
tokens, without exhaustive tool lists. Output-schema wire size is separate
from this estimate; actual host tokenization/tool deferral can differ.

Before there is an instance, `cruxible_server_info` can auto-start its daemon;
[First run, step by step](#first-run-step-by-step) initializes against it.
Never store keys or tokens in the plugin.

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
        "CRUXIBLE_MCP_TOOLS": "COPY THE GENERATED CONFIG'S EXACT ALLOWLIST",
        "CRUXIBLE_PRINCIPAL_ID": "owner",
        "CRUXIBLE_MCP_KEY_DIR": "/absolute/keys/owner",
        "CRUXIBLE_PRINCIPAL_KEY": "/absolute/keys/owner/owner.ed25519"
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
approval/acceptance from a separate agent session (its own principal), then preview and run its start Runbook.
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
