# Data and network

What Cruxible 0.5 reads, writes and connects to, for a security or procurement
review. Each statement cites the code it comes from. **[unverified]** marks
anything that was not confirmed in code.

Source prefixes:

| Prefix | Source |
|---|---|
| `core:` | `src/cruxible_core/` in this repository |
| `client:` | `packages/cruxible-client/src/cruxible_client/` in this repository |
| `runtime:` | the `cruxible_provider_runtime` package, version 0.2.0 (the version `uv.lock` pins; the cited lines are the same in 0.4.0, which the security kit bundles) |
| `kit:` | `kits/security/source/` in the [cruxible-kits](https://github.com/cruxible-ai/cruxible-kits) repository |
| `osv:` | `security_osv_provider/osv.py` inside `kits/security/providers/security_osv_provider-0.1.0.dev0-py3-none-any.whl` in the same repository |

Line numbers were checked against the source before the 0.5.0 tag. They can
drift in later releases.

## Files it writes

| What | Where | Protection | Source |
|---|---|---|---|
| Daemon state root | `~/.cruxible`, or `$CRUXIBLE_STATE_ROOT` | refuses temporary directories (`/tmp`, `/var/tmp`, `/private/var/folders`) | client:contracts/workspace_layout.py:89-99; core:server/config.py:16-22 |
| Instance registry | `<state root>/daemon/registry.db` (SQLite) | | core:server/registry.py:108 |
| Per-instance data (git ledger, projections, content store, logs, credentials, leases) | `<state root>/instances/<id>/` | the instance directory and its subdirectories are created 0700; **[unverified]** that every file inside is 0600 | core:server/registry.py:308; client:contracts/types.py:110-118; core:runtime/instance.py:611-623; core:governance/keys.py:82 |
| Daemon signing key (Ed25519, **not encrypted**) | `<instance>/credentials/daemon_ed25519` | 0600; the daemon refuses to load it if group or world can read it | core:governance/keys.py:20-21, 99-103, 134; core:runtime/instance.py:747-766 |
| API tokens | `<state root>/daemon/runtime_credentials.db` | stored as SHA-256 hashes, compared in constant time | core:server/credentials.py:119, 176, 580 |
| Bootstrap secret (auth on only) | `<state root>/daemon/` | 0600, never printed; removed when auth is off | core:server/bootstrap_secret.py:42-45, 66-95 |
| Provider secrets (if an operator stores any) | `<state root>/daemon/provider-secrets/` | directories 0700, files 0600, **plaintext at rest** | core:runtime/provider_runtime.py:411-418; core:providers/provider_local_runtime.py:455-478 |
| Request log | `<state root>/daemon/logs/server.log` (and `fatal.log`), rotated at 10 MiB, 5 files kept | each request logs method, route template, status, credential ID, label and type, principal ID, role, instance scope and ID, operation ID; no token and no body | core:server/config.py:208-228; core:server/request_logging.py:18-19, 174-188, 236-241 |
| Principal (agent or person) key and settings | `~/.config/cruxible/keys/<instance>/<principal>/` (`$XDG_CONFIG_HOME` honoured) | key unencrypted, 0600; `cruxible.env` holds the **bearer token in plaintext**, 0600 | core:cli/principal_settings.py:39-53, 83-87, 107-117; core:governance/keys.py:134 |
| Workspace directory | `<your repo>/.cruxible/` (floor export, `world.pyi` stub) | refuses `$HOME` and any path inside the state root | client:contracts/workspace_layout.py:23-26, 109-125 |
| Login service (only after `cruxible server install-service`) | `~/Library/LaunchAgents/*.plist` or `~/.config/systemd/user/cruxible.service` | holds no secrets, only `CRUXIBLE_SERVER_AUTH=true/false` | core:server/service_install.py:68-74, 94-124 |
| Provider environments | under the state root, built with `uv` | offline: `uv pip sync --no-index`, `uv build --offline` | runtime:backends.py:272-285; core:service/procedures/provider_installation.py:324-325 |

## What it listens on

- **Default: a Unix socket** at `<state root>/run/daemon.sock`, bound 0600
  inside an owner-only directory the user owns (core:mcp/daemon.py:59;
  core:server/app.py:495-497, 601-604).
- **Auth is off by default on a socket.** Any process running as the same OS
  user can then act as any principal. The daemon prints this when it starts
  (core:server/config.py:436-439, 456-463; core:server/app.py:758).
  [SECURITY.md](https://github.com/cruxible-ai/cruxible/blob/main/SECURITY.md#trust-boundary)
  states this as the trust boundary.
- **TCP is opt-in** (`--host/--port`, default `127.0.0.1:8100`). The daemon
  **refuses to start on TCP without auth** (core:server/config.py:441-453;
  core:server/app.py:793-795). With auth on, it binds whatever host it is
  given. It serves **plain HTTP and has no TLS option** (core:server/app.py:795;
  no ssl or certificate argument anywhere in core:server).
- **Browser protection:** a request with a non-loopback `Origin` header is
  refused unless that origin is in `CRUXIBLE_ORIGIN_ALLOWLIST`. This blocks DNS
  rebinding and hostile web pages (core:server/config.py:364-391).
- **MCP server** (`cruxible mcp`) talks over **stdio only**
  (core:mcp/server.py:280). If no daemon is running, it starts one on the
  default socket with an admin ceiling. The daemon gets an allowlisted
  environment, so API keys and tokens stay with the MCP process
  (core:mcp/daemon.py:341-352, 435-444).

## Outbound network

**The `cruxible` package makes no outbound calls unless an operator or agent
asks for one of the actions below.** The source has no telemetry, analytics, crash reporting or
update pings: a word search for sentry, posthog, analytics, opentelemetry and
datadog finds nothing. The `telemetry summary` counters from 0.3.2 are no
longer in the source, and they only ever counted locally
(CHANGELOG.md:1669-1674).

| Destination | Triggered by | What is sent | Source |
|---|---|---|---|
| `pypi.org/simple`, `files.pythonhosted.org` (or operator-set `provider_index_urls`) | ADMIN `provider install`, which fetches dependency wheels | the package names and versions requested; files are checked against the index's sha256; no redirects followed; 60 s timeout; 512 MiB cap | core:providers/package_index.py:32, 176-181; core:providers/package_materialization.py:113-138; core:service/procedures/provider_installation.py:207-208 |
| `ghcr.io/cruxible-ai/kits` (or any OCI registry named) | `kit add <registry ref>`; `kit status`, which checks for newer tags with a 5 s timeout unless `--offline` | the kit name; registry credentials go only to the host named in `CRUXIBLE_REGISTRY` | client:kits.py:401-414, 498, 520-558; client:artifacts/registry.py:33, 140-150 |
| An operator-configured git remote | ledger mirror publication, only if a mirror is bound | the accepted ledger refs (all governed state); the token goes to git as an HTTPS header through the environment, never on the command line | core:ledger/git.py:1186-1201; core:ledger/ledger_mirror.py:57-84 |
| A provider's declared endpoints | a Procedure, Line or Runbook run that calls a provider | whatever that provider sends (below) | core:providers/provider_local_runtime.py:919-931 |

**On a local machine, provider traffic is recorded, not contained.** The
runtime records the endpoints a provider's instrumented client contacts in the
run's receipt, but a local provider runs with the operator's privileges and
could open a connection the runtime never sees. Only the hosted backend blocks
undeclared traffic (runtime:egress.py:1-25).

### Security kit providers

Once the kit's automation is accepted, a daily trigger at 14:00 UTC runs these
(kit:pilot/template.py:57-61, 108-111).

| Provider | Destination | What leaves the machine | Source |
|---|---|---|---|
| OSV (`osv.query`) | `https://api.osv.dev` (`POST /v1/querybatch`, `GET /v1/vulns/{id}`) | **every versioned package URL in your SBOM, internal package names included**; distro packages and unversioned ones are skipped | osv:66, 93-106, 181-197, 228 |
| CISA KEV (`web.fetch`) | `https://www.cisa.gov/.../known_exploited_vulnerabilities.json` | a plain GET | kit:pilot/definitions.py:62, 782 |
| FIRST EPSS (`web.fetch.pages`) | `https://api.first.org/data/v1/epss?cve=...` | the CVE IDs on your watch list | kit:pilot/definitions.py:63, 605-613, 783-790 |
| Web fetch (any kit or Procedure) | whatever URL the run input names (`dynamic:target-from-run-input`); redirects followed up to 20 hops, to http or https only | User-Agent `cruxible-provider-web/0.1 (+https://cruxible.ai)`; **no block on private, loopback or cloud-metadata addresses** | kit:providers/cruxible-provider-web/src/cruxible_provider_web/http.py:52-54, 201-202; manifest.yaml:33-34 |
| SARIF import | none (reads a local file) | nothing | `declared_endpoints: []` in its manifest |

## Credentials it handles

- **Daemon and principal Ed25519 keys**: files, unencrypted, 0600 (see the
  table above). The OS keychain is not used.
- **Bearer tokens**: random, with a `cruxible_...` prefix. The daemon keeps
  only a hash. Client copies sit in plaintext in `cruxible.env` or in
  `CRUXIBLE_SERVER_BEARER_TOKEN` (core:server/credentials.py:118-127;
  client:authoring/principal_settings.py:20).
- **Provider secrets**: read from `CRUXIBLE_PROVIDER_SECRET_*` environment
  variables or the plaintext file store. They reach the provider process
  through an inherited pipe, not its environment or command line
  (core:providers/provider_local_runtime.py:429-452, 1270-1282).
- **Registry and mirror tokens**: environment only
  (`CRUXIBLE_REGISTRY_PASSWORD`, `CRUXIBLE_MIRROR_TOKEN`)
  (client:artifacts/registry.py:140-150; client:contracts/ledger_mirror.py:18).

## What an agent can do through MCP, by tier

`CRUXIBLE_MODE` sets the process ceiling. **When it is unset, the default is
ADMIN**, unless `CRUXIBLE_DEFAULT_READ_ONLY=1` (core:runtime/permissions.py:229-283).
A credential can narrow the ceiling but never raise it
(core:runtime/permissions.py:1-6). The tool table is at
core:runtime/permissions.py:76-172.

| Tier | Tools | What it allows |
|---|---|---|
| READ_ONLY | 37 | reads, queries, audit, kit status. **A read-only caller can also run observe-only Lines, Procedures and Runbooks, which can call providers and so reach the network**, as long as the daemon's ceiling is above read-only; a daemon whose ceiling is read-only refuses every run (core:service/procedures/procedure_runs.py:422-450) |
| GOVERNED_WRITE | +36 | for example: propose claims, documents, contracts and procedures; store bodies; attest; add or remove kits; any run that proposes or settles |
| GRAPH_WRITE | +5 | approve, reject or activate proposals; erase a captured body |
| ADMIN | +4 | install or retire providers (fetches and builds code), upgrade the compiler, propose principals |

**[unverified]** Whether a read-only run whose provider fails still sends the
request was not exercised; only the code path was read.
