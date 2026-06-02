# cruxible-client

Typed HTTP client and public API contracts for talking to a governed Cruxible
daemon.

Install `cruxible-client` in agent environments that should only talk to a
separate Cruxible daemon over HTTP/MCP.

This package intentionally contains:

- the typed HTTP client
- shared public API request/response models
- client-side error decoding

It does not ship the daemon/runtime, graph/storage internals, workflow
executor, or MCP server implementation. Those stay in `cruxible-core`.

If you need to run the daemon, CLI, or MCP server, install `cruxible-core`
instead.

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
