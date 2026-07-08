# KEV: Vulnerability Triage On Hard State

The KEV pair turns CISA's Known Exploited Vulnerabilities catalog into a
governed triage brain over **your** assets. Two kits, two roles:

- **kev-reference** is the published layer: KEV + NVD CPE + EPSS, built into
  a typed graph (vulnerabilities, products, vendors, affected-by edges) and
  released as a versioned state you subscribe to. You do not build it.
- **kev-triage** is your layer: assets, services, owners, controls,
  exceptions, patch windows — and the governed judgments that connect them
  to the reference (which assets run which products, which exposures are
  material).

The output is a standing work queue —
`asset_vulnerability_postures_requiring_action` — where every row is a
judgment-admitted exposure with evidence, ordered by priority and KEV due
date, and every answer carries a receipt.

## 1. Subscribe to the reference

Prerequisite: the [oras](https://oras.land/docs/installation) CLI for the
OCI transport (`brew install oras` on macOS).

```bash
pip install cruxible
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/kev" cruxible server start   # shell 1

# shell 2 — creates a local overlay instance tracking the published reference
cruxible --server-url http://127.0.0.1:8100 state create-overlay \
  --state-ref kev-reference \
  --kit kev-triage \
  --root-dir "$PWD/kev-workspace"
cruxible context connect --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id>
```

## 2. Load your inventory

The kit ships seed data as a worked example; real use replaces it with your
asset, service, owner, and control exports. Point an agent at the
[`kev-start`](https://github.com/cruxible-ai/cruxible/tree/main/kits/kev-triage/skills/kev-start)
skill to adapt the kit to your data, then build:

```bash
cruxible run --workflow build_local_state --save-preview kev-local.json
cruxible apply --preview-file kev-local.json
```

## 3. Judge the mappings

Asset-to-product mappings are governed: the workflow can only propose, and
the proposals wait for review with their matching signals and evidence:

```bash
cruxible propose --workflow propose_asset_products
cruxible group list --status pending_review
cruxible group get --group <group-id>
cruxible group resolve --group <group-id> --action approve \
  --rationale "Verified against the CMDB export" \
  --expected-pending-version <n>
```

With mappings approved, propose and review exposure postures the same way
(`propose_asset_exposure`), then work the queue:

```bash
cruxible query run asset_vulnerability_postures_requiring_action --json
```

## 4. Stay current

The reference republishes as new releases land. Updates are preview-first,
like everything else:

```bash
cruxible state status
cruxible state pull-preview
cruxible state pull-apply
```

Your local judgments, exceptions, and controls persist across pulls; only
the upstream reference layer moves.

## Building or publishing your own reference

`init --kit kev-reference` plus the `build_public_kev_reference` workflow
builds the reference locally from the kit's pinned data snapshot — for
offline use, demos, or publishing your own release. The
[Quickstart](quickstart.md) walks that path, including publishing to a
`file://` transport.
