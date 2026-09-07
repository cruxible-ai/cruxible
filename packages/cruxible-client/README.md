# cruxible-client

Typed authoring SDK, World reads, and HTTP contracts for a Cruxible Playbill
daemon.

Install `cruxible-client` in agent environments that should only talk to a
separate Cruxible daemon over HTTP/MCP.

This package contains:

- `Playbill`: typed Claim, Subject, ClaimType, changeset, and bounded Procedure authoring
- `World`: coordinate-bound vocabulary, Subject attributes, and bounded Claim prefetch
- source selections, Capture references, audit/repair reads, and agent-owned projection helpers
- `CruxibleClient`: the lower-level typed HTTP client
- shared public contracts, client-side error decoding, and local approval/Claim-attestation signing

It does not ship the daemon/runtime, Git ledger, CAS, projection internals, or
MCP server implementation. Those stay in `cruxible`.

If you need to run the daemon, CLI, or MCP server, install `cruxible` instead.

## Connect to an existing instance

Configure `CRUXIBLE_SERVER_BEARER_TOKEN` with the credential supplied by your
instance operator. Keep it out of source files and command output. The SDK uses
that credential for the explicit instance; it does not create a principal or
obtain approval authority by connecting.

```python
from pathlib import Path
from cruxible_client import Playbill

with Playbill.connect(
    target="http://localhost:8000",  # Or unix:/path/to/daemon.sock
    instance="your-instance-id",
    workspace=Path.cwd(),
) as pb:
    world = pb.world()
    print(world.coordinate)
```

Accepted reads on `pb` use the current head by default. Each request resolves
one coordinate on the daemon; pagination retains that coordinate. `pb.coordinate`
reports the last observed coordinate without performing I/O. Typed references
carry explicit coordinates and remain pinned. Mixed-coordinate Claim batches
refuse rather than silently moving a reference.

Use `snapshot = pb.at(coordinate)` (or `Playbill.connect(..., at=coordinate)`)
for a fixed context. `snapshot.world()` and its lazy reads stay at that coordinate.
`pb.world()` returns an independent snapshot that remains readable after the live
client advances. `refresh()` on a pinned context refreshes that same snapshot.
Borrowed contexts share the original connection's lifetime; closing one does not
close the original transport.

`pb.accept(proposal_id)` requests acceptance and returns its exact coordinate;
it does not export a workspace floor or approve the proposal. For exact readback,
even if another writer has advanced the head again:

```python
receipt = pb.accept(proposal_id)
if receipt.accepted_coordinate is not None:
    accepted = pb.at(receipt.accepted_coordinate)
    claims = accepted.claim_views(claim_ids)
```

Drafts retain their observed vocabulary/reference coordinate; admission still
checks current state and reports stale inputs. Operational queues, signing and
write admission retain their current authority/evidence checks; a pinned reading
context does not rewind operational state.

World attributes return live Claim contenders rather than silently selecting a
scalar. Use `world.prefetch(subjects=(...), predicates=(...))` for bounded reads
of known selections, then inspect each Claim's value and verdict. A returned
Claim's `subject` path can be passed directly to `pb.claim(subject=...)` or a
changeset Claim writer when revising it. Acceptance and
evidential support are distinct: an accepted Claim may remain unsupported under
its evidence policy.

File-backed authoring requires a declared `.playbill/sources.yaml` catalog in the
workspace. Supplying a body with `self_source` is an explicit self-assertion, not
an observation of an independent source. The SDK's `derived_by()` method currently
returns a typed unavailable refusal; it is not a supported derivation writer.

## Review and approve an exact candidate

Save `intent.intent_id` after preparation. After a process interruption, reopen
the daemon-owned work through the same instance and authenticated actor:

```python
intent = pb.resume_intent(saved_intent_id)
if intent.refused:
    print(intent.diagnostics)
proposal = intent.proposal  # Last observed proposal, without another HTTP call.
status = intent.status()    # Explicitly check the current lifecycle state.
```

Resuming reads the latest revision and persisted preflight diagnostics. It does
not prepare again, submit, approve, accept, or refresh the local floor. In
particular, recover an uncertain submission this way before deciding what to do
next. Python call-site locations and response-only lint warnings are not persisted
and are unavailable on the reopened handle. Review tokens are also process-local;
review the proposal again before signing in a new process.

Your operator supplies an `ApprovalSigner` capability, configured for an existing
accepted principal. The agent does not discover a key, select its own authority,
or send private key bytes to the daemon.

```python
proposal = pb.proposal(submitted.status().proposal_id)
reviewed = proposal.review()
review = reviewed.details  # Inspect all members, evidence, governance and provenance.
# After deciding to approve this exact candidate:
approval = proposal.approve(signer=configured_signer, reviewed=reviewed)
# After a separate decision to accept:
receipt = pb.accept(proposal.proposal_id)
world = pb.world()
```

`ReviewedProposal` is a process-local, originating-session/instance-bound snapshot.
`details` returns a fresh copy; editing it cannot alter the approved candidate.
The token binds identity, not proof that a human or agent actually read it.
The helper obtains fresh governance/challenge data, checks the reviewed candidate,
root and signer, verifies the local signature and submits it. It never accepts
or refreshes implicitly. The authenticated submitter may differ from the signer.

This convenience path requires a complete unredacted review. An
`ApprovalReviewMismatch` includes a repair instruction and never automatically
reviews or signs a replacement candidate. If the receipt check fails after
submission, inspect proposal status before retrying. Existing raw
`CruxibleClient` review/challenge/attestation APIs remain available for advanced
external signing and partial-visibility workflows under the server's policy.

For operator configuration, `LocalEd25519ApprovalSigner.open(...)` takes an explicit
principal ID, private key path, expected public key, and forbidden custody roots.
It preserves existing file permissions, nonsymlink/no-follow reads, and key checks
on every signature. The `ApprovalSigner` protocol is also the seam for a separately
provided signer backend; hardware and broker implementations are not included.

The [complete disposable example](examples/claim_review_repair.py) authors a
source-backed Claim, prompts for review and acceptance, reads its World state,
detects changed source evidence with free audit/`next`, and repairs the same Claim.
It needs an initialized disposable instance and operator-provisioned signer;
it does not bootstrap authority. World's qualified Claim IDs can be passed
directly to `revises` and `dispositions`; duplicate normalized keys are refused.

## Public contract snapshot

The public request/response contract is the set of Pydantic models and
`Literal` aliases in `cruxible_client.contracts`. Tests freeze that surface in
`tests/goldens/cruxible_client/contracts_snapshot.json`.

Breaking changes include removing a model or field, making an optional field
required, removing accepted enum/Literal values, or narrowing an accepted JSON
type. Additive optional fields, new models, and widened accepted values are
compatible, but still require snapshot review.

After an intentional contract change, regenerate the snapshot from the repo
root:

```bash
uv run python scripts/update_client_contract_snapshot.py
```

Raw dictionary response methods are not part of this frozen model contract
unless they are promoted to a model in `cruxible_client.contracts`.
