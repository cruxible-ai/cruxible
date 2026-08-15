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
