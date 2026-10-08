# Kits

A kit is a release of definitions that one instance exports and another
installs: vocabulary, named queries, Procedures and Blueprints, plus the
provider packages those Procedures need. Installing or upgrading a kit is one
change set, previewed first and accepted under the instance's approval policy
like any other change.

This page covers using kits and building them. Command details are in the
[CLI reference](cli-reference.md#kit).

## What a kit carries

A kit carries definitions from seven families:

| Family | What it is |
|---|---|
| ClaimTypes | Vocabulary: what a field may hold and what evidence backs it |
| CaptureContracts | What a retained piece of evidence must look like |
| QueryDefinitions | Named queries |
| Procedures | Runnable graphs, every provider slot bound to an exact provider |
| Blueprints | Procedure skeletons whose provider slots the consumer binds |
| ProviderInterfaces | The typed contracts provider slots name |
| SourceAcquisitionPolicies | What a Procedure may read from outside sources |

A kit never carries state or authority: no Subjects or Claims, no Lines or
Triggers, no mandates, no principals, no approval policy. Those belong to the
instance that installs the kit. A Line that should run a kit's Procedure is
authored and enabled by the consumer.

A kit names the provider packages its definitions need in one of two ways:

- **Bundled**: a kit's own domain code (for example a parser for one domain's
  feed) travels inside the kit as the package's built wheel and lock file,
  never as source. Installing the kit installs the package first, and the
  kit's Procedures pin that exact build.
- **Default**: a published, general-purpose package (for example
  `cruxible-provider-web` for `web.fetch`) is named by package and exact
  version, and the kit carries none of its files. Installing the kit installs
  it by name from the provider index, only when nothing installed already
  implements what the kit needs.

## Procedures and Blueprints

A Procedure binds every provider slot to one exact provider, so it runs as
soon as it is accepted. A kit ships a Procedure when the kit itself decides
what fills each slot: its own bundled provider, a default provider, or a
built-in one.

A Blueprint is the same definition with one or more slots left open. Each open
slot names the ProviderInterface it needs, and a Blueprint never runs. A kit
ships a Blueprint when the consumer should choose the provider: which search
service, which model, which converter. The consumer instantiates it, binding
one installed provider per slot, and gets an ordinary Procedure that records
the Blueprint and the bindings it came from. A kit can name a default provider
for a Blueprint's slot, so the slot has something to bind on an instance that
has nothing better.

Any implementation of a slot's interface fits it. An interface is registered
once per instance, by its definition: a second package implementing the same
definition installs onto the same registration instead of a new one, so its
Provider fits the same slots. A provider runs only the kinds of input (buckets)
it claims, and an input it does not claim is refused before it runs.

## Providers you already have

- **Built in.** Every new instance starts with the `workspace.file` interface
  and a built-in provider for it, which turns an authorized workspace file read
  into evidence. Nothing to install. Kits that use it pin it as it is.
- **Published by name.** General-purpose providers install by package name:
  from the operator's provider repository when one is configured, otherwise
  from the provider index (PyPI unless the operator names another).

  ~~~bash
  cruxible provider list                     # what a configured repository offers
  cruxible provider install cruxible-provider-web
  ~~~

  `cruxible-provider-web` implements `web.fetch` and `search.web`. `web.fetch`
  refuses loopback, private, link-local (cloud metadata included), CGNAT,
  multicast and reserved addresses, on the first request and on every
  redirect, and connects only to the address it checked, so DNS rebinding
  cannot reach behind it. It needs direct outbound access and fetches pages as
  served: there is no browser rendering.
  Cruxible owns the `web.fetch` contract, so every package that implements it
  installs onto the same `ProviderInterface:web.fetch`.
- **From a kit.** A kit's own domain provider arrives bundled in the kit; a
  kit's default provider is installed by name when the kit is added and
  nothing here implements its interfaces yet.

Installing a provider needs `admin` permission. It registers the package's
Provider and interfaces as a change, which lands at once when the approval
policy requires no approval and otherwise stops at a proposal to approve and
activate. Installing grants no permission to run anything.

## Use a kit

### Find and inspect

A kit comes from a registry reference, a kit directory, or an OCI image
layout. A bare name resolves under `ghcr.io/cruxible-ai/kits`, so `acme:1.0.0`
means `ghcr.io/cruxible-ai/kits/acme:1.0.0`.

~~~bash
cruxible kit pull acme:1.0.0 --out ./acme-1.0.0     # fetch and verify, install nothing
~~~

`kit pull` prints the digest-pinned reference it fetched. Installing by that
digest reference installs exactly those bytes even if the tag later moves.

### Install

`kit add` previews by default and writes nothing:

~~~bash
cruxible kit add ghcr.io/cruxible-ai/kits/acme@sha256:<digest>
~~~

The preview lists the plan grouped by kind (what each definition does and
how many dependents it has), each provider package and what happens to it
(already installed, `satisfied` when an installed provider already implements a
default's interfaces, or to be installed), and who built the release, from
which instance and at which coordinate. That build
record is claimed by the builder, not proven. The preview ends with the
command that commits exactly what you saw:

~~~bash
cruxible kit add ghcr.io/cruxible-ai/kits/acme@sha256:<digest> --commit --at <oid>
~~~

The result is one of:

| Status | Meaning | Next |
|---|---|---|
| `accepted` | The change set landed: the approval policy required no approval. | Nothing. |
| `proposed` | It awaits approval. | `cruxible proposal approve`, then `cruxible proposal activate`. |
| `awaiting_providers` | A provider installation the kit needs awaits approval. | Approve and activate that installation, then run `kit add` again. |
| `blocked` | Something in current state stops it; the detail says what. | Fix the cause, or choose another release. |

Installing the provider packages a kit needs requires `admin` permission. A
`governed_write` caller can add a kit whose providers are already installed or
satisfied.

A default provider is installed at exactly the version the kit names, and only
as the build the kit recorded: the provider index must list the recorded wheel
hash before the wheel is fetched, and the wheel's lock must match before any
dependency is fetched. Another build under the same name and version is
refused (`cruxible.provider.index_build_differs`) with nothing installed. A
default is skipped (`satisfied`) when no Procedure the kit carries pins it and
an installed provider already implements each interface the kit carries for
it; the Blueprint's slot then takes that provider. A daemon configured with a
provider repository installs by name only from that repository, so there a
default that is not satisfied is refused: install an implementation yourself
and add the kit again.

### Instantiate a Blueprint

`get` on a Blueprint lists each open slot with its interface and the installed
providers that fit it:

~~~bash
cruxible get Blueprint:acme.triage
~~~

When nothing fits a slot, install a provider that implements its interface.
Then bind one provider per slot:

~~~bash
cruxible authoring example blueprint-instance      # a template to edit
cruxible authoring submit - --dry-run <<'EOF'
{"kind": "blueprint_instance",
 "name": "acme.triage-with-web",
 "blueprint": "acme.triage",
 "bindings": {"search": "cruxible-provider-web"}}
EOF
~~~

`--dry-run` reports every refusal without saving anything: a missing or
retired Blueprint, a provider that does not implement the slot's interface, a
slot left unbound. Submit without `--dry-run` (add `--and-activate` to settle
it at once when no approval is needed). The result is an ordinary Procedure:
run it with `cruxible procedure run`, or author a Line over it.

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
such as `--keep ClaimType:acme.account.seats`), or keep every definition you
edited since install with `--keep-local-edits`. The kit's record notes what
you kept on purpose, so later upgrades do not ask again while the release's
version is unchanged.

A definition the release dropped is retired when nothing depends on it. When
something does, it stays live until you decide: `--keep IDENTITY` keeps it,
and `--retire-dependents IDENTITY` retires it together with its dependents.
Definitions the release replaces carry their dependents (Claims, Procedures,
Blueprints) forward to the new version in the same change set.

`kit add` refuses a release older than the one installed unless you pass
`--allow-downgrade`; the preview names the transition (install, upgrade,
downgrade or reinstall).

### Remove

~~~bash
cruxible kit remove acme                 # preview
cruxible kit remove acme --commit --at <oid>
~~~

Removing retires the definitions the kit owns. It does not retire what the kit
carried in from elsewhere, nor its provider packages. A definition you edited
since install must be reverted or retired on its own first, and removal is
refused while live Claims or other definitions still depend on what it would
retire. Like `kit add`, it lands at once when the approval policy requires no
approval and otherwise stops at a proposal.

## Build a kit

A kit is exported from an instance where its definitions are accepted. You
name the identity prefixes the kit owns; every live definition under them, in
the seven families above, is exported together with everything those
definitions pin.

~~~bash
cruxible kit build --id acme --version 1.0.0 --owns acme. --out ./acme-1.0.0
~~~

- `--owns` is repeatable and each prefix ends in a dot (`acme.`,
  `acme.billing.`). Two installed kits may not own overlapping prefixes.
- `--version` is `MAJOR.MINOR.PATCH`. A new release needs a higher version.
- The release is a snapshot: each definition is exported without its local
  history, so the consumer's instance gets its own.
- The built-in `workspace.file` provider is pinned as it is and never
  exported.
- The manifest records who built the release, on which instance and at which
  accepted coordinate.

### Bundle your own provider

When a kit's Procedures pin a provider that is not built in, the kit must
bundle it. Bundling takes the provider's package directory, which holds its
`pyproject.toml`, its `uv.lock`, and in `dist/` the built wheel plus a wheel
for each dependency the lock names by path:

~~~bash
cruxible provider install ./acme-parser/dist/acme_parser-1.0.0-py3-none-any.whl \
  --lock ./acme-parser/uv.lock
# author and accept the Procedures that pin it, then:
cruxible kit build --id acme --version 1.0.0 --owns acme. \
  --provider ./acme-parser --out ./acme-1.0.0
~~~

The build checks that what you bundle is exactly what this instance
installed, that every Provider a carried Procedure pins is bundled, and that
every carried ProviderInterface is what a bundled wheel registers. A changed
wheel needs a new package version, since an instance holding the old build
refuses a different build under the same version. Registry dependencies (for
example `cruxible-provider-runtime`) are not bundled; the consumer's daemon
resolves them from its provider index.

### Name a default provider

For a general-purpose contract such as `web.fetch`, name a published package
as the kit's default instead of bundling it. Lay its directory out as for
`--provider`: a `pyproject.toml` naming the package, the lock the published
wheel embeds as `uv.lock` (`cruxible.kit.provider_lock_not_embedded`
otherwise), and in `dist/` the published wheel plus the wheel of each
dependency that lock names by path (read to check the build, not carried).
Install that same build here, and name it:

~~~bash
cruxible provider install cruxible-provider-web
cruxible kit build --id acme --version 1.1.0 --owns acme. \
  --default-provider ./cruxible-provider-web --out ./acme-1.1.0
~~~

The manifest records the package, its exact version and the wheel and lock
digests (`delivery: index`), and the kit carries none of its files. Every
Provider a carried Procedure pins must be named by `--provider` or
`--default-provider`, as the build this instance installed. A Procedure that
pins the default always gets it installed on `kit add`; a Blueprint slot typed
by its interface takes whatever implementation the consumer already has.

### Share it

The kit directory holds `cruxible-kit.json` (the manifest), `artifacts/`
(one file per definition) and `providers/` (the bundled wheels and lock
files). Share it
as a directory, or as an OCI image layout. There is no public `kit push`:
official kits are published by Cruxible's release tooling, and a version tag
there never moves to different content.

## On MCP

`cruxible_kit_add` (a registry reference or an inline bundle),
`cruxible_kit_build`, `cruxible_kit_status` and `cruxible_kit_remove` are in
the `full` MCP profile; set `CRUXIBLE_MCP_PROFILE=full`. They behave as the
CLI commands do. See [MCP tools](mcp-tools.md).
