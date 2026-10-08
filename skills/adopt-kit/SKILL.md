---
name: adopt-kit
description: Start from a published Cruxible kit and fit it to a local use - preview and install it, review what it brings, instantiate its Blueprints with local providers, add only the local vocabulary and data it lacks, and upgrade it later without losing local decisions.
---

# Adopt a Kit

Use this skill when a kit already models most of the user's domain and the job
is to install it and fit it locally, with as little local machinery as the use
case allows.

This skill is for:

- installing a kit and understanding what it brought
- choosing providers for a kit's Blueprints
- adding the local vocabulary, values and Lines the kit leaves to the consumer
- upgrading a kit later while keeping deliberate local choices

A kit carries definitions (ClaimTypes, CaptureContracts, named queries,
Procedures, Blueprints, ProviderInterfaces, acquisition policies) and the
provider packages its own Procedures run on. It never carries values, Lines,
Triggers, mandates, principals or policies: those are local. See
[Kits](../../docs/kits.md).

## Phase 1: Inspect before installing

```bash
cruxible kit status                                # what is installed already
cruxible kit pull NAME:VERSION --out ./NAME        # fetch and verify, install nothing
cruxible kit add ghcr.io/cruxible-ai/kits/NAME@sha256:DIGEST   # preview
```

The preview writes nothing. It lists the plan by kind (each definition's
action, consequence and dependent count), the provider packages it would
install, the release's claimed build provenance, and ends with the exact commit
command. Install by the digest reference `kit pull` printed, so the bytes you
previewed are the bytes you install.

Show the user the preview. Check especially:

- provider packages (installing them needs `admin`);
- any consequence on an existing definition (`overwrites your edit`,
  `re-adds a definition you retired`, `takes over a definition you defined
  outside the kit`);
- a `blocked` status: overlapping owned prefixes with another kit, a
  definition another kit owns, another build of a bundled provider, a
  downgrade.

## Phase 2: Install

```bash
cruxible kit add ghcr.io/cruxible-ai/kits/NAME@sha256:DIGEST --commit --at OID
```

| Result | Next |
|---|---|
| `accepted` | Installed. |
| `proposed` | Approve and activate per `../_shared/references/governance-flow.md`. |
| `awaiting_providers` | Approve and activate the provider installation the result names, then run `kit add` again. |
| `blocked` | Read the detail; fix the cause or choose another release. |

Then read what arrived:

```bash
cruxible kit status
cruxible orient
cruxible orient --section claim_types
cruxible orient --section procedures
cruxible orient --section queries
```

## Phase 3: Choose providers for Blueprints

A Blueprint is a Procedure skeleton with provider slots the consumer fills. For
each one the user needs:

```bash
cruxible get Blueprint:NAME          # each slot's interface, and installed providers that fit
cruxible provider install PACKAGE    # when nothing fits (admin); e.g. cruxible-provider-web
cruxible authoring example blueprint-instance
```

Bind one provider per slot, check, then submit:

```bash
cruxible authoring submit - --dry-run <<'EOF'
{"kind": "blueprint_instance", "name": "LOCAL-NAME", "blueprint": "NAME",
 "bindings": {"SLOT": "PROVIDER"}}
EOF
```

Add `"acquisition_policy": "POLICY"` when the Procedure reads sources. The
result is an ordinary Procedure: run it once with `cruxible procedure run`, or
put it on a Line with `automate-with-procedures`.

## Phase 4: Fit locally

Prefer what the kit provides. Add only what the use case needs and the kit
lacks:

- **Values**: seed them with the value verbs (`cruxible write -`), citing
  catalogued files where they came from. Use `prepare-data` for messy inputs.
- **Vocabulary**: a field the kit lacks gets a ClaimType under your own prefix,
  not the kit's (the kit owns its prefixes, and a definition under them would be
  taken over on upgrade). Follow `create-state` Phase 2.
- **Edits to kit definitions**: allowed, but every upgrade will ask about them.
  Prefer a local addition; edit a kit definition only when the kit's meaning is
  wrong for this instance, and record why.
- **Automation**: Lines, Triggers and mandates over the kit's Procedures are
  local; see `automate-with-procedures`.

If a change really belongs in the kit, say so to the user as upstream work.

## Phase 5: Upgrade later

```bash
cruxible kit status                      # "latest X available" for registry kits
cruxible kit add NAME:NEW-VERSION        # preview
```

An upgrade always proposes, and the release's version wins by default. For each
consequence in the preview, decide with the user:

- keep a local version: `--keep ClaimType:PREFIX.FIELD` (repeatable), or
  `--keep-local-edits` for every definition edited since install; the kit's
  record remembers the decision for later upgrades;
- a definition the release dropped that something still depends on: keep it
  (`--keep IDENTITY`) or retire it with its dependents
  (`--retire-dependents IDENTITY`);
- an older release: refused unless `--allow-downgrade`.

Dependents of replaced definitions are carried forward in the same change set.
Commit with `--commit --at OID` once the preview is right.

## Phase 6: Hand off

Follow `../_shared/references/governance-flow.md` for the approval policy,
agent principals and the final checks, and tell the user clearly which
definitions came from the kit, which are local additions, and which kit
definitions were edited or kept on purpose.
