# Playbill developer quickstart

This quickstart targets the breaking Playbill development branch.

## Install

Requirements are Python 3.11+, Git, and uv.

~~~bash
uv sync --all-extras
~~~

## Start the daemon

In shell one:

~~~bash
uv run cruxible server start \
  --state-root /tmp/cruxible-playbill-dev \
  --bootstrap-secret-file /tmp/cruxible-playbill-bootstrap
~~~

The daemon creates a one-time bootstrap secret with mode 0600.

In shell two:

~~~bash
export CRUXIBLE_SERVER_URL=http://127.0.0.1:8100
export CRUXIBLE_SERVER_BEARER_TOKEN="$(cat /tmp/cruxible-playbill-bootstrap)"
~~~

Allocate an empty daemon-owned host. The CLI remembers it as the active
instance:

~~~bash
uv run cruxible playbill host create --instance-id playbill-demo
~~~

Initialize Playbill and generate an owner key outside the repository:

~~~bash
uv run cruxible playbill init \
  --key-dir /tmp/cruxible-playbill-owner \
  --principal-id bootstrap-admin
~~~

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
BODY_DIGEST="$(uv run cruxible playbill body store /tmp/demo-policy.md)"
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
uv run cruxible playbill document propose \
  --envelope /tmp/demo-envelope.json \
  --name add-demo-policy \
  --json
~~~

Copy the proposal ID from the response, then review and activate:

~~~bash
uv run cruxible playbill proposal review PROPOSAL_ID
uv run cruxible playbill proposal activate PROPOSAL_ID
~~~

Read accepted state and its explanation:

~~~bash
uv run cruxible playbill document get document:demo-policy
uv run cruxible playbill document body document:demo-policy
uv run cruxible playbill explain document:demo-policy --detail evidence
uv run cruxible playbill document history document:demo-policy
~~~

Storing body bytes was inert. Proposing created a frozen candidate. A voluntary
non-creator approval, when supplied, signs exactly that candidate. Only
activation changed accepted state.

## Source catalogs

For local or external files, author a portable catalog and optional ignored
local overlay. Compilation is client-side:

~~~bash
uv run cruxible playbill sources compile \
  --catalog sources.yaml \
  --root . \
  --output /tmp/source-bundle.json
~~~

Use sources check for read-only alignment validation and sources propose to
submit the frozen path-free bundle. The daemon never dereferences a client path.

## Verify the branch

~~~bash
uv run pytest -q tests/test_playbill tests/test_architecture/test_playbill_dp0_boundaries.py
uv run mypy src/cruxible_core/playbill src/cruxible_core/service
uv run ruff check src packages/cruxible-client/src tests
~~~
