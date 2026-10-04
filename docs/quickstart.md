# Cruxible developer quickstart

This quickstart targets the breaking Cruxible development branch.

## Install

Requirements are Python 3.11+, Git, and uv.

~~~bash
uv sync --all-extras
~~~

## Start the daemon

In shell one:

~~~bash
uv run cruxible server start \
  --socket /tmp/cruxible-playbill-run/daemon.sock \
  --state-root /tmp/cruxible-playbill-dev
~~~

A Unix-socket daemon runs with auth off and says so in one line when it starts:
every process of your OS user is equally trusted, so no bearer token is needed
locally. A TCP daemon refuses to start without `--auth`. The daemon binds the
socket with mode 0600 in a directory it creates with mode 0700. It refuses to
start unless the socket's directory is yours and owner-only, and no ancestor
lets another user replace it (a sticky root-owned `/tmp` is fine; a socket
directly in `/tmp` is not).

In shell two:

~~~bash
export CRUXIBLE_SERVER_SOCKET=/tmp/cruxible-playbill-run/daemon.sock
~~~

Allocate an empty daemon-owned host. The CLI remembers it as the active
instance. When run inside a Git worktree, the local socket also lets the daemon
attach that exact workspace before initialization:

~~~bash
uv run cruxible host create --instance-id inst_demo
~~~

Initialize Cruxible and make yourself the owner, with a key generated outside
the repository. The principal ID is yours to choose:

~~~bash
uv run cruxible init \
  --key-dir /tmp/cruxible-playbill-owner \
  --principal-id me
export CRUXIBLE_PRINCIPAL_ID=me
~~~

Every later command, SDK session and MCP server sends `CRUXIBLE_PRINCIPAL_ID`,
and the daemon attributes writes to that principal after checking it is
registered and active. With auth off this is a claim of identity, not
authentication: every process of your OS user is equally trusted. Approvals
are still signed with the principal's private key.

The private key remains in its client custody directory; the daemon receives
only its public ordinary-principal record. Local key directories provide
attribution and repository hygiene, not a security boundary. To opt into an
independent in-daemon approval requirement, add both
`--reviewer-key-dir /tmp/cruxible-playbill-reviewer` and
`--require-independent-approval`. Organization review normally rides the state
repository's branch protection and CODEOWNERS policy. Real custody separation
belongs at the parked Cloud broker/leasing seam.

## Govern a Document

Create a body:

~~~bash
printf '# Demo policy\n\nExact governed bytes.\n' > /tmp/demo-policy.md
BODY_DIGEST="$(uv run cruxible body store /tmp/demo-policy.md)"
~~~

Create an envelope at /tmp/demo-envelope.json, substituting the entire
`BODY_DIGEST_FROM_PREVIOUS_COMMAND` value with the printed digest:

~~~json
{
  "identity": "document:demo-policy",
  "document_kind": "policy",
  "title": "Demo policy",
  "media_type": "text/markdown",
  "body_digest": "BODY_DIGEST_FROM_PREVIOUS_COMMAND",
  "governance_scope": ["project:demo"],
  "lifecycle": {"revision": 1}
}
~~~

Propose it:

~~~bash
uv run cruxible document propose \
  --envelope /tmp/demo-envelope.json \
  --name add-demo-policy \
  --json
~~~

Copy the proposal ID from the response, then review and activate:

~~~bash
uv run cruxible proposal review PROPOSAL_ID
uv run cruxible proposal activate PROPOSAL_ID
~~~

Read accepted state and its explanation:

~~~bash
uv run cruxible orient --section documents
uv run cruxible get Document:demo-policy
uv run cruxible get Document:demo-policy --detail body
uv run cruxible get Document:demo-policy --detail why
uv run cruxible get Document:demo-policy --detail history
~~~

Storing body bytes was inert. Proposing created a frozen candidate. A voluntary
non-creator approval, when supplied, signs exactly that candidate. Only
activation changed accepted state.

## Add a propose-only agent

A propose-only agent authors and proposes but cannot approve or activate. That
limit is a credential tier, so it needs a daemon with auth. Start the daemon
with `--auth` instead:

~~~bash
uv run cruxible server start \
  --socket /tmp/cruxible-playbill-run/daemon.sock \
  --state-root /tmp/cruxible-playbill-dev --auth
~~~

The daemon never prints its bootstrap secret. It writes it owner-only (0600) to
`<state-root>/daemon/bootstrap-secret`, and `server status`, `restart` and
`stop` read it from there when they talk to this daemon, so a local restart
needs no credential typed in. Allocate the host with it, claim the one-time
operator credential, then initialize: init makes you the owner and mints your
own admin credential into your settings file:

~~~bash
export CRUXIBLE_SERVER_SOCKET=/tmp/cruxible-playbill-run/daemon.sock
SECRET_FILE=/tmp/cruxible-playbill-dev/daemon/bootstrap-secret
CRUXIBLE_SERVER_BEARER_TOKEN="$(cat "$SECRET_FILE")" \
  uv run cruxible host create --instance-id inst_demo
uv run cruxible credential claim-bootstrap --secret-file "$SECRET_FILE"
export CRUXIBLE_SERVER_BEARER_TOKEN=<the admin token it printed>
uv run cruxible init --key-dir /tmp/cruxible-playbill-owner --principal-id me
set -a; . /tmp/cruxible-playbill-owner/cruxible.env; set +a
~~~

Add the agent in one command. `--signer-key` defaults to your own key from the
settings you just loaded, so the registration is proposed, approved by you, and
activated, and the agent's `governed_write` credential is minted:

~~~bash
uv run cruxible principal add agent-b --key-dir /tmp/agent-b
~~~

Hand the agent its directory. It loads its settings and acts as `agent-b`:

~~~bash
set -a; . /tmp/agent-b/cruxible.env; set +a
uv run cruxible whoami        # agent-b, governed_write
uv run cruxible document propose --envelope /tmp/demo-envelope.json \
  --name agent-b-change
uv run cruxible proposal activate PROPOSAL_ID   # refused: needs graph_write
~~~

You review, approve, and activate the agent's proposal under your own settings.
With auth off (the default socket daemon) the same `principal add` registers
the agent and writes its settings without a credential, but every process of
your OS user is equally trusted, so nothing stops a process from loading your
settings instead; the principal ID is a claim, not a boundary.

## Source catalogs

For local or external files, author a portable catalog and optional ignored
local overlay. Compilation is client-side:

~~~bash
uv run cruxible sources compile \
  --catalog sources.yaml \
  --root . \
  --output /tmp/source-bundle.json
~~~

Use sources check for read-only alignment validation and sources propose to
submit the frozen path-free bundle. The daemon never dereferences a client path.

## Verify the branch

~~~bash
uv run pytest -q tests/test_ledger tests/test_claims tests/test_procedures tests/test_architecture
uv run mypy src
uv run ruff check src packages/cruxible-client/src tests
~~~
