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
non-empty output directory unless --force is given.

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
