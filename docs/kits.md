# Kits

A kit is a starting point. One instance exports it and another installs it:
vocabulary, named queries, Procedures, guidance Documents, the provider
packages those Procedures need, reference pages, and an automation template
you tailor. It is
not a schema you have to adopt. Every garage that starts from a car parts kit
begins with the same parts, vehicles and fitments, so the same questions and
automation work on day one. From there the instance is yours to grow: revise
the kit's definitions, add your own, and adapt or replace its automation, all
by ordinary authoring. `kit add` previews first and writes nothing. Its
commit proceeds in three phases, each change accepted under the instance's
approval policy: when bundled providers are missing, first one change with the
Contracts their operations use, then a separate registration change for each
missing provider, then one change with the kit's definitions.
An earlier change can be accepted before a later one stops: an
`awaiting_providers` or `blocked` result means the definitions are not in yet,
not that nothing changed. An upgrade names every definition you have edited; the release's
version replaces your edit unless you keep it.

This page covers using kits, the automation a kit suggests, and building and
documenting kits. Command details are in the [CLI reference](cli-reference.md#kit).

## A worked example: installing car parts

The car parts kit is published as `car-parts`. `kit add` previews first and
writes nothing:

<!-- publish: fill the car-parts digests -->
~~~text
$ cruxible kit add car-parts:1.0.0
car-parts 1.0.0: would_propose (install)
Built by … (claimed)
CaptureContract:
  add: CaptureContract:cruxible.coordinator-self-source-v1
ClaimType:
  add: ClaimType:catalog.decision.amends
  ...
  add: ClaimType:catalog.part.title
  ...
QueryDefinition:
  add: QueryDefinition:catalog.parts_for_vehicle
SubjectKind:
  add: SubjectKind:catalog.decision
  add: SubjectKind:catalog.note
  add: SubjectKind:catalog.part
  add: SubjectKind:catalog.vehicle
Preview at 8a8fa804bbb3; nothing was written.
Commit it: rerun the same command with --commit --at 8a8fa804bbb3
~~~

Committing at the previewed coordinate lands exactly what you saw, or refuses
if the instance moved in between:

~~~text
$ cruxible kit add car-parts:1.0.0 --commit --at 8a8fa804bbb3
car-parts 1.0.0: accepted (install)
...

$ cruxible kit status
car-parts 1.0.0 sha256:3d992c39…; …
  source: ghcr.io/cruxible-ai/kits/car-parts@sha256:<car-parts 1.0.0 digest>
  built by … (claimed)
~~~

Reads say who owns each definition: `orient --section claim_types` ends each
row the kit owns with `kit=car-parts`. From here the vocabulary is yours to
use and to extend: write Claims with it, add your own fields and queries
beside it, and edit its definitions if you need to. A newer release arrives
the same way, and [Evolving your
vocabulary](evolving-your-vocabulary.md#upgrading-a-kit-youve-edited) shows an
upgrade over a local edit.

## What a kit carries

A kit carries definitions from seven families:

| Family | What it is |
|---|---|
| ClaimTypes | Vocabulary: what a field may hold and what evidence backs it |
| SubjectKinds | How a kind's Subjects are keyed; travels with its key ClaimTypes |
| CaptureContracts | What a retained piece of evidence must look like |
| Contracts | Immutable data shapes that Procedures and provider operations pin |
| QueryDefinitions | Named queries |
| Procedures | Runnable graphs over provider calls |
| Documents | Governed Documents, such as guidance linked by `guides` |

Beside the definitions, a release can carry inert files for its reader: its
README, other reference pages, `examples/` and agent skills under
`skills/<name>/`. The manifest lists each one with its sha256, but they are
not part of the release's content digest, so a docs-only fix leaves every
definition digest and every install receipt as it was (the image digest still
moves, so a published version takes a patch bump). `kit add` never sends them
to the daemon; `kit pull` writes them into the kit directory at their own
paths.

A kit never carries state or authority: no Subjects or Claims, no Lines or
Triggers, no mandates, no principals, no approval policy. Those belong to the
instance that installs the kit. A Line that should run a kit's Procedure is
authored and enabled by the consumer.

A kit names the provider packages its definitions need in one of two ways:

- **Bundled**: a kit's own domain code (for example a parser for one domain's
  feed) travels inside the kit as the package's built wheel and lock file,
  never as source. Installing the kit installs the package first, and the
  package's operations serve the kit's Procedure slots.
- **Default**: a published, general-purpose package (for example
  `cruxible-provider-web` for `web.fetch`) is named by package and exact
  version, and the kit carries none of its files. Installing the kit installs
  it by name from the provider index, only when builds already installed do
  not serve every slot the kit's Procedures declare.

## Procedures with Provider slots

A Procedure never names a provider. Each provider call names a slot, and the
Procedure declares each slot's signature: its input Contract, its output
Contract and its effect (`pure`, `external_read` or `external_mutation`). Any
installed provider build with an operation of exactly that signature fits the
slot, so a kit's Procedures leave the consumer free to choose which search
service, converter or other implementation runs them.

A Line binds every slot to one exact build and operation when it is accepted,
and its runs never rebind. A direct run fits each slot it is not told about to
the single live operation serving its signature; `--bind slot=provider`
chooses one. The run records the bound builds for replay; the accepted
Procedure stays unchanged.

Contracts are shared definitions: builds whose operations name the same
Contracts serve the same slots. A provider is pure computation behind its
Contracts: it receives every input its operation's input Contract admits, and
refuses, typed, what it cannot serve.

## Providers you already have

- **Built in.** Every new instance starts with the built-in `workspace.file`
  operation and its core-owned Contracts, which turn an authorized workspace
  file read into evidence. Nothing to install; a slot with its signature binds
  to it.
- **Published by name.** General-purpose providers install by package name:
  from the operator's provider repository when one is configured, otherwise
  from the provider index (PyPI unless the operator names another).

  ~~~bash
  cruxible provider list                     # what a configured repository offers
  cruxible provider install cruxible-provider-web
  ~~~

  `cruxible-provider-web` implements `web.fetch`, `search.web` and
  `web.render`. `web.fetch` returns the body the origin sent as its Capture,
  verbatim, with the URL, final URL, status, media type, request digest and
  kept headers in the Capture's coordinate; `search.web` likewise returns the
  search instance's response body. `web.render` reads a fetched Capture and
  returns a derived Capture citing it (the page assembled in a browser, or its
  main content as Markdown). `web.fetch`
  refuses loopback, private, link-local (cloud metadata included), CGNAT,
  multicast and reserved addresses, on the first request and on every
  redirect, and connects only to the address it checked, so DNS rebinding
  cannot reach behind it. It needs direct outbound access and fetches pages as
  served.
  Any package whose operation serves the same `web.fetch` Contracts fits the
  same slots.
- **From a kit.** A kit's own domain provider arrives bundled in the kit; a
  kit's default provider is installed by name when the kit is added and the
  kit's slots are not already served here.

Installing a provider needs `admin` permission. It registers the build and
its operations as a change, which lands at once when the approval
policy requires no approval and otherwise stops at a proposal to approve and
activate. Installing grants no permission to run anything.

## Use a kit

### Find and inspect

A kit comes from a registry reference, a kit directory, or an OCI image
layout. A bare name resolves under `ghcr.io/cruxible-ai/kits`, so `car-parts:1.0.0`
means `ghcr.io/cruxible-ai/kits/car-parts:1.0.0`.

~~~bash
cruxible kit pull car-parts:1.0.0 --out ./car-parts-1.0.0     # fetch and verify, install nothing
~~~

`kit pull` prints the digest-pinned reference it fetched. Installing by that
digest reference installs exactly those bytes even if the tag later moves.

### Install

`kit add` previews by default and writes nothing:

~~~bash
cruxible kit add ghcr.io/cruxible-ai/kits/car-parts@sha256:<digest>
~~~

The preview lists the plan grouped by kind (what each definition does and
how many dependents it has), each provider package and what happens to it
(already installed, `satisfied` when builds installed here already serve
every slot the kit's Procedures declare, or to be installed), and who built the release, from
which instance and at which coordinate. That build
record is claimed by the builder, not proven. The preview ends with the
command that commits exactly what you saw:

~~~bash
cruxible kit add ghcr.io/cruxible-ai/kits/car-parts@sha256:<digest> --commit --at <oid>
~~~

The result is one of:

| Status | Meaning | Next |
|---|---|---|
| `accepted` | The change set landed: the approval policy required no approval. | Nothing. |
| `proposed` | It awaits approval. | `cruxible proposal approve`; the approval that completes it activates it. |
| `awaiting_providers` | A provider installation the kit needs awaits approval. | Approve that installation; its activation proposes the definitions on their own, as you (`kit status` shows the resume). |
| `blocked` | Something in current state stops it; the detail says what. | Fix the cause, or choose another release. |

Installing the provider packages a kit needs requires `admin` permission. A
`governed_write` caller can add a kit whose providers are already installed or
satisfied.

Your own definitions under a kit's prefix are not an ownership collision. A
different definition appears as `takes_over_outside_definition` in the plan.
Next: pass `--keep IDENTITY` to keep your version.

Two installed kits cannot own overlapping prefixes, and a kit cannot replace a
definition another kit still holds, even outside that kit's current prefixes.
These are refusals (`cruxible.kit.ownership_conflict`). Prefix overlap remains
a hard block even with `--keep`. The result's `refusals` name our prefixes,
the owning kit, and its prefix or held identity, with a repair.
Next: remove the owning kit first with `cruxible kit remove KIT`, or install a
release that does not own the overlapping prefix or replace the held definition.

Reusing an installed `kit_id` with wholly disjoint owned prefixes is refused
(`cruxible.kit.id_reused`), rather than planned as an upgrade. The refusal names
both prefix sets. Releases with the same prefixes, or with a shared parent or
sub-prefix, can still upgrade normally.
Next: remove the installed kit first with `cruxible kit remove KIT`, then add
the other kit.

A default provider is installed at exactly the version the kit names, and only
as the build the kit recorded: the provider index must list the recorded wheel
hash before the wheel is fetched, and the wheel's lock must match before any
dependency is fetched. Another build under the same name and version is
refused (`cruxible.provider.index_build_differs`) with nothing installed. A
default is skipped (`satisfied`) when live builds of other providers here
already serve every slot signature the kit's Procedures declare; those slots
then bind to them. A daemon configured with a
provider repository installs by name only from that repository, so there a
default that is not satisfied is refused: install an implementation yourself
and add the kit again.

### Run a slotted Procedure

`get` on a Procedure renders its declared slots and their signatures:

~~~bash
cruxible get Procedure:catalog.price-check
~~~

A direct run fits each slot to the single live operation serving its
signature. No fit refuses `cruxible.line.slot_unfilled`; several fits refuse
`cruxible.line.slot_ambiguous` and list the candidates. Select explicitly with
`cruxible procedure run NAME INPUT --bind slot=provider` (`provider[/build][#operation]`;
a bare name is its most recently registered live build), or pass
`bindings={"slot": "provider"}` to the SDK's `Procedure.run`. A Line records an
exact build and operation for every slot in its `bindings`, alongside its
Procedure and parameters.


### Check and upgrade

~~~bash
cruxible kit status
~~~

`kit status` lists each installed kit with its version, the definitions
edited here since install, the ones kept on purpose, its provider packages
with their delivery and state (`installed`, `differs`, `satisfied` or
`missing`),
and, for a kit installed from a registry, the newest release available (it
lists the repository's tags; `--offline` skips that). A kit installed from a
directory or layout reports a local source.

Installing a newer release is the same `kit add` with the new version. An
upgrade always proposes, and the release's version wins by default. The
preview names what each replacement does:

- it overwrites an edit you made since install;
- it re-adds a definition you retired;
- it takes over a definition you defined outside the kit;
- it replaces a definition the kit carried in as a dependency.

To keep your version instead, name it with `--keep` (repeatable, by identity
such as `--keep ClaimType:catalog.part.title`), or keep every definition you
edited since install with `--keep-local-edits`. The kit's record notes what
you kept on purpose, so later upgrades do not ask again while the release's
version is unchanged.

A kit add installs the builds the release ships and moves every live Line bound
to another build of those provider names, including Lines over your own
Procedures, to the release's exact builds in the definitions change set. A
Procedure names no build, so only Lines move. The preview lists each re-pin and
every carried Line (disabled) and every Mandate losing settle. A build change
or Procedure-definition change carries same-identity Lines to the final Procedure
version and same-name Mandates to propose-only grants, with settle fields cleared.
Triggers stay byte-identical. `next` names each disabled upgrade; use the existing
`line enable` to resume its cadence or events, then explicitly author and approve
a settle re-grant if needed. Normal kit approval policy applies. Subject-creating
settle workflows retain their namespace: proposing Subjects needs explicit
namespace authoring. Retiring a Procedure still retires its automation. To keep a Line on its current builds,
pass `--keep-build Line:NAME` (repeatable). Lines bound to other providers
are unaffected. Old builds stay installed and runnable; kit add neither retires
nor lists them.

Provider builds have identities `Provider:NAME/BUILD`, where BUILD is a hash of
the wheel and lock or image, excluding the host environment. Contracts are shared:
builds whose operations name the same Contracts serve the same slots. Installing a
build adds it alongside earlier builds; existing exact pins stay put. New
authoring by bare provider name chooses the live build registered most recently,
even when its package version is lower. Signing-key and capture-declaration changes
do not change registration order. Retiring the newest build makes the previous
live build the default; builds registered in the same generation require an
explicit build name until the tie is resolved. Explicit build names select that build.
These rules also apply to retained single-path Providers: changing signing keys
or Capture declarations preserves their runtime build's original registration order.
Procedures name no builds, so moving a provider moves Lines:
`cruxible provider move NAME --to newest|BUILD` rebinds the Lines bound to NAME
(or only those named with `--only`) as a governed change with its own preview,
carrying them disabled and their Mandates propose-only. `cruxible provider retire NAME/BUILD` is refused
while live definitions pin it and retains its runtime environment after retirement.
Legacy single-path Providers remain readable without a rewrite; `cruxible-builtin`
keeps its single path.

A definition the release dropped is retired when nothing depends on it. When
something does, it stays live until you decide: `--keep IDENTITY` keeps it,
and `--retire-dependents IDENTITY` retires it together with its dependents.
The preview counts dependents after the release's replacements: a query that
the same release replaces and that no longer uses the dropped type does not count.
Definitions the release replaces carry their dependents (such as Claims and
Procedures) forward in the same change set.
For a carried Claim, `get --detail evidence` and the floor's `changes/` file
show the kit transition as the rationale for that revision.

Reads name the owner: `cruxible orient --section claim_types` and `--section
subject_kinds` mark each row an installed kit owns with `kit`, and so do the
`get ClaimType:` and `get SubjectKind:` cards. A definition the kit only
carried, or one you authored yourself, names none.

`kit add` refuses a release older than the one installed unless you pass
`--allow-downgrade`; the preview names the transition (install, upgrade,
downgrade or reinstall).

### Remove

~~~bash
cruxible kit remove car-parts                 # preview
cruxible kit remove car-parts --commit --at <oid>
~~~

Removing retires the definitions the kit owns. It does not retire what the kit
carried in from elsewhere, nor its provider packages. A definition you edited
since install must be reverted or retired on its own first, and removal is
refused while live Claims or other definitions still depend on what it would
retire. Like `kit add`, it lands at once when the approval policy requires no
approval and otherwise stops at a proposal.

## Automation that comes with a kit

A kit never installs Lines, Triggers or mandates: what runs on your instance,
when, and with how much authority is yours to decide. A kit that has
automation to suggest ships it as one **automation template**: a change set of
Lines, Triggers, mandates and Runbooks, at the path its `cruxible-kit.json`
names in `automation_template` (the security kit's is `automation.json`).
`kit add` and an upgrade never install or submit it; `kit add` and `kit pull`
name it. The kit's build checked that it authors cleanly against the kit's own
definitions. The template includes a start Runbook naming its Lines in the
order their reads and writes imply (the security kit's is `security.start`).

The standard start:

1. **Tailor** the template: the Line names (and the start Runbook's steps with
   them), the sources each Line reads, the cron times (UTC), and each
   mandate's `valid_from` and `expires_at`.
2. **Submit** it (`cruxible authoring submit automation.json`) and **approve**
   the proposal under your approval policy. A Runbook may name Lines the same
   change set adds, and a Trigger may aim at a Runbook it adds.
3. **Preview** the start: `cruxible runbook preview security.start` shows, per
   step, what it reads and writes, what its mandates settle and what would go
   to review, and any step that reads what only a later step writes.
4. **Run** it: `cruxible runbook run security.start`. Steps run in order; a step
   whose writes go to review stops the run, so approve that proposal and run it
   again.

Then enable each Line you want running on its schedule (`cruxible line enable
NAME`). The kit's README says which Lines settle what and why
([Lines and automation](lines-and-automation.md#earning-settle)).

## Build a kit

A kit is exported from an instance where its definitions are accepted. You
name the identity prefixes the kit owns; every live definition under them, in
the six families above, is exported together with everything those
definitions pin.

~~~bash
cruxible kit build --id car-parts --version 1.0.0 --owns catalog. --out ./car-parts-1.0.0
~~~

- `--owns` is repeatable and each prefix ends in a dot (`catalog.`,
  `catalog.pricing.`). Two installed kits may not own overlapping prefixes.
- `--version` is `MAJOR.MINOR.PATCH`. A new release needs a higher version.
- The release is a snapshot: each definition is exported without its local
  history, so the consumer's instance gets its own.
- The built-in `workspace.file` provider is pinned as it is and never
  exported.
- The manifest records who built the release, on which instance and at which
  accepted coordinate.
- `--automation-template FILE` names the kit's automation template (it sits at
  the kit's root under its own file name). The build refuses
  (`cruxible.kit.automation_template_invalid`, naming the template and its first
  diagnostic) unless it authors cleanly against this instance's accepted
  definitions. The check is an authoring preview: nothing is stored. The
  template travels as one of the kit's files, outside the release's content
  digest, and is never installed.

### Bundle your own provider

A kit's own domain provider travels bundled. Bundling takes the provider's package directory, which holds its
`pyproject.toml`, its `uv.lock`, and in `dist/` the built wheel plus a wheel
for each dependency the lock names by path:

~~~bash
cruxible provider install ./catalog-parser/dist/catalog_parser-1.0.0-py3-none-any.whl \
  --lock ./catalog-parser/uv.lock
# author and accept the Procedures whose slots it serves, then:
cruxible kit build --id car-parts --version 1.0.0 --owns catalog. \
  --provider ./catalog-parser --out ./car-parts-1.0.0
~~~

The build checks that what you bundle is exactly what this instance
installed and that every Provider a carried definition pins is bundled. A
changed wheel needs a new package version, since an instance holding the old build
refuses a different build under the same version. Registry dependencies (for
example `cruxible-provider-runtime`) are not bundled; the consumer's daemon
resolves them from its provider index.

### Name a default provider

For general-purpose operations such as `web.fetch`, name a published package
as the kit's default instead of bundling it. Lay its directory out as for
`--provider`: a `pyproject.toml` naming the package, the lock the published
wheel embeds as `uv.lock` (`cruxible.kit.provider_lock_not_embedded`
otherwise), and in `dist/` the published wheel plus the wheel of each
dependency that lock names by path (read to check the build, not carried).
Install that same build here, and name it:

~~~bash
cruxible provider install cruxible-provider-web
cruxible kit build --id car-parts --version 1.1.0 --owns catalog. \
  --default-provider ./cruxible-provider-web --out ./car-parts-1.1.0
~~~

The manifest records the package, its exact version and the wheel and lock
digests (`delivery: index`), and the kit carries none of its files. Every
Provider a carried definition pins must be named by `--provider` or
`--default-provider`, as the build this instance installed. `kit add` installs
the default unless builds already here serve every slot the kit's Procedures
declare. A Line binds each slot to an exact build when accepted; a direct run
fits each slot, and more than one fitting operation requires an explicit
binding.

### Document it

A kit's users read its README first, so the README carries what someone needs
to decide whether to install it and to get a first answer. By convention it
covers, in order:

- **What it is**: what the kit models, in a paragraph.
- **What it answers**: the questions it can answer, each with the saved query
  that answers it (`cruxible query --name NAME`).
- **How it models your world**: its kinds and how they relate, ideally as a
  diagram, with which values are observed and which are judged.
- **How it runs on its own**: the example change set, the Lines and Triggers
  it authors, and how its Lines graduate. It may come before the install
  steps, as the security kit's does, since those steps adopt it.
- **Install and first answers**: the commands from `kit add` to a first
  answer, and what stays the user's job.
- **Upgrading**: what each release changes, and what an upgrade asks of the
  user (Lines to re-enable, grants to re-author).
- **Operating notes**: the network hosts it reaches, how stale a source may
  get, and how much it retains.

Beside it, a `reference.md` lists every kind, field, saved query and
Procedure the kit defines. Generate it from the definitions, as the security
kit does, so it can't drift from what the kit installs. A larger kit can move
long material into companion pages beside the README (the security kit has
`walkthroughs.md`, `intake.md` and `in-depth.md`), listed in the manifest
like `reference.md`, with a closing **Where next** section in the README
that links each one.

### Share it

The kit directory holds `cruxible-kit.json` (the manifest), `artifacts/`
(one file per definition), `providers/` (the bundled wheels and lock
files) and the inert files the manifest lists, at their own paths (README,
`reference.md` and any companion pages, `examples/`, `skills/<name>/`). Other files in the directory
(build scripts, tests) are never read. Each skill installs as its own
`<kit>-<skill>/` directory (the name stays as is when it already starts with
the kit's id), so a skill's relative links reach only its own files and its
sibling skills; name the README's sections instead of linking to them. Share it
as a directory, or as an OCI image layout. There is no public `kit push`:
official kits are published by Cruxible's release tooling, and a version tag
there never moves to different content.

## Pitfalls

- **Expecting a kit to start anything.** It installs definitions only; adopt
  its example automation deliberately.
- **Upgrading without reading the preview.** A release replaces your edits
  unless you keep them.
- **Building a kit over another kit's prefix.** Two installed kits can't own
  overlapping prefixes, so give a kit you build a prefix of its own.
- **Forgetting the Lines after an upgrade.** An upgrade that changes a
  Procedure brings its Lines back disabled and its settle mandates back to
  propose; `next` lists each one.

## On MCP

`cruxible_kit_add` (a registry reference or an inline bundle),
`cruxible_kit_build`, `cruxible_kit_status` and `cruxible_kit_remove` are in
the `full` MCP profile; set `CRUXIBLE_MCP_PROFILE=full`. They behave as the
CLI commands do. See [MCP tools](mcp-tools.md).
