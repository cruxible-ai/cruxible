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

### Publish the demo states to GHCR

Release managers publish both a dated immutable tag and the moving `latest`
tag. Authenticate ORAS with a GitHub token that can write packages for the
`cruxible-ai` organization; do not put the token on the command line:

```bash
export GHCR_USERNAME=<github-username>
read -rsp "GHCR token: " GHCR_TOKEN; echo
printf '%s' "$GHCR_TOKEN" | oras login ghcr.io \
  --username "$GHCR_USERNAME" \
  --password-stdin
unset GHCR_TOKEN

export RELEASE_ID=$(date -u +%Y-%m-%d)
```

The KEV publisher fetches fresh public data, runs the canonical build, and
uses the existing release-bundle publisher for both tags:

```bash
uv run python scripts/publish_kev_release.py \
  --release-id "$RELEASE_ID" \
  --transport-ref oci://ghcr.io/cruxible-ai/models/kev-reference
```

Publish the already-built banking Crux from its daemon instance with the
normal `state publish` path. The two commands intentionally publish the same
release bundle under the immutable and moving tags:

```bash
export CRUXIBLE_SERVER_URL=http://127.0.0.1:8100
export BANKING_INSTANCE_ID=<banking-crux-instance-id>

cruxible --instance-id "$BANKING_INSTANCE_ID" state publish \
  --transport-ref "oci://ghcr.io/cruxible-ai/models/banking-crux-demo:$RELEASE_ID" \
  --state-id banking-crux-demo \
  --release-id "$RELEASE_ID"

cruxible --instance-id "$BANKING_INSTANCE_ID" state publish \
  --transport-ref oci://ghcr.io/cruxible-ai/models/banking-crux-demo:latest \
  --state-id banking-crux-demo \
  --release-id "$RELEASE_ID"
```

Never reuse an immutable release tag for different state. After publishing,
verify that both repositories expose the expected dated and `latest` tags:

```bash
oras repo tags ghcr.io/cruxible-ai/models/kev-reference
oras repo tags ghcr.io/cruxible-ai/models/banking-crux-demo
```

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

### Integrity, not authenticity

Be precise about what these digests buy you. They establish **integrity**: the
bundle you materialize is byte-identical to the one whose manifest you read, and
nothing was added, removed, or edited between publication and use. They do not
establish **authenticity**: nothing here proves *who* published the bundle. A
party who can rewrite the whole directory in transit can rewrite the manifest
along with it, and every digest will agree.

What closes that gap is signing, and signing is future work. Until it lands:

- The transport is the trust boundary. Use one you control or one that
  authenticates the publisher (a private registry, an authenticated URL, a
  filesystem only you can write).
- The first pull is trust-on-first-use. After it, the release ID and the
  materialized upstream are both pinned, so *later* substitutions are caught —
  but the first fetch is taken on the transport's word.

### The member contract is non-downgradable

`manifest.json` carries two fields that describe the bundle's own verification:
`bundle_format_version` (this bundle was published with `members.json`) and
`members_digest` (the sidecar body the manifest vouches for, covering every
member except the manifest itself — the manifest cannot pin its own final
bytes).

They exist so the weaker legacy path cannot be *chosen*. Deleting `members.json`
from a current bundle used to buy the deleter the pre-sidecar rules; now the
manifest still declares the sidecar, so its absence is a refusal. Replacing the
sidecar wholesale is caught by `members_digest`.

A mismatch is always a refusal — never a warning, never a silent overwrite:

| Refusal | What it means | How to recover |
| --- | --- | --- |
| `failed digest verification for member '<name>'` | A bundle member's bytes are not what the publisher pinned. | Re-publish the release upstream under a **new** `--release-id`, then pull that. Never edit a pulled bundle in place. |
| `carries members that members.json does not pin` | The bundle gained a file after publication. | Re-publish upstream and pull again. |
| `declares bundle_format_version <n> ... but that file is absent` | The per-member digest sidecar was stripped from a bundle that was published with one. | Re-publish upstream and pull again. Do not hand-delete `members.json`. |
| `has a members.json that its manifest does not vouch for` | The sidecar was replaced after publication. | Re-publish upstream and pull again. |
| `declares bundle_format_version <n>, but this Cruxible understands at most <m>` | The bundle came from a newer Cruxible. | Upgrade Cruxible, then pull again. |
| `Release '<id>' was already materialized ... but the transport now resolves that same release_id to <other digest>` | A release ID was rewritten upstream. Release IDs are immutable. | Publish the changed state under a **new** release ID and pull that. If the local copy is what drifted, delete `.cruxible/upstream/releases/<id>/` and pull again. |
| `no longer matches its recorded '<member>' digest` | The materialized upstream under `.cruxible/upstream/` was edited locally. | Restore the file from the published release, or re-create the overlay with `state create-overlay`. |

### The materialized upstream is verified on every read, not just on pull

`.cruxible/upstream/current/` is pulled state. `manifest.json`, `graph.json`,
`config.yaml`, and `cruxible.lock.yaml` are each pinned in the overlay's
tracking metadata when the pull records them, and each is verified immediately
before it is consumed — by `state pull preview`, by `state pull apply`, by
`config reload` (which composes the active config *by extending* the upstream
config), and by ownership resolution (which decides which types the overlay may
write). Editing a file under `.cruxible/upstream/` therefore does not quietly
re-scope anything; it makes the next operation that reads it refuse, with the
member name, both digests, and the recovery.

### Refusals leave the target untouched

Verification runs before anything is written. A refused `state create-overlay`
leaves no instance root behind; a refused `state pull apply` leaves the live
graph and the active config exactly as they were, with the previous release
still tracked. There is no partial-apply state to clean up and no flag that
proceeds past a failed verification.

### Bundles published before `members.json` existed

`members.json` and the manifest fields that declare it are later additions, so
older bundles carry neither. Those still verify `graph.json` and
`cruxible.lock.yaml` against the digests `snapshot.json` has always recorded — a
mismatch there is a refusal exactly as above. Only `config.yaml` has no
pre-existing digest, so it is the one member that cannot be verified in an old
bundle. The pull reports this rather than implying the bundle was checked:
`state pull preview` and `state create-overlay` both return a warning
(`predates per-member digests ... config.yaml could not be verified`), and the
CLI prints it. Re-publish the release from a current Cruxible to get full
verification.

Verify-if-present, refuse-if-mismatch, warn-only-if-absent applies **only** to
bundles whose manifest declares no `bundle_format_version`. Once a bundle
declares one, every member is verified and any mismatch refuses.
