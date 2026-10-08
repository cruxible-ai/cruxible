<p align="center">
  <a href="https://cruxible.ai">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-white.svg">
      <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-black.svg" alt="Cruxible" width="360">
    </picture>
  </a>
</p>

# Cruxible

Cruxible is hard state for AI agents: typed, governed, durable state that
humans and agents share. Every value is a Claim about a Subject, under a
ClaimType that says what the value may be and what evidence backs it. Every
change is proposed, checked deterministically, accepted under the instance's
approval policy, and recorded in a signed Git ledger, so every answer can say
where it came from and at which accepted coordinate.

No LLM runs inside Cruxible. Agents and people propose, review and query;
Cruxible validates and settles deterministically.

## What it does

- **Reads in three verbs.** `orient` maps an instance (its Subject kinds,
  fields, artifacts, and what needs attention), `query` answers a question as
  rows of values with verdict flags, and `get` reads one thing by any
  reference, values first. The floor, a directory of plain files the daemon
  keeps current, makes the same state greppable.
- **Writes in two lanes.** Values change with `set`, `add`, `retire` and
  `write` (several changes as one atomic change set), accepted at once when
  the approval policy allows. Definitions (Subjects, named queries,
  Procedures, Lines, Triggers, policies) go through `authoring`, which reports
  every refusal before anything is proposed. ClaimTypes keep their own
  `claim-type` group, because changing vocabulary decides what happens to
  every Claim that uses it.
- **Evidence you can check.** A Claim can cite a passage of a catalogued
  workspace file; when the file changes, `next` reports the citation as
  drifted. A whole file can also be governed as a Document.
- **Governed review.** Proposals are frozen candidates. Approvals are Ed25519
  signatures made with client-held keys over the exact candidate. Activation
  advances accepted state by compare-and-set.
- **Procedures and Lines.** A Procedure is a governed, deterministic graph
  that reads state and calls pinned providers. Run as a Line, it can also
  retain evidence and propose or settle changes; a Line runs on its Triggers
  once a principal enables it, and on demand with `line run`.
- **Kits.** Definitions travel between instances as kit releases, installed
  and upgraded as one reviewed change set.

## Install

Requirements: Python 3.11+ and Git.

~~~bash
pip install cruxible        # or: uv tool install cruxible
~~~

The package installs the `cruxible` CLI, the daemon, the MCP server, and the
`cruxible-client` Python SDK.

## Start

Start a daemon on a Unix socket. A socket daemon runs with auth off and says
so: every process of your OS user is equally trusted. A TCP daemon refuses to
start without `--auth`.

~~~bash
cruxible server start --socket ~/.cruxible/run/daemon.sock
~~~

In another shell, inside the Git repository you want to work in:

~~~bash
export CRUXIBLE_SERVER_SOCKET=~/.cruxible/run/daemon.sock
cruxible init
cruxible orient
~~~

With no instance selected, `cruxible init` creates a host on the daemon,
makes you its owner under your OS username with a key kept under
`~/.config/cruxible/keys/`, attaches the repository as its workspace, and
remembers all of it, so later commands need no flags or environment. The
[Quickstart](docs/quickstart.md) continues from here: define vocabulary, write
values with evidence, read them back, review a change, and render a table
into a page.

An MCP client launches `cruxible mcp` (`uvx cruxible mcp` from the registry
listing). Every tool runs on a daemon: the server uses `CRUXIBLE_SERVER_SOCKET`
from its `env` block, or reuses the local daemon on
`~/.cruxible/run/daemon.sock`, starting one there when none answers. See
[MCP tools](docs/mcp-tools.md#the-daemon).

## Credentials and principals

Two different things answer "who is this":

- a **credential** is a daemon bearer token. It lets a caller reach the
  daemon and caps what it may do with a tier (`read_only`, `governed_write`,
  `graph_write`, `admin`). Credentials are managed with `cruxible credential`
  and matter when the daemon runs with `--auth`;
- a **principal** is a governed identity in the instance's ledger: a public
  key with a role (owner, ordinary or recovery). Principals attribute every
  governed act, and an approval is a signature made with the principal's
  private key, which never leaves the client. Principals are managed with
  `cruxible principal`.

On an auth-off socket daemon the principal ID a process sends is a claim of
identity, not authentication. Local key directories give attribution and
repository hygiene; they are not a security boundary between processes of the
same OS user.

## Documentation

- [Quickstart](docs/quickstart.md)
- [Concepts](docs/concepts.md)
- [Modeling state](docs/modeling-state.md)
- [For AI agents](docs/for-ai-agents.md)
- [Kits](docs/kits.md)
- [CLI reference](docs/cli-reference.md)
- [MCP tools](docs/mcp-tools.md)
- [Python SDK](packages/cruxible-client/README.md)
- [Upgrading](docs/upgrading.md)

## Develop

~~~bash
git clone https://github.com/cruxible-ai/cruxible
cd cruxible
uv sync --all-packages --all-extras
uv run pytest -n auto --dist loadfile
~~~

See [CONTRIBUTING.md](CONTRIBUTING.md) for the provider-runtime tests, lint,
format and type checks.

Apache-2.0 licensed.

<!-- mcp-name: io.github.cruxible-ai/cruxible-core -->
