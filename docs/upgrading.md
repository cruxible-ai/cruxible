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
| `cruxible init` | Initialized a config-authority instance in the project's `.cruxible/` | Creates a daemon host when none is selected, bootstraps its governed state (owner principal, genesis) and attaches the worktree |
| `cruxible kit` | Installed and repinned bundled config kits | Builds, pulls, adds, upgrades and removes kit releases of definitions as governed change sets |
| `cruxible instance` | Managed config-authority instances | Decommissions a daemon-hosted instance |
| `cruxible query` | Ran a named query from the config | Runs a compact or named query over accepted state |
| `cruxible procedure` | Listed, showed, ran and withdrew config procedures | Runs and measures governed Procedures; `cruxible get Procedure:NAME` reads one |
| `cruxible_init` (MCP) | Initialized or reloaded a config instance | Removed: setup is an operator step (`cruxible init`) |
| `cruxible_query` (MCP) | Ran a named query | Runs a compact or named query over accepted state |
| `.cruxible/` in a project | The 0.3 instance directory (`instance.json`, `state.db`) | The workspace directory (workspace config, file catalog, floor, block stamps); a worktree whose `.cruxible/` holds a 0.3 instance is refused until it moves aside, and so is one whose `.cruxible/` is, lies inside or holds the daemon state root (`~/.cruxible` or `CRUXIBLE_STATE_ROOT`) |

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

1. Install the new release the same way you installed Cruxible:

   ~~~bash
   pip install --upgrade cruxible     # or: uv tool upgrade cruxible
   ~~~

   From a source checkout, `git pull` and `uv sync --all-packages --all-extras`.

   For a container, pull the new [runtime image](hosted-runtime-image.md).

2. Restart the daemon so it runs the new code:

   ~~~bash
   cruxible server restart
   ~~~

   A start after a release can migrate derived state before the daemon
   answers: evicting projection pieces past retention, compacting a loose
   ledger, rebuilding a missing head projection, and (just after it starts
   serving) building the `next` facts from nothing. On a large instance each
   can take minutes. The daemon logs a start line and a done line with counts
   and elapsed time for each (`startup_step_started` and
   `startup_step_finished` in `STATE_ROOT/daemon/logs/server.log`);
   When the daemon runs on this machine, `server restart` prints them while
   it waits, and each one restarts its `--timeout`; `cruxible server status`
   against a daemon that is still starting (it answers nothing until every
   instance has opened) says which step it is on rather than only failing to
   connect. A foreground `cruxible server start` prints them too.

3. Check the instance:

   ~~~bash
   cruxible next
   ~~~

   `next` prints any status facet that needs attention before its rows. After
   a restart, built-in workers resume from where they stopped; a worker whose
   state was written by an earlier release rebuilds that state, and its
   findings are complete again once it has caught up. Enabled Lines stay
   enabled and watch forward from the restart; time the daemon was down needs an
   explicit `cruxible line evaluate ... --dispatch`, which `cruxible next` names per gap (see
   [Recovering missed work](lines-and-automation.md#recovering-missed-work)).

If `next` prints nothing about the compiler (in `--json`, `status.compiler.state`
is `current`), the upgrade is done.

## Adopt a new compiler revision

When the running daemon installs a newer compiler than the instance's accepted
head uses, and an explicit upgrade path exists between them, `next` reports the
`compiler` facet as `upgrade_available`, and its repair is the bare command:

~~~bash
cruxible compiler upgrade
~~~

With no `--to`, the upgrade targets the compiler the daemon runs and is named
`upgrade-to-<revision>` (for example `upgrade-to-provider-builds-v1`). The
proposal pins that compiler's full digest, bound to the exact accepted head,
and its preview and review show the digest. `--to DIGEST` and `--name NAME`
override either default. The proposal is admin-only.

### Back up first

Activating a compiler upgrade is one-way. The accepted head then names the new
compiler, and a daemon serves an instance only through the compilers it
installs: an earlier release replays the upgrade on open, finds a target it does
not install, and refuses to open or serve the instance. No upgrade path leads
back. The upgrade's preview, proposal and review say so in a typed
`compiler_upgrade` notice (`one_way: true`).

So before you activate, stop the daemon and copy, from the state root
(`CRUXIBLE_STATE_ROOT`, else `~/.cruxible`):

- the instance directory, `instances/INSTANCE_ID/`, except `projections/`:
  projections are derived and rebuilt from the ledger when the instance
  opens;
- the daemon registry and runtime credential store,
  `daemon/registry.db` and `daemon/runtime_credentials.db` (and
  `daemon/provider-secrets/` if Providers use secrets);
- the pinned trust roots, `trust/` (`trust/INSTANCE_ID.json` for this
  instance).

~~~bash
cruxible server stop
mkdir -p BACKUP/instances BACKUP/daemon
rsync -a --exclude projections/ ~/.cruxible/instances/INSTANCE_ID BACKUP/instances/
cp -p ~/.cruxible/daemon/registry.db ~/.cruxible/daemon/runtime_credentials.db BACKUP/daemon/
cp -Rp ~/.cruxible/trust BACKUP/
cruxible server start   # or however you run the daemon
~~~

To go back, stop the daemon, put these back in place, and install the earlier
release. Signing keys live outside the state root (see below) and are not
part of this copy.

### Upgrade in one command

~~~bash
cruxible compiler upgrade          # preview; at a terminal, asks to commit
cruxible compiler upgrade --yes    # commit without asking
~~~

Because activation is one-way, the command previews first when it would
activate: it prints the source and target compiler, the exact target digest,
the one-way notice, and the signing key it will use, and writes nothing. The
commit (`y` at the prompt, `--yes`, or `--commit --at OID` with the OID the
preview printed) proposes the upgrade, signs it with your key and activates
it, in one command, when nothing else is needed:

- you are an admin (proposing and activating a compiler upgrade both need
  it);
- you hold your signing key;
- the approval policy needs no other principal's approval.

A compiler upgrade settles only with at least one signed approval, whatever
the approval policy says; a policy that asks for none still needs one
signature. Under the default policy your own signature is that approval, so
the one command lands it. Activation then validates the full projection under
the target compiler before it advances the signed ledger; a projection that
does not validate leaves the instance on its current compiler.

Otherwise the command proposes the upgrade and stops, and its result
(`outcome` in `--json`) names the reason and the next step:

| Reason | Meaning | Next |
|---|---|---|
| `independent_approval_required` | The policy (`init --require-independent-approval`) needs an ordinary principal other than the proposer; your own signature never counts toward it, so nothing is signed. | That principal runs `cruxible proposal approve PROPOSAL_ID`; their signature completes and activates it (activation needs admin: a graph_write approver's signature is recorded and an admin runs `cruxible proposal activate PROPOSAL_ID`). |
| `signing_key_missing` | No signing key was given or found. | `cruxible proposal approve PROPOSAL_ID --key PATH` (see below). |
| `accept_never` | `--no-accept`: propose only. | `cruxible proposal approve PROPOSAL_ID`. |
| `activation_not_permitted` | Your signature was recorded; your credential cannot activate a compiler upgrade. | An admin runs `cruxible proposal activate PROPOSAL_ID`. |
| `head_moved` | Accepted state moved after the proposal was checked. | `cruxible proposal readmit PROPOSAL_ID`. |
| `recompiled`, `approvals_remaining`, `refused` | As named. | The `next` the result carries. |

The two-step path stays available: `cruxible compiler upgrade --no-accept`,
then `cruxible proposal review PROPOSAL_ID` and
`cruxible proposal approve PROPOSAL_ID` (the approval that completes it
activates it; `--hold` signs only, and `cruxible proposal activate` lands it
later).

### Your signing key

`cruxible init` generates the owner's Ed25519 key in a per-user custody
directory outside every workspace and the state root:
`$XDG_CONFIG_HOME/cruxible/keys/INSTANCE_ID/PRINCIPAL_ID/PRINCIPAL_ID.ed25519`,
by default under `~/.config`. It prints that path, writes `cruxible.env`
beside it (which exports `CRUXIBLE_PRINCIPAL_KEY`), and the CLI remembers those
settings and loads them itself. So after `cruxible init`, `compiler upgrade`
and `proposal approve` find the key with no flag, and say where they found it.
They look, in order, at `--key`, `CRUXIBLE_PRINCIPAL_KEY` (your remembered
settings set it), then that default directory. A principal added with
`cruxible principal add ID --key-dir DIR` keeps its key and settings in `DIR`;
`cruxible context use --principal ID` makes the CLI act as it. When no key is
found, the refusal names the default path it checked and these commands.

After activation, new generations are accepted under the new compiler and
authoring that needs it is accepted. Earlier generations keep their original
compiler, so reads and proofs at them are unchanged. The instance descriptor
keeps its genesis compiler; inspection reports the active compiler from
accepted history.

This release installs one compiler, revision 39 (`nodes-extract-v1`), and no
upgrade edge leads to or from it: `cruxible compiler upgrade` refuses
`cruxible.compiler_upgrade.already_current` (or
`cruxible.compiler_upgrade.unsupported_transition` for any other target). An
instance created by an earlier, unshipped build records another compiler or
another genesis and is refused, never converted: the daemon answers
`cruxible.instance.reseed_required` and `cruxible host show` reports it
`reseed_required` (`compiler_lineage_not_writable`). Move it aside and re-import
its content into a fresh instance.

The compiler writes only v4 identity evidence policies. A ClaimType that still
states a v3 policy (the unedited genesis seeds) keeps working as it is, but
waits for `cruxible claim-type upgrade` to restate its policy as v4 before it
can be edited; the restatement is one reviewed change set that admits exactly
the same evidence and carries every Claim with its backing intact.

## Compiler facet states

| State | Meaning | What to do |
|---|---|---|
| `current` | The accepted head uses the compiler the daemon runs. | Nothing. |
| `upgrade_available` | The daemon runs a newer compiler with an explicit upgrade path from the accepted one. | Back up, then run the repair, `cruxible compiler upgrade`. |
| `claim_type_upgrade_available` | The accepted head uses the running compiler, and live ClaimTypes you authored still state a v3 evidence policy. They keep working; an edit of one is refused until it is restated as v4. The detail lists them (`claim_types`, up to 20, and `count`). | Run the repair, `cruxible claim-type upgrade`: preview, then commit and approve the one change set that restates them. |
| `no_upgrade_path` | The accepted compiler has no path to the running one, for example when the daemon is older than the state it serves. | Install a release whose daemon supports the accepted compiler. Nothing is proposed. |

Downgrades and unsupported transitions are refused.

## What an upgrade does not do

- Installing or restarting a daemon does not upgrade an instance.
- A compiler upgrade does not rewrite existing artifacts. Artifacts keep their
  original format and digests. Adopting a new format for an existing artifact
  is separate, governed authoring.
