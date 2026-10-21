---
name: adopt-kit
description: Start from a published Cruxible kit and fit it to a local use - preview and install it, review what it brings, adopt its example automation deliberately, add only the local vocabulary it lacks, and upgrade it later without losing local decisions.
---

# Adopt a Kit

Use this skill when a kit already models most of the user's domain and the
job is to install it and fit it locally, with as little local machinery as the
use case allows. A kit carries definitions and the providers its Procedures
run on; it never carries values, Lines, Triggers, mandates, principals or
policies. Those stay local.

The source for everything below is the [Kits](https://docs.cruxible.ai/kits/)
guide; read the kit's own README too, which says what the kit answers and how
to get a first answer.

## 1. Inspect before installing

```bash
cruxible kit status                                       # what is installed already
cruxible kit pull NAME:VERSION --out ./NAME               # fetch and verify, install nothing
cruxible kit add ghcr.io/cruxible-ai/kits/NAME@sha256:DIGEST   # preview; writes nothing
```

Install by the digest reference `kit pull` printed, so the bytes you
previewed are the bytes you install. Show the user the preview, especially
provider packages (installing them needs `admin`), any consequence on an
existing definition, and a `blocked` status.

## 2. Install

Commit exactly what the preview showed, with the command it printed (`kit add
… --commit --at OID`). `accepted` means done; `proposed` waits for approval
(`../_shared/references/governance-flow.md`); `awaiting_providers` waits for a
provider installation to be approved. Then read what arrived:

```bash
cruxible kit status
cruxible orient --section claim_types       # kit-owned rows end with kit=NAME
cruxible orient --section queries
```

For a Blueprint, choose a provider for each slot and instantiate it ([Kits:
Instantiate a Blueprint](https://docs.cruxible.ai/kits/#instantiate-a-blueprint)).

## 3. Adopt its automation deliberately

If the kit ships `examples/automation.json`, it's an inert starting point.
Copy it, change the Line names, sources, cron times (UTC) and mandate expiry
with the user, submit it, review the proposal, and enable each Line the user
wants running ([Lines and automation](https://docs.cruxible.ai/lines-and-automation/)).
Leave it at `propose` until the readings justify graduating.

## 4. Fit locally

Prefer what the kit provides, and add only what the use case lacks:

- **Values**: seed them with the value verbs, citing where they came from
  (`prepare-data` for messy inputs).
- **Vocabulary**: a field the kit lacks gets your own prefix, never the
  kit's (`create-state`, step 3).
- **Edits to kit definitions**: allowed, but every upgrade will ask about
  them. Edit only when the kit's meaning is wrong here, and record why.

If a change really belongs in the kit, tell the user it's upstream work.

## 5. Upgrade later

`kit add NAME:NEW-VERSION` previews the upgrade and names every definition
where the local version differs. Decide each with the user: keep a local
version with `--keep IDENTITY` (or `--keep-local-edits` for all of them), and
for a definition the release dropped that something still uses, keep it or
`--retire-dependents IDENTITY`. An upgrade that changes a Procedure brings its
Lines back disabled and settle mandates back to propose; `next` lists each.
[Evolving your vocabulary](https://docs.cruxible.ai/evolving-your-vocabulary/#upgrading-a-kit-youve-edited)
shows one.

## 6. Hand off

Follow `../_shared/references/governance-flow.md`, and tell the user which
definitions came from the kit, which are local additions, and which kit
definitions were edited or kept on purpose.
