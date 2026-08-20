# CLI reference

The public CLI has four top-level command groups.

## Global options

~~~text
--server-url TEXT
--server-socket TEXT
--instance-id TEXT
--json-compact
--version
~~~

Server-mode instance selection defaults to remembered context.

## context

Manage remembered daemon and instance context:

~~~text
cruxible context connect
cruxible context use
cruxible context show
cruxible context clear
~~~

## credential

Manage runtime bearer credentials:

~~~text
cruxible credential claim-bootstrap
cruxible credential mint
cruxible credential list
cruxible credential rotate
cruxible credential revoke
cruxible credential recover-admin
~~~

These credentials authorize transport operations. They are distinct from
Playbill signing principals.

## server

~~~text
cruxible server start
cruxible server status
cruxible server info
cruxible server restart
~~~

server start is the long-running daemon process and does not connect to an
existing server.

## playbill host

~~~text
cruxible playbill host create [--instance-id ID]
~~~

Allocates an empty daemon-owned host and remembers it.

## playbill init

~~~text
cruxible playbill init --key-dir DIR
  [--principal-id ID]
  [--recovery-key-dir DIR]
  [--recovery-principal-id ID]
  [--profile local|cloud]
~~~

Generates client-held principal keys outside the workspace and bootstraps the
ledger with public principal records.

## playbill body

~~~text
cruxible playbill body store PATH
~~~

Stores exact bytes in inert CAS and prints their digest.

## playbill document

~~~text
cruxible playbill document propose --envelope FILE --name NAME
cruxible playbill document list
cruxible playbill document get IDENTITY
cruxible playbill document body IDENTITY [--output FILE]
cruxible playbill document history IDENTITY
~~~

## playbill subject

~~~text
cruxible playbill subject propose --envelope FILE --name NAME
cruxible playbill subject list
cruxible playbill subject get KIND ID
cruxible playbill subject history KIND ID
~~~

A Subject is an identity-only referent. KIND and ID are the two halves of its
`Subject:<kind>/<id>` identity.

## playbill claim-type

~~~text
cruxible playbill claim-type propose --envelope FILE --name NAME
cruxible playbill claim-type list
cruxible playbill claim-type get PREDICATE
~~~

A ClaimType is the governed interface a predicate must satisfy before any Claim
may state it.

## playbill claim

~~~text
cruxible playbill claim propose --authoring FILE --name NAME
cruxible playbill claim list [--subject PATH] [--predicate P] [--include-retired]
cruxible playbill claim get IDENTITY
cruxible playbill claim history IDENTITY
cruxible playbill claim explain IDENTITY [--evaluation-time TS]
~~~

propose creates one inert Capture and one dependency-closed Claim in a single
governed proposal. explain returns the verdict together with the law evidence
and source handles it was computed from.

## playbill query

~~~text
cruxible playbill query propose --envelope FILE --name NAME
cruxible playbill query list
cruxible playbill query get NAME
cruxible playbill query run NAME [--parameters FILE] [--evaluation-time TS]
~~~

run executes one accepted QueryDefinition and prints its
`playbill-query-execution-receipt-v1`: the definition digest, the resolved
parameter digest, and the result digest that replays it.

## playbill discover

~~~text
cruxible playbill discover [--query TEXT] [--entrypoint NAME]
  [--profile interfaces|subjects|all]
  [--evaluation-time TS]
~~~

Exactly one of --query or --entrypoint selects the page. Matching is exact and
lexical over the accepted naming layer; it is never a similarity score.

## playbill expand

~~~text
cruxible playbill expand ARTIFACT_PATH [--facet NAME]... [--evaluation-time TS]
~~~

Returns one bounded context capsule for an accepted address. Repeat --facet to
narrow what the capsule carries.

## playbill floor

~~~text
cruxible playbill floor export --output DIR [--force]
~~~

Writes the deterministic greppable floor of accepted state. The daemon returns
bytes keyed by floor path and never writes a client path; export refuses a
non-empty output directory unless --force is given. The export carries its own
coverage boundary in `coverage-manifest.json`, enumerated in the root manifest
like every other floor file.

## playbill native

~~~text
cruxible playbill native render --output DIR
  [--evaluation-time ISO-8601] [--discard]
cruxible playbill native status DIR
~~~

The ledger is the semantic object store; the rendered directory is its editable
working tree. `render` checks out accepted knowledge as browsable Markdown --
one page per Subject with its Claims grouped by predicate, plus pages for
ClaimTypes, QueryDefinitions, and Documents, a `README.md` orientation floor,
and a `render-manifest.json` holding the baseline every field is compared
against. The render is a pure function of accepted state and an explicit render
context, so the same generation and the same read time always produce identical
bytes; the CLI supplies the read time, and the renderer never reads a clock.

Fields are typed. Statement values and qualifiers are editable and free-form
inside themselves; verdict, provenance, and coverage blocks are derived, always
rendered generation- and time-qualified, and regenerate rather than accept an
edit. Editing changes nothing accepted: compile is a separate gate.

`render` writes the bytes -- the daemon never writes into a repository -- and
refuses to overwrite an editable field you have edited unless you pass
`--discard`, naming every dirty field it would otherwise have lost.

`status` compares a rendered directory against its own baseline and needs no
daemon:

~~~text
      clean  subjects/project.work_item/wi-43.md  8 region(s): 8 clean, 0 dirty, ...
      dirty  subjects/project.work_item/wi-42.md  8 region(s): 7 clean, 1 dirty, ...
Playbill coverage: 1 exact, 2 drifted, 3 candidates, 1 none
invalidated derived fields: 2 beside 1 edited statement(s); no governance fact
reaches the edited material
~~~

The invalidation half of that answer comes from the coverage resolver, not from
`status` itself: a rendered file is a working source, the render baseline is what
accepted state said its bytes were at that generation, and the drift the resolver
reports is what invalidates the verdict and provenance blocks beside an edited
field. No card it returns can grant the edited material a governance fact.

Deleting a rendered file or directory loses uncompiled local edits and nothing
else; removal is never inferred as retirement.

The render format is deliberately experimental through dogfood. The typed
editable/derived split and the round-trip laws are the contract; the Markdown
spellings and the invisible marker channel are versioned by the lens and
expected to change.

## playbill coverage

~~~text
cruxible playbill coverage resolve
  [--bind PATH=PLANE:IDENTITY]... [--bindings FILE] [--root DIR]
  [--file PATH]... [--range PATH:START-END]...
  [--grep-results FILE] [--all]
cruxible playbill coverage status
  [--bind PATH=PLANE:IDENTITY]... [--bindings FILE] [--root DIR]
~~~

resolve answers what the working files you just read or changed have to do with
accepted state. Every working path is bound to a logical source by an explicit
declaration -- coverage never infers a binding from a filename, because
identical bytes in another file are precisely not the same source. The CLI reads
and hashes the bytes locally; the daemon reads no client filesystem.

Governed spans are annotated inline in card order. Ungoverned results are
summarized once per operation, never one line per result:

~~~text
Playbill coverage: 2 exact, 1 drifted, 3 candidates, 41 none
coverage complete for 47 returned spans at generation gen-sha256:...
omitted cards: 0, truncated spans: 0
~~~

A `none` is factual only inside a complete boundary, so a span whose health is
`partial`, `stale`, `denied`, or `unavailable` prints that health and its reason
codes rather than reading as an absence.

status renders the coverage manifest over the whole declared scope: epoch,
health, completeness, and the sources a `none` would have been factual inside.

Resolving coverage changes no accepted state and appends no receipt.

## playbill proposal

~~~text
cruxible playbill proposal inspect PROPOSAL_ID
cruxible playbill proposal refusal PROPOSAL_ID
cruxible playbill proposal review PROPOSAL_ID [--include-body|--redacted]
cruxible playbill proposal approve PROPOSAL_ID
  --signer-id ID --key FILE [--yes]
cruxible playbill proposal activate PROPOSAL_ID
~~~

approve signs locally. The private-key path is not sent to the daemon.

## playbill principal

~~~text
cruxible playbill principal list
cruxible playbill principal rotate ...
cruxible playbill principal revoke ...
cruxible playbill principal recover ...
~~~

Rotation, revocation, and recovery are governed principal-change proposals.
Recovery cannot approve ordinary Document candidates.

## playbill sources

~~~text
cruxible playbill sources check ...
cruxible playbill sources compile ...
cruxible playbill sources propose ...
~~~

Compilation reads declared local files client-side and emits a path-free bundle.
The daemon never reads a submitted client path.

## playbill explain

~~~text
cruxible playbill explain IDENTITY
  [--detail summary|evidence|proof]
  [--include-body]
~~~

summary and evidence are implemented. proof is reserved and returns a typed
unsupported-detail result.

Use --json on operation commands for machine-readable output. Run any command
with --help for its exact options.
