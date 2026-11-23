<!-- Bundled from cruxible 0.5.1 docs/kits.md. -->

# Kits

A kit is a starting point. One instance exports it and another installs it:
vocabulary, named queries, Procedures, the capabilities their slots name,
guidance Documents, the provider packages those Procedures need, reference
pages, and an automation template you tailor. It is
not a schema you have to adopt. Every garage that starts from a car parts kit
begins with the same parts, vehicles and fitments, so the same questions and
automation work on day one. From there the instance is yours to grow: revise
the kit's definitions, add your own, and adapt or replace its automation, all
by ordinary authoring. `kit add` previews first and writes nothing. Its
commit proceeds in two phases, each change accepted under the instance's
approval policy: when bundled providers are missing, first a registration
change for each missing provider, then one change with the kit's definitions.
So where a second principal must approve, a kit that carries three providers
is four proposals, not one: one per provider install, then the definitions,
which are filed automatically once the installs activate. An earlier change
can be accepted before a later one stops: an `awaiting_providers` or `blocked`
result means the definitions are not in yet, not that nothing changed. An
upgrade names every definition you have edited; the release's version replaces
your edit unless you keep it.

Every instance starts with one kit already installed: the **base kit**, which
holds the capabilities shared across kits (fetching a web page, reading a
workspace file). This page covers the base kit, using kits, the automation a
kit suggests, and building and documenting kits. Command details are in the CLI reference.

## A worked example: installing car parts

The car parts kit is a folder, `kits/car-parts` in a checkout of the kits
repository. `kit add` takes the folder's path, previews first and writes
nothing:

~~~text
$ cruxible kit add ../kits/kits/car-parts
car-parts 2.0.0: would_propose (install)
Built by owner on inst_de98fea953864570 at 34f5e2a690f7 (claimed)
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
Preview at 4d1d73961885; nothing was written.
Commit it: rerun the same command with --commit --at 4d1d73961885
~~~

Committing at the previewed coordinate lands exactly what you saw, or refuses
if the instance moved in between:

~~~text
$ cruxible kit add ../kits/kits/car-parts --commit --at 4d1d73961885
car-parts 2.0.0: accepted (install)
...
Proposal: sha256:90d28c62…

$ cruxible kit status
base 1.0.0 content sha256:07e64748…; up to date
  source: bundled
car-parts 2.0.0 content sha256:7a99e029…; update check local source
  source: car-parts
  built by owner on inst_de98fea953864570 at 34f5e2a690f7 (claimed)
~~~

`accepted` is what an instance whose owner may approve its own changes prints.
Where a second principal must approve, the same commit prints `proposed` and
the approve command, and the kit is installed once that proposal is approved.

A kit id with a version (`cruxible kit add car-parts:2.0.0`) names a release
in the kit registry. No kit is published there yet, so today that form refuses
`cruxible.kit.not_found`; use the folder path.

Reads say who owns each definition: `orient --section claim_types` ends each
row the kit owns with `kit=car-parts`. From here the vocabulary is yours to
use and to extend: write Claims with it, add your own fields and queries
beside it, and edit its definitions if you need to. A newer release arrives
the same way, and [Evolving your
vocabulary](evolving-your-vocabulary.md#upgrading-a-kit-youve-edited) shows an
upgrade over a local edit.

## What a kit carries

A kit carries definitions from eight families:

| Family | What it is |
|---|---|
| ClaimTypes | Vocabulary: what a field may hold and what evidence backs it |
| SubjectKinds | How a kind's Subjects are keyed; travels with its key ClaimTypes |
| CaptureContracts | What a retained piece of evidence must look like: its decoding and, for a kind an extract reads, its layout |
| Contracts | Immutable data shapes: request, result and row types |
| Capabilities | Immutable, versioned kinds of call that Procedure slots name |
| QueryDefinitions | Named queries |
| Procedures | Runnable graphs over provider calls |
| Documents | Governed Documents, such as guidance linked by `guides`; each one's body travels beside it, at `bodies/<sha256 hex>` |

Beside the definitions, a release can carry inert files for its reader: its
README, other reference pages, `examples/` and agent skills under
`skills/<name>/`. The manifest lists each one with its sha256, but they are
not part of the release's content digest, so a docs-only fix leaves every
definition digest and every install receipt as it was (the image digest still
moves, so a published version takes a patch bump). `kit add` never sends them
to the daemon (its result returns only the automation template's text); `kit
pull` writes them into the kit directory at their own paths.

A kit never carries state or authority: no Subjects or Claims, no Lines or
Triggers, no mandates, no principals, no approval policy. Those belong to the
instance that installs the kit. A Line that should run a kit's Procedure is
authored and enabled by the consumer.

A kit names the provider packages its definitions need in one of two ways.
Which way follows from who uses the provider: a general-purpose provider that
more than one kit uses (such as `cruxible-provider-web`) is a published
dependency, which the kit names at an exact version and the daemon fetches
from the package index; a provider specific to one kit ships inside that kit.

- **Bundled**: a kit's own domain code (for example a parser for one domain's
  feed) travels inside the kit as the package's built wheel and lock file,
  never as source. Installing the kit installs the package first, and the
  package's operations serve the capabilities the kit's Procedure slots name.
- **Default**: a published, general-purpose package (for example
  `cruxible-provider-web` for `web.fetch`) is named by package and exact
  version, and the kit carries none of its files. Installing the kit installs
  it by name from the provider index, only when builds already installed do
  not serve every slot the kit's Procedures declare.

In the manifest a bundled package is an entry of `providers` with its `wheel`,
`lock` and `dependencies` files; a default has `"delivery": "index"`, names
its `operations`, and lists no dependency files. A build made to test a kit
before its general-purpose provider is published may bundle that provider
instead; the kit's released form names it as a dependency.

## Procedures, capabilities and providers

A Procedure never names a provider. Each provider call names a slot, and each
slot names a **capability**: a small, versioned artifact that says what kind of
call it is, with its request Contract, its result Contract and its effect
(`pure`, `external_read` or `external_mutation`):

~~~text
$ cruxible get Capability:workspace.file@1
capability: Capability:workspace.file@1
version: 1
description: Reads one file from an authorized workspace and returns its exact bytes as one Capture. …
effect: pure
in: workspace.file.request@1
out: workspace.file.result@1
~~~

A provider operation declares which capabilities it `serves`, by name, and
carries its own copies of the Contracts and CaptureKind layouts it accepts and
returns. Any installed operation that serves a slot's capability and fits it
can fill the slot, so a kit's Procedures leave the consumer free to choose
which search service, converter or other implementation runs them. Fit is
structural: the operation accepts everything the capability's request may
send, returns what its result promises and nothing more, has the same effect,
and returns each Capture as the CaptureKind the capability names, under a
layout that covers this instance's.

`cruxible orient --section capabilities` lists the capabilities this instance
holds; `--section operations` lists the provider operations a slot can bind
to, each with its request and result Contracts, effect and refusals. Which
capabilities an operation serves is on `get Provider:NAME` (and in the
section's `--json`).

A Line binds every slot to one exact build and operation when it is accepted,
and its runs never rebind. A direct run fits each slot it is not told about to
the single live operation that serves its capability and fits; `--bind
slot=provider` chooses one. The run records the bound builds for replay; the
accepted Procedure stays unchanged. [Lines and
automation](lines-and-automation.md#when-a-slot-cannot-be-bound) lists the
refusals and their repairs.

A provider is pure computation behind its capabilities: it receives every
input its operation's request Contract admits, and refuses, typed, what it
cannot serve. Every Capture a call returns is a named field of its result
(`capture`, `captures`, `document`), typed `capture<K>` for the CaptureKind it
produces, or `capture<*>` for bytes returned verbatim, whose kind the
Procedure's call names.

## The base kit

Every instance is created with the base kit, kit `base`, installed as its first
change. It ships inside Cruxible, so it needs no registry and no network, and
holds the capabilities shared across kits under the prefixes `web.` and
`workspace.`:

| Capability | Effect | What it does |
|---|---|---|
| `workspace.file@1` | pure | Read one workspace file; one Capture. Served by core. |
| `workspace.files@1` | pure | Read every file a glob matches; a sorted list of Captures. Served by core. |
| `web.fetch@1` | external_read | One HTTP GET; the body the origin sent, as one Capture. |
| `web.fetch.pages@1` | external_read | One resource fetched as pages, one Capture per page. |
| `web.render@1` | pure | A readable document (Markdown or JSON) derived from one fetched HTML Capture. |
| `web.render.dom@1` | external_read | A page loaded in a browser; the serialized DOM. |
| `web.search@1` | external_read | One query to a search service; its response verbatim. |
| `web.search.results@1` | pure | A search response read into the shared form, CaptureKind `web.search-results`. |

It also holds each capability's request and result Contracts and the one
shared CaptureKind, `web.search-results`, with its layout. It holds no
provider, Procedure or vocabulary.

~~~text
$ cruxible init
...
Approval policy: self_approval_allowed
Base kit: base 1.0.0 installed
...
$ cruxible kit status
base 1.0.0 content sha256:07e64748…; up to date
  source: bundled
~~~

- **Who serves what.** Cruxible serves `workspace.file` and `workspace.files`
  itself, through its built-in provider `cruxible-builtin`; a provider package
  that declares a `workspace.` capability is refused
  (`cruxible.provider.capability_reserved`). Nothing serves the `web.`
  capabilities until you install a provider:
  `cruxible provider install cruxible-provider-web` (it needs the network). Until
  then a run whose slot names one refuses `cruxible.line.slot_unfilled`.
- **Under independent approval.** An instance created with
  `--require-independent-approval` cannot land the base kit on its creator's
  approval alone: `init` stops it at a proposal and prints the approve
  command for a second principal (`Base kit: base 1.0.0 awaits approval,
  proposal sha256:… (cruxible proposal approve sha256:… --signer-id reviewer,
  as a second principal)`). Until it is approved, the capabilities are
  absent, and `kit status` lists the pending add with its proposal and the
  approve command. If other changes land before the approval, the approval
  lands the same change set at the new head (`Landed at the head as sha256:…:
  the same change set`). A kit with vocabulary that was proposed while the
  base kit waited is different: once the base kit lands, that proposal was
  planned against an older head and must be committed again
  ([Install](#install)), so approve the base kit first.
- **Upgrading.** The base kit changes only with a Cruxible release. A newer
  release may bundle a newer version; `kit status` compares the installed one
  with the bundled one (no registry is asked), and says when the installed
  content differs from the bundled release of the same version; and `cruxible kit add base`
  previews and proposes the upgrade, with the usual keep rules. Nothing
  upgrades by itself.
- **Yours to extend.** Its definitions are ordinary instance state: add a
  capability version, grow `web.search-results` with a record set (see
  [When a slot cannot be bound](lines-and-automation.md#when-a-slot-cannot-be-bound)
  for what growing a derived kind asks of its providers), or author your own
  capabilities under your own prefix. A capability or Contract version never
  changes once accepted: a wording fix is the next version.

## A kit that requires another kit

A kit whose definitions use another kit's definitions does not carry copies
of them: its manifest lists the kit it **requires**, with the lowest version
it works with:

~~~json
"requires": [{"kit_id": "base", "min_version": "1.0.0"}]
~~~

`kit build` writes this for you. A Procedure whose slot names `web.fetch@1`
pins a capability the base kit owns, so building its kit leaves that pin out of
the bundle and records the base kit at the version installed where you build:

~~~text
$ cruxible kit build --id catalog --version 1.0.0 --owns catalog. --out ./catalog-1.0.0
catalog 1.0.0: 21 artifacts, 0 provider package(s), content sha256:2616b6a2…
  requires: base >= 1.0.0
~~~

`kit add` checks each requirement before anything else, and `kit remove`
refuses to take away a kit another one needs:

| Refusal | When | Repair |
|---|---|---|
| `cruxible.kit.requires_missing` | A required kit is not installed, or is older than `min_version`; also when a change would take a required kit below it. | Add or upgrade the required kit first (the message names the command). |
| `cruxible.kit.requires_unresolved` | A pin the bundle left out does not resolve here: a Contract or capability at the exact digest the kit was built against, or a CaptureKind by name, live. | Install the required kit's release that holds it, or restore the definition. |
| `cruxible.kit.remove_required` | `kit remove` of a kit an installed kit requires. | Remove the dependent kit first. |

~~~text
$ cruxible kit add ./catalog-1.0.0
Error: cruxible.kit.requires_missing: Kit catalog 1.0.0 requires kit base 1.0.0 or later; this instance holds none. Add it first: kit add base (or the path of a local build of base 1.0.0 or later).
Repair: cruxible kit add base

$ cruxible kit remove base          # on an instance with the security kit
Error: cruxible.kit.remove_required: Kit base cannot be removed: kit security requires it.
Repair: cruxible kit remove security
~~~

The base kit itself is never removed (see [Remove](#remove)).

`kit status` and the `kit add` preview show each requirement and whether it
is met (`requires base >= 1.0.0: met (installed 1.0.0)`, or `not met (not
installed)`), and the kit's receipt records it as installed.
Carrying a copy still works for a definition that belongs to no installed
kit.

## Providers you already have

- **Built in.** Every instance has `cruxible-builtin`, core's own provider,
  which serves `workspace.file` and `workspace.files`: the daemon reads the
  file and keeps its exact bytes as a Capture. Nothing to install; a slot that
  names either capability binds to it.
- **Published by name.** General-purpose providers install by package name:
  from the operator's provider repository when one is configured, otherwise
  from the provider index (PyPI unless the operator names another).

  ~~~bash
  cruxible provider list                     # what is installed, and what a configured repository offers
  cruxible provider install cruxible-provider-web
  ~~~

  `cruxible-provider-web` serves `web.fetch`, `web.fetch.pages`, `web.search`,
  `web.render` and `web.render.dom`. `web.fetch` returns the body the origin
  sent as its Capture, verbatim, with the URL, final URL, status, media type,
  request digest and kept headers in the Capture's coordinate; `web.search`
  likewise returns the search service's response body. `web.render` reads a
  fetched Capture and returns a derived Capture citing it (the page's main
  content as Markdown, or canonical JSON). `web.fetch` refuses loopback,
  private, link-local (cloud metadata included), CGNAT, multicast and reserved
  addresses, on the first request and on every redirect, and connects only to
  the address it checked, so DNS rebinding cannot reach behind it. It needs
  direct outbound access and fetches pages as served. Any package whose
  operation serves `web.fetch` and fits fills the same slots.
- **From a wheel file.** A built wheel installs without the index, on an
  instance with no kit: pass the lock it was built with and each locked
  dependency wheel. The client reads the files and sends their bytes, so this
  also works against a remote daemon. Here under an approval policy, so it
  stops at a proposal:

  ~~~text
  $ cruxible provider install ./cruxible_provider_web-0.7.0-py3-none-any.whl \
      --lock ./cruxible-provider-web-0.7.0.uv.lock \
      --dependency ./cruxible_provider_runtime-0.5.0-py3-none-any.whl --commit
  cruxible-provider-web/f3543a2990f61a7aaab8bfcd7263fc0334577142e7ef1ee40de389ab68373e93: awaiting_approval
  Installation and registration grant no execution permissions. Runtime resources require verification.
  Proposal: sha256:7e748e91dc8d94ddbbe867c1d906e536a4fd41c356342ac4f20064734cd4d624
  ...
  ~~~

  `--dry-run` previews an install by name only; a local wheel refuses it.
- **From a kit.** A kit's own domain provider arrives bundled in the kit; a
  kit's default provider is installed by name when the kit is added and the
  kit's slots are not already served here.

Installing a provider needs `admin` permission. It registers the build and
its operations as a change, which lands at once when the approval policy
requires no approval and otherwise stops at a proposal to approve and
activate. A provider installs on its own, with no kit present: it carries the
shapes it accepts and returns. Installing grants no permission to run
anything.

`get Provider:NAME` shows each operation with what it serves and where its
code is:

~~~text
$ cruxible get Provider:cruxible-builtin
provider: cruxible-builtin
...
operations:
  workspace.file  workspace.file.handoff@1 -> workspace.file.result@1 (pure)  serves workspace.file
    Reads one file from an authorized workspace and returns its exact bytes as one Capture. ...
    source src/cruxible_core/providers/builtin_workspace_file.py (https://github.com/cruxible-ai/cruxible)
    ...
~~~

## Use a kit

### Find and inspect

A kit comes from a kit directory, an OCI image layout, or a registry
reference. A directory works today: a checkout of the kits repository, a
folder `kit build` wrote, or one `kit pull --out` wrote. Registry references
resolve once kits are published to the registry, which no kit is yet: a bare
name resolves under `ghcr.io/cruxible-ai/kits`, so `car-parts:2.0.0` means
`ghcr.io/cruxible-ai/kits/car-parts:2.0.0`, and until that release exists
`kit add` and `kit pull` refuse it (`cruxible.kit.not_found`).

~~~bash
cruxible kit pull ../kits/kits/car-parts --out ./car-parts    # verify and copy, install nothing
cruxible kit pull car-parts:2.0.0 --out ./car-parts-2.0.0     # from the registry, once published
~~~

`kit pull` of a registry reference prints the digest-pinned reference it
fetched. Installing by that digest reference installs exactly those bytes even
if the tag later moves.

### Install

`kit add` previews by default and writes nothing:

~~~bash
cruxible kit add /abs/path/kits/security
~~~

KIT is a local kit directory (one holding `cruxible-kit.json`, such as a
checkout or a `kit pull --out` folder), an OCI image layout, or, once kits are
published, a registry reference
(`ghcr.io/cruxible-ai/kits/car-parts@sha256:<digest>`).
`cruxible_kit_add` takes the same local paths.

The preview lists the plan grouped by kind (what each definition does and
how many dependents it has), each provider package and what happens to it
(already installed, `satisfied` when operations installed here already serve
and fit every slot the kit's Procedures declare, or to be installed), the
kits it requires and whether each is met, and who built the release, from
which instance and at which coordinate. For a kit that bundles providers, it
says a commit installs those providers first, then proposes the definitions,
which are checked against the installed providers. That build
record is claimed by the builder, not proven. The preview ends with the
command that commits exactly what you saw:

~~~bash
cruxible kit add /abs/path/kits/security --commit --at <oid>
~~~

The result is one of:

| Status | Meaning | Next |
|---|---|---|
| `accepted` | The change set landed: the approval policy required no approval. | Nothing. |
| `proposed` | It awaits approval. | `cruxible proposal approve`, as a second principal; the approval that completes it activates it. |
| `awaiting_providers` | One or more provider installations the kit needs await approval, each its own proposal. | Approve each installation; when the last one activates, the definitions are proposed on their own, as you (`kit status` shows the resume). Then approve that proposal too. |
| `blocked` | Something in current state stops it; the detail says what. | Fix the cause, or choose another release. |

Under independent approval the security kit, which carries three providers,
goes like this:

~~~text
$ cruxible kit add /abs/path/kits/security --commit --at b0a3ac708d13
security 1.3.0: awaiting_providers (install)
  provider cruxible-provider-web (cruxible-provider-web 0.7.0): awaiting approval, proposal sha256:e4d45ca1…
  provider security-osv-provider (security-osv-provider 0.1.0.dev0): awaiting approval, proposal sha256:c7ba6329…
  provider security-sarif-provider (security-sarif-provider 0.1.0.dev0): awaiting approval, proposal sha256:d351f954…
  requires base >= 1.0.0: met (installed 1.0.0)
...
$ cruxible kit status --offline
security 1.3.0: kit add waiting (as owner); waiting on provider install cruxible-provider-web, security-osv-provider, security-sarif-provider
...
~~~

After the reviewer approves the three installs, the fourth proposal is there
without another `kit add`:

~~~text
$ cruxible kit status --offline
security 1.3.0: kit add proposed (as owner); proposal sha256:11b0a727…
  next: cruxible proposal approve sha256:11b0a727… --signer-id '<SIGNER_ID>' (the approval that completes it activates it)
...
~~~

Approving it installs the kit, and `kit status` then lists it like any
installed kit, with each provider `installed`.

**A kit proposal planned against an older head must be committed again.** A
kit change that holds vocabulary is planned against the instance as it was
(kit ownership, requirements, provider pins, your edits), so it is never
rebased. If another change lands between the commit and the approval, the
approval refuses and the author runs the same `kit add ... --commit` at the
current head, which files a fresh proposal to approve:

~~~text
$ cruxible proposal approve sha256:f47a43c8… --yes
...
Error: cruxible.proposal.readmit_requires_resubmission: proposal sha256:f47a43c8… is a kit change planned against an earlier state (kit ownership, requirements, provider pins, local edits), so it is not readmitted or landed by its old bytes; its author 'owner' adds kit car-parts again at the current head (kit add): kit car-parts 2.0.0, from the kit directory, layout or registry reference it was added from (recorded as car-parts); the daemon holds the kit's bytes and that label, not a reference it can vouch for
...
~~~

The daemon does not know the path the kit came from, so the author names the
kit's folder again. The proposal left behind stays in `proposal list --status
open` and can never be approved.

The provider installs and the base kit hold no vocabulary, so their approvals
land at the head whatever landed first.

Installing the provider packages a kit needs requires `admin` permission. A
`governed_write` caller can add a kit whose providers are already installed or
satisfied.

Your own definitions under a kit's prefix are not an ownership collision. One
the release also defines appears in the plan as `replace: ... (takes over a
definition you defined outside the kit)` (`takes_over_outside_definition` in
`--json`). Next: pass `--keep IDENTITY` to keep your version.

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
default is skipped (`satisfied`) when, for every slot the kit's Procedures
declare, some other live operation here serves its capability and fits; those
slots then bind to them. A daemon configured with a
provider repository installs by name only from that repository, so there a
default that is not satisfied is refused: install an implementation yourself
and add the kit again.

### Run a slotted Procedure

`get` on a Procedure renders its declared slots and the capability each names:

~~~bash
cruxible get Procedure:catalog.price-check
~~~

A direct run fits each slot to the single live operation that serves its
capability and fits. No fit refuses `cruxible.line.slot_unfilled`; several fits refuse
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

`kit status` lists each installed kit with its version and content digest
(the one `kit add` pins, labelled `content sha256:`), the definitions edited
here since install (`edited locally: PATH`), the ones kept on purpose
(`kept: IDENTITY (your edit)`), its provider packages with their state
(`installed`, `differs`, `satisfied` or `missing`), the kits it requires,
and, for a kit installed from a registry, the newest release available (it
lists the repository's tags; `--offline` skips that). An installed release
whose content differs from the available one of the same version says so,
rather than `up to date`. A kit installed from a directory or layout reports
`update check local source`. Two kinds of kit add still under way are listed
first, each with the command that approves it: the base kit proposal `init`
filed, and an add that stopped at `awaiting_providers` (while it waits, and
then with the definitions proposal it filed). An ordinary `proposed` add of a
kit without missing providers is not in `kit status`: find it with `cruxible
proposal list --status open`. Once its proposal is accepted, a kit is listed
as installed and the pending line is gone.

Installing a newer release is the same `kit add` with the new version. An
upgrade always proposes, and the release's version wins by default. The
preview names what each replacement does:

- it overwrites an edit you made since install;
- it re-adds a definition retired outside the kit's record (for example after
  `kit remove`); the plan names it, and in this release the add is then
  refused, because a retired definition cannot be added back
  ([Remove](#remove));
- it takes over a definition you defined outside the kit;
- it replaces a definition the kit carried in as a dependency.

A definition the kit installed that you retired since -- any kind, including a
provider build -- stays retired: the upgrade skips it without `--keep` and lists
it as `kept retired: ...`. The add refuses only when something the release
still installs depends on it (`cruxible.kit.retired_dependency`, naming both).
Retiring a definition an installed kit bundles warns, naming the kit, because
nothing re-adds it afterwards.

To keep your version instead, name it with `--keep` (repeatable, by identity
such as `--keep ClaimType:catalog.part.title`), or keep every definition you
edited since install with `--keep-local-edits`. The kit's record notes what
you kept on purpose, so later upgrades do not ask again while the release's
version is unchanged. Copy the identity as the preview prints it, and read the
preview again with the flag before committing: a `--keep` that matches nothing
in the plan is ignored without a message, and a Document's identity is
lowercase (`--keep document:catalog.fitment-review`; `Document:...` does not
match).

**Fields you add beside a kit's.** A field you add to a kit's kind
(`catalog.part.shelf`, say) is yours: it lands with no kit marker and no
warning, `kit status` does not list it, and an upgrade leaves it alone for as
long as no release defines that name. If a later release adds a definition
with the same identity, the upgrade replaces yours with the release's and the
kit owns it from then on, unless you keep yours; the preview says so and names
the flag:

~~~text
ClaimType:
  replace: ClaimType:catalog.part.weight_grams (takes over a definition you defined outside the kit)
...
Next: rerun cruxible kit add with --keep ClaimType:catalog.part.weight_grams to keep yours.
~~~

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
the wheel and lock or image, excluding the host environment. Capabilities are
shared: builds whose operations serve the same capability and fit it fill the
same slots. Installing a
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
carrying them disabled and their Mandates propose-only; each slot moves to the
target build's operation of the same name if it serves the capability and
fits, else to the one that does, else the move refuses with the reason.
`cruxible provider retire NAME/BUILD` is refused
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

### Revise a kit's Document

A Document a kit installs (its review guidance, say) lands as a Document
only: no file appears in your workspace and `.cruxible/files.yaml` gets no
entry. To revise it, write its body to a file, catalogue the file as that
Document, record its links, then propose the edited file:

~~~bash
cruxible get Document:catalog.fitment-review                  # title, kind, links
cruxible get Document:catalog.fitment-review --detail body --output docs/fitment-review.md
cruxible files add fitment-review docs/fitment-review.md --document-id catalog.fitment-review \
  --document-kind guidance --title "Fitment review" --scope catalog
cruxible link docs/fitment-review.md --guides ClaimType:catalog.part.fits
# edit docs/fitment-review.md, then:
cruxible files propose --file fitment-review --name fitment-review-local
~~~

Give `files add` the Document's own id, kind and title, and its governance
scope, which `get` does not print: it is `governance_scope` in the kit
directory's `artifacts/documents/<id>.json`. Run `cruxible link FILE --guides
IDENTITY` for every link `get` lists before the first `files propose`: the
catalog entry carries the links, and a revision proposed from an entry without
them drops them (run `link` again to restore one). Afterwards `kit status`
lists the Document as `edited locally: documents/catalog.fitment-review.json`,
and an upgrade replaces your revision unless you keep it (`--keep
document:catalog.fitment-review` or `--keep-local-edits`).

### Remove

~~~bash
cruxible kit remove car-parts                 # preview
cruxible kit remove car-parts --commit --at <oid>
~~~

**Removal is one-way in this release.** It retires the kit's definitions that
are still live, and a retired definition cannot be added back: after a
removal, `kit add` of the same kit (any version that carries those
definitions) is refused on that instance. Remove a kit only when you will not
want it there again; to start over with it, use a new instance. The preview
says so on its `one-way:` line:

~~~text
$ cruxible kit remove car-parts
car-parts 2.0.0: would_propose
ClaimType:
  retire: ClaimType:catalog.decision.amends
  ...
QueryDefinition:
  retire: QueryDefinition:catalog.parts_for_vehicle
SubjectKind:
  retire: SubjectKind:catalog.decision
  retire: SubjectKind:catalog.note
  retire: SubjectKind:catalog.part
  retire: SubjectKind:catalog.vehicle
one-way: the definitions this removal retires cannot be added back in this release
Preview at 4b947a8393e9; nothing was written.
Commit it: rerun the same command with --commit --at 4b947a8393e9
~~~

A definition that stays is listed as `keep:` with its reason, for example
`keep: document:catalog.fitment-review (Documents have no retired state and
remain accepted)`.

Removing retires the definitions the kit owns: its ClaimTypes, SubjectKinds,
CaptureContracts, QueryDefinitions and Procedures. It does not retire what the
kit carried in from elsewhere, nor its provider packages. Some owned
definitions stay accepted, each with its reason on the preview's `keep` line:
Contracts and capabilities (their versions are immutable), Documents (they have
no retired state), and the endpoint ClaimTypes of a pair kind (a pair kind's
endpoints are fixed for life; the security kit keeps six). A definition you
edited since install blocks the removal (`would_block`, `edited kit paths must
be reverted or retired by their own change first: PATH`): revert it (`kit add`
of the installed release again replaces your edit with the release's) or
retire it on its own first. An edit you kept on purpose at an upgrade does not
block. Removal is refused while an installed kit requires the kit
(`cruxible.kit.remove_required`).

Removal is also refused while live Claims or definitions you authored (Lines,
Mandates, Triggers, Runbooks over the kit's Procedures) still depend on what it
would retire. The refusal counts the dependents by kind (Claims grouped by
ClaimType), names a few, and gives the repair: retire the automation you
authored in one authored change, retire the Claims one Subject at a time
(`cruxible retire KIND/ID --with-claims --because TEXT`), then preview the
removal again; or keep the kit. `kit remove` has no option that retires Claims,
so an instance that has recorded many Claims under a kit in practice keeps it.
Nor does it retire the automation you authored over the kit: that is yours,
and it stays until you retire it. A blocked removal prints the refusal and its
repair first, then the plan as counts per action and kind; add `--detail` to
list each path.

A blocked removal records nothing: the commit checks the removal at the head
its preview was pinned to and submits only what passes, and the command exits
1; if the head moved since the preview, it refuses `cruxible.preview.state_moved`
before checking. A blocked preview is still an answer and exits 0. Like `kit add`, a removal
that passes lands at once when the approval policy requires no approval and
the request's tier may activate (`graph_write`), and otherwise stops at a
proposal. Below `graph_write` (an MCP server capped by `CRUXIBLE_MODE`, say),
`kit add` and `kit remove` stay proposed and the result says who can activate
them.

After a removal, adding the kit again previews as `would_block`: each
definition is listed as `replace: ... (re-adds a retired definition)`, and the
refusal follows, one code per kind with a count. The commit prints the same
and exits 1:

~~~text
$ cruxible kit add ../kits/kits/car-parts
car-parts 2.0.0: would_block (install)
...
ClaimType:
  replace: ClaimType:catalog.decision.amends (re-adds a retired definition)
  ...
Refused: cruxible.claim_type.lifecycle_invalid: A retired ClaimType cannot be revived or revised. (19 definitions); cruxible.query_definition.lifecycle_invalid: A retired QueryDefinition cannot be revived or revised.; cruxible.subject_kind.revival_refused: A retired SubjectKind cannot be revived; propose a new SubjectKind name instead. (4 definitions). Definitions retired here cannot be added back in this release: add the kit on a new instance
Preview at 64b5d915270e; nothing was written.
~~~

When Subjects were retired to clear the way for the removal, their retired
Claims still name the old definitions and the same add refuses
`cruxible.change_set.unresolved_pin` instead; the repair is the same, a new
instance. A removal whose every path is kept says `retires nothing` in place
of the `one-way:` line.

The base kit cannot be removed:

~~~text
$ cruxible kit remove base
Error: cruxible.kit.base_required: the base kit stays installed: it holds the capabilities and Contracts that workspace reads and web fetches name, and a definition it retired could not be added back.
Repair: Leave the base kit installed; to stop using one of its definitions, retire that definition in an authored change (cruxible authoring submit).
~~~

## Automation that comes with a kit

A kit never installs Lines, Triggers or mandates: what runs on your instance,
when, and with how much authority is yours to decide. A kit that has
automation to suggest ships it as one **automation template**: a change set of
Lines, Triggers, mandates and Runbooks, at the path its `cruxible-kit.json`
names in `automation_template` (the security kit's is `automation.json`).
`kit add` and an upgrade never install or submit it, and `kit add` writes
nothing to your workspace: its result carries the template's text
(`automation_template_content`, in a preview and a commit alike, and in
`--json` output), checked against the manifest's sha256, for you to write to a
file. `kit pull --out DIR` writes it into the kit directory with the kit's
other files. The kit's build checked that it authors cleanly against the kit's own
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
   step, what it reads and writes, each call's provider build
   (`Provider:NAME/<12 hex>`) and the capability its slot names, what its
   mandates settle and what would go to review, and any step that reads what only a later step writes.
4. **Run** it: `cruxible runbook run security.start`. Steps run in order; a step
   whose writes go to review stops the run, so approve that proposal and run it
   again.

Then enable each Line you want running on its schedule (`cruxible line enable
NAME...`, or `--runbook RUNBOOK` for every Line a Runbook names). The kit's README says which Lines settle what and why
([Lines and automation](lines-and-automation.md#earning-settle)).

A Line in a kit's template that posts to the inbox follows the inbox rule
([Lines and automation](lines-and-automation.md#the-inbox-findings-a-line-cant-decide)):
an item is a call for attention, not storage. A list that is already a Query
or a Capture gets one summary item (the count, a short sample, the Query or
Capture to read) deduped by a digest of the set, so an unchanged set is seen
again and a changed set is a new item; one item per element only when each
needs its own decision recorded nowhere else.

## Build a kit

A kit is exported from an instance where its definitions are accepted. You
name the identity prefixes the kit owns; every live definition under them, in
the families above, is exported together with everything those definitions
pin.

~~~bash
cruxible kit build --id car-parts --version 1.0.0 --owns catalog. --out ./car-parts-1.0.0
~~~

- `--owns` is repeatable and each prefix ends in a dot (`catalog.`,
  `catalog.pricing.`). Two installed kits may not own overlapping prefixes.
- `--version` is `MAJOR.MINOR.PATCH`. A new release needs a higher version.
- The release is a snapshot: each definition is exported without its local
  history, so the consumer's instance gets its own.
- A definition another installed kit owns (the base kit's capabilities, for
  example) is left out of the bundle and that kit is recorded in `requires`
  ([A kit that requires another kit](#a-kit-that-requires-another-kit)).
  Cruxible's built-in provider is pinned as it is and never exported.
- The manifest records who built the release, on which instance and at which
  accepted coordinate.
- A kit cannot carry a rule that names one workspace file. Own words and
  workspace file and section citations are admitted on every field that is not
  `evidence_requirement: captured`, and need no rule; `captured` means a
  shaped payload under a CaptureContract the field names in `evidence:`, and
  that contract travels in the kit. A rule that names one workspace file's own
  contract (`cruxible.foreign-source.NAME`) can no longer be authored
  (`cruxible.claim_type.per_file_evidence_rule`), and a field that still holds
  one from an earlier build refuses the kit build
  (`cruxible.kit.per_file_evidence_rule`): the consumer may not have that
  file.
- `--automation-template FILE` names the kit's automation template (it sits at
  the kit's root under its own file name). The build refuses
  (`cruxible.kit.automation_template_invalid`, naming the template and its first
  diagnostic) unless it authors cleanly against this instance's accepted
  definitions. The check is an authoring preview: nothing is stored. The
  template travels as one of the kit's files, outside the release's content
  digest, and is never installed.
- `--file PATH` (repeatable; new in 0.5.0) copies a plain file for the kit's
  reader, such as `README.md` or a reference page, into the kit at its path
  relative to the workspace root, and lists it in the manifest's `files` with
  its sha256 (a root `README*` is the `readme`, `examples/...` an `example`,
  `skills/<name>/...` a `skill`, anything else a `reference`). Like the
  template it is outside the content digest. A missing file or one outside the
  workspace refuses `cruxible.kit.file_refused` with its repair.

~~~text
$ cruxible kit build --id car-parts --version 1.0.0 --owns catalog. --out ../car-parts-1.0.0 \
    --file README.md --file reference.md --file examples/parts.csv
car-parts 1.0.0: 16 artifacts, 0 provider package(s), content sha256:967b563a…
$ cruxible kit build --id car-parts --version 1.0.0 --owns catalog. --out ../x --file nope.md
Error: cruxible.kit.file_refused: nope.md: is not a file in the workspace; repair:
Repair: check the path, or create the file before building
~~~

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

### What a provider package declares

A provider package ships `registration.json`, schema 3. Each operation says
what it is, where its code is, and which capabilities it serves; its request,
result and refusal Contracts are the package's own:

~~~json
{
  "schema_version": 3,
  "manifest": {"path": "manifest.yaml", "digest": "sha256:…"},
  "operations": [
    {
      "name": "parts.read",
      "description": "Reads one supplier price sheet: one document of parts with their list prices.",
      "source": {
        "path": "src/catalog_parser/sheet.py",
        "repository": "https://github.com/example/catalog-parser"
      },
      "serves": ["catalog.price-sheet.read"],
      "in": "catalog.price-sheet.read.request@1",
      "out": "catalog.price-sheet.read.result@1",
      "effect": "pure",
      "refusals": {
        "provider_declined": {"contract": "parts.read.refusal.provider_declined@1", "absorbable": false}
      },
      "entrypoint": "catalog_parser.sheet:ReadSheet"
    }
  ],
  "declares": {
    "contracts": ["… every Contract the operations reach, in full …"],
    "capture_kinds": ["… every CaptureKind they name, with its decoding and layout …"],
    "built_against": [
      {"kind": "Capability", "name": "catalog.price-sheet.read", "version": 1, "digest": "sha256:…"}
    ]
  }
}
~~~

- `description` (at most 2048 characters) says what the operation does, led
  by its result. `source.path` is required; `repository` and `revision` (a
  full commit hash) are optional, since a local build has neither.
- `serves` lists capability names, sorted, at most 16, with no versions: fit
  is checked against whichever version a slot pins. Naming a `workspace.`
  capability is refused (`cruxible.provider.capability_reserved`): core serves
  those.
- `declares` carries a copy of every Contract and CaptureKind the operations
  reach, and nothing they do not. The copies are never installed as instance
  definitions, so a provider installs with no kit present, and its names
  never collide with the consumer's. The usual way to build them is to copy
  the capability's Contracts and the kind's layout byte for byte from the kit
  (or the base kit) the provider is built for: the digests are then equal and
  fit is immediate. An operation may accept more than the capability sends
  (an extra optional request field, a wider type) by declaring its own
  request Contract under its own name; it may not return a field the
  capability's result does not declare.
- `built_against` records which capability and kind versions the copies came
  from; nothing gates on it.
- A result Contract cannot be the Capture itself: every Capture is a named
  field, `capture<K>` (or a list of them), and an operation that returns a
  derived kind must name that kind (`capture<*>` fits only a capability that
  also says `*`).
- `may_truncate: true` marks an operation that may return a truncated
  listing; it binds only a slot that declares `may_truncate` too.

A registration in an older schema is refused at install. A new version on
either side (a capability version, a provider build) never needs the package
re-registered: bindings are rechecked at the next Line acceptance or run
admission.

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
the default unless operations already here serve and fit every slot the kit's
Procedures declare. A Line binds each slot to an exact build when accepted; a direct run
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

Beside it, a `reference.md` can list every kind, field, saved query and
Procedure the kit defines. Cruxible has no command that writes one: generate
it with a script of your own over the instance's definitions (`cruxible orient
--section claim_types --json`, and likewise `subject_kinds`, `queries` and
`procedures`), run before each build, so it can't drift from what the kit
installs. A larger kit can move long material into companion pages beside the
README (the security kit ships a `reference/` folder of them), with a closing
**Where next** section in the README that links each one. Name every such
file with `--file` at build, so the manifest lists it.

### Share it

The kit directory holds `cruxible-kit.json` (the manifest), `artifacts/`
(one file per definition), `bodies/` (the body of each Document the kit
carries, one file named by its sha256 in hex; absent when the kit carries no
Document), `providers/` (the bundled wheels and lock
files; absent when it bundles none) and the inert files the manifest lists, at their own paths (README,
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
- **Removing a kit to reinstall it.** Removal is one-way in this release: the
  definitions it retires cannot be added back on that instance.
- **Counting on one approval.** Under independent approval a kit with
  providers is one proposal per provider install and then one for the
  definitions.
- **Building a kit over another kit's prefix.** Two installed kits can't own
  overlapping prefixes, so give a kit you build a prefix of its own.
- **Copying shared definitions into your kit.** Use the base kit's
  capabilities where they fit; your kit then requires `base` and carries no
  copy. Declare a capability of your own, under your own prefix, only for a
  kind of call the base kit does not have.
- **Forgetting the Lines after an upgrade.** An upgrade that changes a
  Procedure brings its Lines back disabled and its settle mandates back to
  propose; `next` lists each one.

## On MCP

`cruxible_kit_add` (a kit directory, layout or registry reference, or an inline bundle),
`cruxible_kit_build`, `cruxible_kit_status` and `cruxible_kit_remove` are in
the `full` MCP profile; set `CRUXIBLE_MCP_PROFILE=full`. They behave as the
CLI commands do. See MCP tools.
