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
- shared public contracts, client-side error decoding, and Claim-attestation signing

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

Worlds are accepted-coordinate snapshots. `pb.accept(proposal_id)` requests
acceptance and returns its coordinate; it does not refresh the connection,
export a workspace floor, or approve the proposal. Call `pb.world()` to acquire
the current vocabulary snapshot, or `pb.refresh()` when you need full orientation.
Do not reuse old typed references after moving the connection's coordinate.

World attributes return live Claim contenders rather than silently selecting a
scalar. Use `world.prefetch(subjects=(...), predicates=(...))` for bounded reads
of known selections, then inspect each Claim's value and verdict. Acceptance and
evidential support are distinct: an accepted Claim may remain unsupported under
its evidence policy.

File-backed authoring requires a declared `.playbill/sources.yaml` catalog in the
workspace. Supplying a body with `self_source` is an explicit self-assertion, not
an observation of an independent source. The SDK's `derived_by()` method currently
returns a typed unavailable refusal; it is not a supported derivation writer.

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
