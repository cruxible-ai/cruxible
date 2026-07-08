# Publishing And Subscribing To States

A reference state is a published, versioned release of an instance's state
that other instances subscribe to and track. Cruxible publishes the KEV
reference this way (consume it via the [KEV Guide](kev-guide.md)); this page
is the generic mechanism — for building a reference locally, publishing your
own releases, and subscribing an overlay instance to any published state.

Everything here runs against a local daemon from the
[Quickstart](quickstart.md) setup.

## Build A Reference State Locally

The KEV reference kit is the worked example: it builds the public reference
graph from the pinned CISA/NVD/EPSS snapshot in the kit's `data/`.

Initialize the standalone KEV reference kit. This materializes the kit bundle,
loads its config, and gives you an instance ID.

```bash
cruxible --server-url http://127.0.0.1:8100 init --kit kev-reference
```

Keep the returned `instance_id`; every server-backed command after init uses it.
Kit init installs the kit's pinned workflow lock automatically, so you can
preview the canonical reference refresh right away:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> run \
  --workflow build_public_kev_reference \
  --save-preview kev-reference-preview.json
```

Canonical workflows preview state first. Apply the preview only after checking
the `apply_digest`, changed counts, receipt ID, and trace IDs:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> apply \
  --preview-file kev-reference-preview.json
```

Run a query and inspect its receipt:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> query run \
  vulnerability_products \
  --param cve_id=CVE-2020-1472
```

Every query returns a receipt ID. In MCP, fetch the full proof with
`cruxible_receipt(instance_id, "<receipt-id>")`. The CLI `explain` command
renders receipts in both server and direct-local modes.

## Publish, Then Subscribe An Overlay

An overlay kit composes local state and workflows over a published upstream.
The KEV triage kit is the worked example: it tracks the KEV reference and
adds local assets, services, controls, and governed proposal workflows.

One extra prerequisite for the `--state-ref` path: the
[oras](https://oras.land/docs/installation) CLI (`brew install oras` on
macOS). The state catalog resolves `--state-ref` aliases to OCI refs, and
the OCI transport shells out to `oras`. The `file://` path below needs no
extra tooling.

```bash
cruxible --server-url http://127.0.0.1:8100 state create-overlay \
  --state-ref kev-reference \
  --kit kev-triage \
  --root-dir "$PWD/kev-triage-workspace"
```

`--state-ref kev-reference` resolves through the published state catalog. In
a source checkout before published OCI reference states are available (or
without `oras`), publish the reference instance you built above to a local
`file://` transport and pass `--transport-ref` instead of `--state-ref`:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> state publish \
  --transport-ref "file://$PWD/releases/kev-reference/v1" \
  --state-id kev-reference \
  --release-id v1

cruxible --server-url http://127.0.0.1:8100 state create-overlay \
  --transport-ref "file://$PWD/releases/kev-reference/v1" \
  --kit kev-triage \
  --root-dir "$PWD/kev-triage-workspace"
```

`file://` refs must be absolute paths, and publish refuses a target that
already exists — pick a new release directory per publish.

The command returns a new overlay `instance_id` and locks the overlay as part
of creation. Preview the local canonical state refresh and apply it:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> run \
  --workflow build_local_state \
  --save-preview kev-local-preview.json
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> apply \
  --preview-file kev-local-preview.json
```

Run a governed proposal workflow and inspect the pending group:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> propose \
  --workflow propose_asset_products

cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> group list \
  --status pending_review
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> group get \
  --group <group-id>
```

Approve or reject only after reviewing the group thesis, member signals,
receipt, trace IDs, and pending version:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> group resolve \
  --group <group-id> \
  --action approve \
  --expected-pending-version <pending-version> \
  --rationale "Reviewed source evidence and accepted the proposed mappings"
```

