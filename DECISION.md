# DECISION: wi-snapshot-clone-credential-lockout

## Problem

On an auth-enabled daemon, `POST /api/v1/{instance_id}/snapshots/clone` minted a
new governed instance with **zero** runtime credentials. Runtime credentials are
scoped to exactly one instance (docs/runtime-auth-and-agent-roles.md), so the
source instance's ADMIN credential cannot reach the clone; the daemon's one-time
bootstrap secret is typically already claimed; and offline `recover-admin`
refuses instances with no prior ADMIN row. The clone was unreachable through
normal auth.

## Chosen mechanism: (a) one-time ADMIN credential in the clone response

When `is_server_auth_enabled()`, the clone route now mints the clone's initial
ADMIN runtime credential (label `clone-admin`, mirroring `bootstrap-admin`) and
returns it once in the response as `CloneSnapshotResult.admin_credential`
(reusing the `RuntimeCredentialBootstrapResult` contract shape: `credential_id`,
`instance_id`, `permission_mode`, `token`). Flow matches the claim-bootstrap and
mint conventions exactly: `prepare_credential` → `materialize_auth_managed_entities`
→ `commit_prepared_credential`; only the SHA-256 hash is stored; the plaintext
appears exactly once, in the creation response. `created_by` records the calling
credential's principal id. Auth-disabled daemons are unchanged
(`admin_credential` is `null`, no rows minted, `auth_required` not flipped).

No privilege escalation: `cruxible_clone_snapshot` already requires ADMIN on the
source, and the clone is a copy of data the caller fully controls.

### Alternatives rejected

- **(b) Re-scope/copy the creating credential to the clone.** Re-scoping bricks
  the caller's access to the source (a credential is scoped to exactly one
  instance — moving it swaps one lockout for another). Copying would require the
  same `token_hash` under two instance ids, which the schema forbids
  (`token_hash UNIQUE`) and which would make `authenticate()` ambiguous. Either
  variant weakens the one-credential-one-instance invariant that the whole scope
  model (and `require_unscoped_operator`) is built on.
- **(c) Refuse authenticated clones without an explicit credential strategy.**
  Restores the invariant vacuously by deleting the feature on exactly the
  deployments (auth-on) that Cloud/hosted care about, and any eventual
  re-enabling still needs (a)'s machinery. Kept only as the implicit behavior
  for paths that remain unreachable on auth-enabled daemons (see audit).

## Audit: instance-minting paths on auth-ENABLED daemons

| Path | Entry point | Verdict | Why |
|---|---|---|---|
| Snapshot clone | `POST /{id}/snapshots/clone` → `api.clone_snapshot_governed` | **Was the hole — FIXED here** | New instance id, no credential story before this change. |
| Bootstrap / hosted init | `POST /runtime/instances` → `api.init_hosted_instance` | Covered | Reachable only by the **unclaimed** bootstrap secret (middleware gate) or auth-off; an instance-scoped credential fails `check_permission` scope on the new id. Claimant exchanges the secret at `POST /{id}/runtime/bootstrap/claim` for the one-time ADMIN token. |
| Init / reload | `POST /instances` → `api.init_governed` | Not reachable (for minting) | A scoped credential may only re-init its **own** workspace root (`authorize_governed_instance_lifecycle` raises `InstanceScopeError` for unknown roots); the bootstrap secret is not accepted as a bearer on this route; no bearer → 401. New-instance creation on auth-on daemons goes through hosted init. |
| `state create-overlay` | `POST /states/overlays` → `api.create_state_overlay_governed` | Not reachable | `check_permission("cruxible_state_create_overlay", instance_id=root_dir)` compares the credential's `inst_…` scope against a workspace **path** — always a mismatch → `InstanceScopeError`; the bootstrap secret bearer is not accepted here either. Auth-on overlay creation is served by hosted init with `source_type=state` (covered above). **If this route is ever opened to authenticated callers it needs the same mint applied — that requires its own authz design (who may create overlays?), so documented here, not fixed.** |
| `state pull-apply` | `POST /{id}/state/pull/apply` → `api.state_pull_apply` | Covered / N/A | Applies the upstream release **into the existing overlay instance** (same `instance_id`; it takes a pre-pull snapshot, mints no instance id). "Pull-apply into a fresh instance" does not exist as a code path — fresh overlays come from hosted init / create-overlay. |
| Instance restore | `POST /instances/restore` → `api.restore_instance` | Covered for same-daemon; **documented gap for cross-daemon migration** | Same-identity restore preserves `manifest.instance_id`, so existing credential rows (keyed by instance id) keep working. Gated to the unscoped operator (`require_unscoped_operator`; bootstrap-secret bearer allowed via `_SERVER_OPERATION_ROUTES`). BUT restoring an artifact from **another** daemon lands an instance id with no rows in the local credential DB → same lockout shape. Not fixed here because it is not the identical mechanism: the mint would have to be conditional (only when the instance id has no active credentials — unconditional minting would hand out an extra ADMIN token on every routine same-daemon restore) and the recipient is the daemon operator, not an instance ADMIN; that conditional + recipient question is its own design. Mitigating hatch: the restore caller is by definition the daemon operator, who can restart the daemon with a **new** bootstrap secret and claim the restored instance (claims are keyed by secret hash and per-instance prior-ADMIN, so a fresh secret claims cleanly). |
| Instance relocate / backup | `POST /{id}/instance/relocate`, `/{id}/instance/backup` | Covered / N/A | Same identity preserved; no new instance id. |
| Local clone / local registry | `api.clone_snapshot_local`, CLI `service_clone_snapshot` fallback | Not reachable | Embedded/local (non-daemon) execution only; daemon auth does not apply. |

## Residual risks (flagged for review)

1. **Non-atomic window.** The clone instance is created and registered before the
   credential commit. If `materialize_auth_managed_entities` or the store commit
   fails, the clone exists credential-less (the original lockout, for that clone
   only). No rollback was added: the registry has no delete API and the
   smallest-change constraint ruled out building one. Escape hatch: the operator
   re-keys the bootstrap secret and claims the clone (per-instance claim
   validation permits it because the clone has no prior ADMIN). The same window
   exists in the pre-existing mint/rotate routes.
2. **Token in transport/transcripts.** The plaintext token rides the clone HTTP
   response and therefore MCP tool output and the CLI echo. This is the same
   exposure class as claim-bootstrap and `credential mint` (one-time delivery is
   the design); request logging records metadata, not bodies.
3. `commit_prepared_credential` is called with a new audit reason string
   `runtime_clone_credential_created` (stored in `runtime_auth_state.reason`),
   alongside the existing `runtime_credential_created` / `runtime_bootstrap_claimed`
   / `runtime_credential_rotated` / `runtime_credential_recovered` vocabulary.
