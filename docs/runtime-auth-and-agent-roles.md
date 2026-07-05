# Runtime Auth And Agent Roles

Cruxible can run as a local library, but agent workflows that rely on review
gates should run through an authenticated daemon. The daemon owns state and
credentials. Agents use scoped runtime credentials to read, write, propose, or
review state through Cruxible APIs.

## What Auth Protects

Review gates only matter if Cruxible can distinguish the actor doing the work
from the actor approving it. A writer agent must not be able to approve its own
work by sending a request body that claims to be the reviewer.

The core rule is:

> Authentication chooses the actor. Request payloads may carry correlation
> context, but they may not choose identity.

Use authenticated daemon mode when:

- multiple agents collaborate on the same state;
- one agent writes state and another reviews it;
- mutation guards depend on actor identity;
- the state directory should stay outside the agent workspace;
- a hosted or long-lived runtime is being exercised.

Unauthenticated local mode is suitable only for single-user scratch work.

## Bootstrap Flow

Start the daemon with auth enabled and a one-time bootstrap secret:

```bash
CRUXIBLE_SERVER_AUTH=true
CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=<one-time-secret>
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/server" \
  cruxible server start
```

The first trusted operator claims the bootstrap secret for the target instance:

```text
POST /api/v1/{instance_id}/runtime/bootstrap/claim
{ "bootstrap_secret": "..." }
```

The daemon returns one plaintext `ADMIN` runtime credential token. Store it in
a secret manager or local operator-owned file outside the agent session. For
local dogfooding, use a file with restrictive permissions such as:

```bash
# ~/.cruxible/auth/agent-operation-admin.env
export CRUXIBLE_SERVER_URL=http://127.0.0.1:8100
export CRUXIBLE_INSTANCE_ID=inst_...
export CRUXIBLE_SERVER_BEARER_TOKEN=<admin-runtime-token>
```

The bootstrap secret cannot be claimed again.

Use the admin credential to create narrower credentials for agents and humans:

```text
POST /api/v1/{instance_id}/runtime/credentials
{
  "label": "writer-agent",
  "permission_mode": "graph_write"
}
```

Runtime credential tokens are stored server-side as hashes. Plaintext token
material is returned only when a credential is created, rotated, or bootstrap is
claimed.

## Credential Custody

Runtime credentials are bearer secrets. Any process that can read a token can
exercise that token's permissions. Treat them like passwords, API keys, or SSH
private keys:

- do not paste tokens into prompts, tickets, logs, or shared documents;
- do not put broad admin credentials in ordinary agent sessions;
- do not give one agent session both writer and reviewer tokens if independent
  review matters;
- revoke or rotate tokens when a session ends or a token may have leaked.

Cruxible enforces the identity and permission of the token presented on each
request. It cannot prevent a local process from using another token that the
operating system allows that process to read. Strong role separation therefore
requires credential custody outside Cruxible: separate OS users, shell sessions,
keychains, password managers, containers, VMs, or hosted user accounts.

## Agent Environment

Agents should not pass bearer tokens on every individual command. Start the
agent or MCP process with its role token in the environment:

```bash
export CRUXIBLE_SERVER_URL=http://127.0.0.1:8100
export CRUXIBLE_INSTANCE_ID=inst_...
export CRUXIBLE_SERVER_BEARER_TOKEN=<agent-runtime-token>
```

Then server-mode CLI, MCP, and client calls can reuse that credential without
printing it in prompts, shell history, or logs.

## Actor Identity

For runtime credentials, Cruxible derives actor identity from the credential:

- `actor_type`: `service_account`
- `actor_id`: runtime credential label
- `org_id`: instance ID
- `operation_id`: generated per request

If a request supplies `actor_context`, it must match the authenticated runtime
credential identity. Request payloads may preserve correlation context such as
`request_id`, but they cannot change `actor_type`, `actor_id`, or `org_id`.

This blocks the unsafe pattern:

```text
writer-agent token + actor_context.actor_id = "reviewer"
```

Mutation guards that check actor identity should use this credential-derived
actor context.

## Agent Role Pattern

For a review-gated agent workflow, create separate credentials for each role:

- `admin`: bootstrap, credential rotation, and operator maintenance
- `writer-agent`: normal graph writes and proposal creation
- `reviewer-agent`: review decisions and guarded approvals
- `human-reviewer`: optional human approval path

Keep the writer and reviewer credentials separate even on a local machine. If
one agent holds both tokens, Cruxible can no longer enforce that the reviewer is
independent from the writer.

Keep the admin credential separate from normal writer/reviewer agent sessions.
An admin token can mint, revoke, and rotate other runtime credentials, so
exposing it to an ordinary agent collapses the local role boundary.

Treat agent credentials as disposable. If a Codex or Claude session closes and
loses its token, use the stored operator/admin credential to mint a replacement
role credential and optionally revoke the old one. Do not restart the daemon
without auth to work around a lost agent token.

## Restart Discipline

For persistent agent-operated instances, treat auth as sticky operational
state. If a daemon has been started with auth for a state directory, restart it
with auth enabled for that same state directory.

Cruxible records this requirement in server state. Normal startup should fail
if that state directory has previously required auth but the daemon is started
without `CRUXIBLE_SERVER_AUTH=true`.

Do not restart a review-gated daemon without auth just because the process is
unresponsive. Restart scripts and supervisor configs should preserve:

- `CRUXIBLE_SERVER_AUTH=true`
- the same `CRUXIBLE_SERVER_STATE_DIR`
- the runtime credential store
- any bootstrap or secret-manager wiring needed for recovery

If you intentionally need unauthenticated scratch mode, use a separate state
directory.

## Local Boundary

Local auth is a product boundary, not a hardened OS sandbox. A local machine
owner can still intervene out of band by changing process environment, state
files, or databases. That is acceptable for local recovery.

The intended boundary is that normal Cruxible API calls preserve credential
identity and permission mode once auth is on. Stronger isolation requires a
separate user, VM, container boundary, or hosted runtime.

Local users remain responsible for token custody. If a single process can read
multiple role tokens, it can choose any of those roles. Cruxible will still
record which credential acted, reject request-body identity spoofing, and apply
permission checks, but it cannot make readable bearer secrets unusable.

## Recovering Access

If every admin runtime token for a local server state directory is lost, stop the
daemon before attempting recovery. Local recovery treats filesystem ownership of
the server state directory and its `runtime_credentials.db` as the root of trust.
It is not a network operation and does not weaken server auth.

Run recovery directly against the stopped daemon's state dir:

```bash
cruxible credential recover-admin --state-dir "$HOME/.cruxible/server"
```

The command verifies that the invoking uid owns both the state dir and
`runtime_credentials.db`, takes a SQLite `BEGIN IMMEDIATE` lock, mints one new
`ADMIN` credential, records a recovery audit event, and prints the plaintext
token once.

Stop the daemon yourself before running recovery. The lock check is
best-effort only: it refuses when another connection is mid-write, but a
running daemon that is idle holds no SQLite lock and will NOT be detected.
Recovery against a live daemon does not corrupt state (credentials are read
fresh on every request), but the operator — not the lock — is the guarantee
that nothing else is serving the state dir.

After recovery, restart the daemon with auth enabled and use the new admin token
to mint, rotate, or revoke credentials. Existing admin credentials are not
revoked automatically; if the old token should no longer work, revoke it after
you regain access.
