# Upgrading

Two kinds of change reach an instance, and they move separately:

- **Software** is the `cruxible` package and its daemon. Installing a new
  release and restarting the daemon never changes accepted state.
- **The compiler** is the set of rules that interprets accepted state. Adopting
  a new compiler revision is a governed change to the instance: it is proposed,
  approved and activated like any other change.

Most releases only need the first. A release that adds a compiler revision also
offers the second, and `cruxible next` says when one is available.

## Same name, new meaning

Some names 0.3.2 shipped are used again with a different meaning. These names
are reused; review 0.3.2 scripts and allowlists against the new meanings
before enabling them.

| Name | In 0.3.2 | Now |
|---|---|---|
| `cruxible init` | Initialized a config-authority instance in the project's `.cruxible/` | Bootstraps governed state (principals and genesis) on a daemon host |
| `cruxible kit` | Installed and repinned bundled config kits | Builds, adds and removes kit releases as governed proposals |
| `cruxible instance` | Managed config-authority instances | Decommissions a daemon-hosted instance |
| `cruxible query` | Ran a named query from the config | Runs a compact or named query over accepted state |
| `cruxible procedure` | Showed and withdrew config procedures | Binds, runs and measures governed Procedures |
| `cruxible_init` (MCP) | Initialized or reloaded a config instance | Bootstraps governed state |
| `cruxible_query` (MCP) | Ran a named query | Runs a compact or named query over accepted state |
| `.cruxible/` in a project | The 0.3 instance directory (`instance.json`, `state.db`) | The workspace directory (client custody, sources, floor); a worktree whose `.cruxible/` holds a 0.3 instance is refused until it moves aside, and so is one whose `.cruxible/` is, lies inside or holds the daemon state root (`~/.cruxible` or `CRUXIBLE_STATE_ROOT`) |

## What the compiler is

The compiler turns the accepted ledger (the Claims, ClaimTypes, Procedures,
Documents and contracts in its Git tree) into the projection that reads,
explanations and proofs use. It also fixes which artifact kinds and format
versions are legal.

Each compiler revision is identified by its digest, and that digest is part of
every accepted coordinate. So a read or proof always names the rules that
interpreted the state it came from. Each accepted generation keeps the compiler
it was accepted under.

## Update the software

1. Install the new release the same way you installed Cruxible. From a source
   checkout, as in the [quickstart](quickstart.md), that is:

   ~~~bash
   git pull
   uv sync --all-extras
   ~~~

   For a container, pull the new [runtime image](hosted-runtime-image.md).

2. Restart the daemon so it runs the new code:

   ~~~bash
   cruxible server restart
   ~~~

3. Check the instance:

   ~~~bash
   cruxible next
   ~~~

   `next` prints any status facet that needs attention before its rows. After
   a restart, built-in workers resume from where they stopped; a worker whose
   state was written by an earlier release rebuilds that state, and its
   findings are complete again once it has caught up. Armed Lines keep their
   arms and watch forward from the restart; time the daemon was down needs an
   explicit `cruxible line evaluate` (see
   [line](cli-reference.md#line)).

If the `compiler` facet reads `current`, the upgrade is done.

## Adopt a new compiler revision

When the running daemon installs a newer compiler than the instance's accepted
head uses, and an explicit upgrade path exists between them, `next` reports the
`compiler` facet as `upgrade_available`. Its repair is the exact command, with
the target digest and a proposal name filled in:

~~~bash
cruxible compiler upgrade --to sha256:... --name upgrade-to-...
~~~

That only proposes the upgrade. The proposal is admin-only and bound to the
exact accepted head and target compiler. Then:

~~~bash
cruxible proposal review PROPOSAL_ID
cruxible proposal approve PROPOSAL_ID
cruxible proposal activate PROPOSAL_ID
~~~

Approval follows the instance's approval policy, as for any proposal.
Activation validates the full projection under the target compiler before it
advances the signed ledger; a projection that does not validate leaves the
instance on its current compiler.

After activation, new generations are accepted under the new compiler and
authoring that needs it is accepted. Earlier generations keep their original
compiler, so reads and proofs at them are unchanged. The instance descriptor
keeps its genesis compiler; inspection reports the active compiler from
accepted history.

You only need the upgrade to use what the new revision adds, such as new
artifact format versions. Until then the daemon keeps serving the instance on
its current compiler and refuses authoring that needs the new revision.

## Compiler facet states

| State | Meaning | What to do |
|---|---|---|
| `current` | The accepted head uses the compiler the daemon runs. | Nothing. |
| `upgrade_available` | The daemon runs a newer compiler with an explicit upgrade path from the accepted one. | Run the repair to propose the upgrade, then approve and activate it. |
| `no_upgrade_path` | The accepted compiler has no path to the running one, for example when the daemon is older than the state it serves. | Install a release whose daemon supports the accepted compiler. Nothing is proposed. |

Downgrades and unsupported transitions are refused.

## What an upgrade does not do

- Installing or restarting a daemon does not upgrade an instance.
- A compiler upgrade does not rewrite existing artifacts. Artifacts keep their
  original format and digests. Adopting a new format for an existing artifact
  is separate, governed authoring.
