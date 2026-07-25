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

## What Gets Verified On Pull, And How To Recover

A published bundle is a directory: `manifest.json`, `snapshot.json`,
`config.yaml`, `graph.json`, `cruxible.lock.yaml`, and `members.json`.
`members.json` pins the sha256 of every other member. Publishing writes it;
pulling verifies it, before the release is materialized and before any
`state pull apply` touches your graph.

A mismatch is always a refusal — never a warning, never a silent overwrite:

| Refusal | What it means | How to recover |
| --- | --- | --- |
| `failed digest verification for member '<name>'` | A bundle member's bytes are not what the publisher pinned. | Re-publish the release upstream under a **new** `--release-id`, then pull that. Never edit a pulled bundle in place. |
| `carries members that members.json does not pin` | The bundle gained a file after publication. | Re-publish upstream and pull again. |
| `Release '<id>' was already materialized ... but the transport now resolves that same release_id to <other digest>` | A release ID was rewritten upstream. Release IDs are immutable. | Publish the changed state under a **new** release ID and pull that. If the local copy is what drifted, delete `.cruxible/upstream/releases/<id>/` and pull again. |
| `no longer matches its recorded '<member>' digest` | The materialized upstream under `.cruxible/upstream/` was edited locally. | Restore the file from the published release, or re-create the overlay with `state create-overlay`. |

### Bundles published before `members.json` existed

`members.json` is a later addition, so older bundles do not carry it. Those
still verify `graph.json` and `cruxible.lock.yaml` against the digests
`snapshot.json` has always recorded — a mismatch there is a refusal exactly as
above. Only `config.yaml` has no pre-existing digest, so it is the one member
that cannot be verified in an old bundle. The pull reports this rather than
implying the bundle was checked: `state pull preview` returns a warning
(`predates per-member digests ... config.yaml could not be verified`), and
`state create-overlay` logs the same warning. Re-publish the release from a
current Cruxible to get full verification.

Verify-if-present, refuse-if-mismatch, warn-only-if-absent applies **only** to
pre-field bundles. Once a bundle carries `members.json`, every member is
verified and any mismatch refuses.

