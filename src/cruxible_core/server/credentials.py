"""Server-side runtime credential storage."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_client.contracts.runtime_credentials import (
    RUNTIME_CREDENTIAL_PROOF_MAX_SKEW_SECONDS,
)
from cruxible_client.contracts.temporal import format_datetime, parse_datetime, utc_now
from cruxible_core.errors import (
    BootstrapClaimRefusedError,
    InstanceNotFoundError,
    PrincipalRefusedError,
    RuntimeCredentialNotFoundError,
)
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry
from cruxible_core.storage.preview_fence import refuse_write_while_previewing

_TOKEN_PREFIX = "crt"
_TOKEN_SECRET_BYTES = 32


@dataclass(frozen=True)
class RuntimeCredentialRecord:
    """Stored runtime credential metadata without plaintext token material."""

    credential_id: str
    instance_id: str
    label: str
    permission_mode: PermissionMode
    token_hash: str
    created_at: str
    created_by: str | None = None
    revoked_at: str | None = None
    # The principal this credential acts as; the label is a description only.
    # None for an unbound operator credential (bootstrap claim, local recovery,
    # or any row minted before this column existed): transport authority only.
    principal_id: str | None = None


@dataclass(frozen=True)
class CreatedRuntimeCredential:
    """Runtime credential creation result with one-time plaintext token."""

    record: RuntimeCredentialRecord
    token: str


class RuntimeCredentialRecoveryBusyError(RuntimeError):
    """Raised when offline credential recovery cannot lock the credentials DB."""


class RuntimeCredentialRecoveryError(RuntimeError):
    """Raised when offline credential recovery cannot safely target an instance."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_credential_id() -> str:
    return new_id("rcred", length=16, separator="_")


def _new_token(credential_id: str) -> str:
    return f"{_TOKEN_PREFIX}_{credential_id}_{secrets.token_urlsafe(_TOKEN_SECRET_BYTES)}"


def _serialize_permission_mode(permission_mode: PermissionMode) -> str:
    return str(permission_mode.name).lower()


def _parse_permission_mode(value: str) -> PermissionMode:
    return PermissionMode[value.upper()]


def _validate_governed_instance_id(instance_id: str) -> None:
    record = get_registry().get(instance_id)
    if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
        raise InstanceNotFoundError(instance_id)


def _proof_replayed() -> PrincipalRefusedError:
    return PrincipalRefusedError(
        "runtime_credential.principal_proof_replayed",
        "this signed consent already minted a credential; repair: sign a fresh "
        "one with `cruxible credential mint --principal-id ID --key-dir DIR`",
        repair=RepairOperationV1(operation="credential.mint"),
    )


class RuntimeCredentialStore:
    """SQLite-backed store for instance-scoped runtime bearer credentials."""

    def __init__(self, db_path: Path, *, initialize: bool = True) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._init_db()

    def _connect(self, *, timeout: float = 5.0) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_credentials (
                    credential_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    permission_mode TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    created_by TEXT,
                    revoked_at TEXT
                )
                """
            )
            self._ensure_principal_column_conn(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_credentials_instance
                ON runtime_credentials(instance_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_credential_proofs (
                    proof_digest TEXT PRIMARY KEY,
                    credential_id TEXT NOT NULL,
                    used_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_bootstrap_claims (
                    bootstrap_secret_hash TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    credential_id TEXT NOT NULL UNIQUE,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (bootstrap_secret_hash, instance_id)
                )
                """
            )
            self._ensure_bootstrap_claims_per_host_conn(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_bootstrap_claims_instance
                ON runtime_bootstrap_claims(instance_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_auth_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reason TEXT
                )
                """
            )
            self._ensure_recovery_events_table_conn(conn)

    def prepare_credential(
        self,
        *,
        instance_id: str,
        label: str,
        permission_mode: PermissionMode = PermissionMode.ADMIN,
        created_by: str | None = None,
        principal_id: str | None = None,
    ) -> CreatedRuntimeCredential:
        """Prepare an instance-scoped credential without committing it."""
        _validate_governed_instance_id(instance_id)
        return self._new_created_credential(
            instance_id=instance_id,
            label=label,
            permission_mode=permission_mode,
            created_by=created_by,
            principal_id=principal_id,
        )

    def commit_prepared_credential(
        self,
        created: CreatedRuntimeCredential,
        *,
        reason: str = "runtime_credential_created",
        proof_digest: str | None = None,
    ) -> CreatedRuntimeCredential:
        """Commit a prepared credential after caller-side materialization succeeds.

        ``proof_digest`` names the principal's signed consent this credential
        consumed; recording it in the same transaction makes that consent
        single-use.
        """
        refuse_write_while_previewing("runtime credential store")
        _validate_governed_instance_id(created.record.instance_id)
        try:
            with self._connect() as conn:
                if proof_digest is not None:
                    self._spend_proof_conn(conn, proof_digest, created.record)
                self._mark_auth_required_conn(
                    conn,
                    updated_at=created.record.created_at,
                    reason=reason,
                )
                self._insert_credential_conn(conn, created.record)
        except sqlite3.IntegrityError as exc:
            if proof_digest is None:
                raise
            raise _proof_replayed() from exc
        return created

    def create_credential(
        self,
        *,
        instance_id: str,
        label: str,
        permission_mode: PermissionMode = PermissionMode.ADMIN,
        created_by: str | None = None,
        principal_id: str | None = None,
        proof_digest: str | None = None,
    ) -> CreatedRuntimeCredential:
        """Create an instance-scoped credential and return its token once."""
        created = self.prepare_credential(
            instance_id=instance_id,
            label=label,
            permission_mode=permission_mode,
            created_by=created_by,
            principal_id=principal_id,
        )
        return self.commit_prepared_credential(created, proof_digest=proof_digest)

    def proof_spent(self, proof_digest: str) -> bool:
        """Whether this signed consent already minted a credential (a read)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runtime_credential_proofs WHERE proof_digest = ?", (proof_digest,)
            ).fetchone()
        return row is not None

    def revoke_credentials_of_principal(self, *, instance_id: str, principal_id: str) -> int:
        """Revoke every active credential that acts as ``principal_id``; return the count."""
        refuse_write_while_previewing("runtime credential store")
        revoked_at = format_datetime(utc_now())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runtime_credentials
                SET revoked_at = ?
                WHERE instance_id = ? AND principal_id = ? AND revoked_at IS NULL
                """,
                (revoked_at, instance_id, principal_id),
            )
        return int(cursor.rowcount)

    def recover_admin_credential(
        self,
        *,
        instance_id: str,
        label: str,
        uid: int,
        hostname: str,
    ) -> CreatedRuntimeCredential:
        """Create a local offline ADMIN recovery credential plus audit row.

        This path intentionally does not call the instance registry. Offline
        recovery is rooted in local ownership of the server state directory and
        runtime credentials DB while the daemon is stopped, so the credentials DB
        itself is the targeting source of truth. Token generation, hashing, auth
        state, and credential insertion still use the same store helpers as the
        normal mint path.
        """
        refuse_write_while_previewing("runtime credential store")
        created = self._new_created_credential(
            instance_id=instance_id,
            label=label,
            permission_mode=PermissionMode.ADMIN,
            created_by="local_recovery",
        )
        conn = self._connect(timeout=0.0)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if _is_sqlite_busy(exc):
                    raise RuntimeCredentialRecoveryBusyError(
                        "Runtime credentials DB is locked. Stop the Cruxible daemon "
                        "serving this state dir before running recover-admin."
                    ) from exc
                raise RuntimeCredentialRecoveryError(
                    f"Could not write runtime credentials DB at {self.db_path}: {exc}"
                ) from exc

            self._ensure_principal_column_conn(conn)
            self._validate_recovery_target_conn(conn, instance_id)
            self._ensure_recovery_events_table_conn(conn)
            self._mark_auth_required_conn(
                conn,
                updated_at=created.record.created_at,
                reason="runtime_credential_recovered",
            )
            self._insert_credential_conn(conn, created.record)
            conn.execute(
                """
                INSERT INTO runtime_recovery_events(
                    created_at,
                    instance_id,
                    credential_id,
                    uid,
                    hostname
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    created.record.created_at,
                    created.record.instance_id,
                    created.record.credential_id,
                    uid,
                    hostname,
                ),
            )
            created_row = self._fetch_record_row(
                conn,
                created.record.instance_id,
                created.record.credential_id,
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

        assert created_row is not None
        return CreatedRuntimeCredential(
            record=self._row_to_record(created_row),
            token=created.token,
        )

    def prepare_bootstrap_credential(
        self,
        *,
        instance_id: str,
        bootstrap_secret: str,
        expected_bootstrap_secret: str | None,
    ) -> CreatedRuntimeCredential:
        """Prepare the initial ADMIN credential without committing it."""
        _validate_governed_instance_id(instance_id)
        if expected_bootstrap_secret is None or not hmac.compare_digest(
            bootstrap_secret,
            expected_bootstrap_secret,
        ):
            raise BootstrapClaimRefusedError(
                "runtime_bootstrap.secret_invalid", instance_id=instance_id
            )

        bootstrap_secret_hash = _hash_token(bootstrap_secret)
        with self._connect() as conn:
            self._validate_bootstrap_claim_conn(conn, instance_id, bootstrap_secret_hash)

        return self._new_created_credential(
            instance_id=instance_id,
            label="bootstrap-admin",
            permission_mode=PermissionMode.ADMIN,
            created_by="runtime_bootstrap",
        )

    def claim_prepared_bootstrap_credential(
        self,
        created: CreatedRuntimeCredential,
        *,
        bootstrap_secret: str,
    ) -> CreatedRuntimeCredential:
        """Commit a prepared bootstrap credential after materialization succeeds."""
        refuse_write_while_previewing("runtime credential store")
        _validate_governed_instance_id(created.record.instance_id)
        bootstrap_secret_hash = _hash_token(bootstrap_secret)
        try:
            with self._connect() as conn:
                self._validate_bootstrap_claim_conn(
                    conn,
                    created.record.instance_id,
                    bootstrap_secret_hash,
                )
                self._mark_auth_required_conn(
                    conn,
                    updated_at=created.record.created_at,
                    reason="runtime_bootstrap_claimed",
                )
                self._insert_credential_conn(conn, created.record)
                conn.execute(
                    """
                    INSERT INTO runtime_bootstrap_claims(
                        bootstrap_secret_hash,
                        instance_id,
                        credential_id,
                        claimed_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        bootstrap_secret_hash,
                        created.record.instance_id,
                        created.record.credential_id,
                        created.record.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise BootstrapClaimRefusedError(
                "runtime_bootstrap.claim_conflict",
                instance_id=created.record.instance_id,
            ) from exc
        return created

    def claim_bootstrap_credential(
        self,
        *,
        instance_id: str,
        bootstrap_secret: str,
        expected_bootstrap_secret: str | None,
    ) -> CreatedRuntimeCredential:
        """Exchange a one-time bootstrap secret for the first ADMIN credential."""
        created = self.prepare_bootstrap_credential(
            instance_id=instance_id,
            bootstrap_secret=bootstrap_secret,
            expected_bootstrap_secret=expected_bootstrap_secret,
        )
        return self.claim_prepared_bootstrap_credential(
            created,
            bootstrap_secret=bootstrap_secret,
        )

    def authenticate(self, token: str) -> RuntimeCredentialRecord | None:
        """Return the active credential matching *token*, if one exists."""
        token_hash = _hash_token(token)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    credential_id,
                    instance_id,
                    label,
                    permission_mode,
                    token_hash,
                    created_at,
                    created_by,
                    revoked_at,
                    principal_id
                FROM runtime_credentials
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(row["token_hash"], token_hash):
            return None
        return self._row_to_record(row)

    def get(self, credential_id: str) -> RuntimeCredentialRecord | None:
        """Return stored credential metadata without plaintext token material."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    credential_id,
                    instance_id,
                    label,
                    permission_mode,
                    token_hash,
                    created_at,
                    created_by,
                    revoked_at,
                    principal_id
                FROM runtime_credentials
                WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_for_instance(self, instance_id: str) -> list[RuntimeCredentialRecord]:
        """List stored credential metadata for one instance."""
        _validate_governed_instance_id(instance_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    credential_id,
                    instance_id,
                    label,
                    permission_mode,
                    token_hash,
                    created_at,
                    created_by,
                    revoked_at,
                    principal_id
                FROM runtime_credentials
                WHERE instance_id = ?
                ORDER BY created_at, credential_id
                """,
                (instance_id,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def revoke_credential(
        self,
        *,
        instance_id: str,
        credential_id: str,
    ) -> RuntimeCredentialRecord:
        """Revoke one instance-scoped credential and return its metadata."""
        refuse_write_while_previewing("runtime credential store")
        _validate_governed_instance_id(instance_id)
        revoked_at = format_datetime(utc_now())
        assert revoked_at is not None
        with self._connect() as conn:
            existing = self._fetch_record_row(conn, instance_id, credential_id)
            if existing is None:
                raise RuntimeCredentialNotFoundError(credential_id)
            if existing["revoked_at"] is None:
                conn.execute(
                    """
                    UPDATE runtime_credentials
                    SET revoked_at = ?
                    WHERE credential_id = ? AND instance_id = ?
                    """,
                    (revoked_at, credential_id, instance_id),
                )
            row = self._fetch_record_row(conn, instance_id, credential_id)
        assert row is not None
        return self._row_to_record(row)

    def prepare_rotated_credential(
        self,
        *,
        instance_id: str,
        credential_id: str,
        rotated_by: str | None = None,
    ) -> CreatedRuntimeCredential:
        """Prepare a replacement credential without revoking the active token."""
        _validate_governed_instance_id(instance_id)
        with self._connect() as conn:
            existing = self._fetch_record_row(conn, instance_id, credential_id)
            if existing is None or existing["revoked_at"] is not None:
                raise RuntimeCredentialNotFoundError(credential_id)
            label = str(existing["label"])
            permission_mode = _parse_permission_mode(str(existing["permission_mode"]))
            principal_id = existing["principal_id"]

        # A rotation replaces the token, never the principal it acts as.
        return self._new_created_credential(
            instance_id=instance_id,
            label=label,
            permission_mode=permission_mode,
            created_by=rotated_by,
            principal_id=principal_id,
        )

    def commit_prepared_rotation(
        self,
        created: CreatedRuntimeCredential,
        *,
        instance_id: str,
        credential_id: str,
        proof_digest: str | None = None,
    ) -> CreatedRuntimeCredential:
        """Revoke an active credential and commit a prepared replacement.

        ``proof_digest`` names the principal's signed consent the replacement
        consumed, spent in the same transaction exactly as a mint spends one.
        """
        refuse_write_while_previewing("runtime credential store")
        _validate_governed_instance_id(instance_id)
        try:
            return self._commit_rotation(
                created,
                instance_id=instance_id,
                credential_id=credential_id,
                proof_digest=proof_digest,
            )
        except sqlite3.IntegrityError as exc:
            if proof_digest is None:
                raise
            raise _proof_replayed() from exc

    def _commit_rotation(
        self,
        created: CreatedRuntimeCredential,
        *,
        instance_id: str,
        credential_id: str,
        proof_digest: str | None,
    ) -> CreatedRuntimeCredential:
        with self._connect() as conn:
            existing = self._fetch_record_row(conn, instance_id, credential_id)
            if existing is None or existing["revoked_at"] is not None:
                raise RuntimeCredentialNotFoundError(credential_id)
            if proof_digest is not None:
                self._spend_proof_conn(conn, proof_digest, created.record)

            self._mark_auth_required_conn(
                conn,
                updated_at=created.record.created_at,
                reason="runtime_credential_rotated",
            )
            conn.execute(
                """
                UPDATE runtime_credentials
                SET revoked_at = ?
                WHERE credential_id = ? AND instance_id = ?
                """,
                (created.record.created_at, credential_id, instance_id),
            )
            self._insert_credential_conn(conn, created.record)
            created_row = self._fetch_record_row(
                conn,
                created.record.instance_id,
                created.record.credential_id,
            )

        assert created_row is not None
        return CreatedRuntimeCredential(
            record=self._row_to_record(created_row),
            token=created.token,
        )

    def rotate_credential(
        self,
        *,
        instance_id: str,
        credential_id: str,
        rotated_by: str | None = None,
    ) -> CreatedRuntimeCredential:
        """Revoke an active credential and create a replacement with one new token."""
        created = self.prepare_rotated_credential(
            instance_id=instance_id,
            credential_id=credential_id,
            rotated_by=rotated_by,
        )
        return self.commit_prepared_rotation(
            created,
            instance_id=instance_id,
            credential_id=credential_id,
        )

    def has_active_credentials(self) -> bool:
        """Return whether at least one active runtime credential exists."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM runtime_credentials
                WHERE revoked_at IS NULL
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def mark_auth_required(self, reason: str) -> None:
        """Persist that this server state dir must not restart without auth."""
        refuse_write_while_previewing("runtime credential store")
        updated_at = format_datetime(utc_now())
        assert updated_at is not None
        with self._connect() as conn:
            self._mark_auth_required_conn(conn, updated_at=updated_at, reason=reason)

    def is_auth_required(self) -> bool:
        """Return whether this state dir must use authenticated daemon mode."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT value
                FROM runtime_auth_state
                WHERE key = 'auth_required'
                """
            ).fetchone()
            if row is not None and row["value"] == "true":
                return True
            active = conn.execute(
                """
                SELECT 1
                FROM runtime_credentials
                WHERE revoked_at IS NULL
                LIMIT 1
                """
            ).fetchone()
        return active is not None

    @staticmethod
    def _new_created_credential(
        *,
        instance_id: str,
        label: str,
        permission_mode: PermissionMode,
        created_by: str | None,
        principal_id: str | None = None,
    ) -> CreatedRuntimeCredential:
        credential_id = _new_credential_id()
        token = _new_token(credential_id)
        token_hash = _hash_token(token)
        created_at = format_datetime(utc_now())
        assert created_at is not None
        return CreatedRuntimeCredential(
            record=RuntimeCredentialRecord(
                credential_id=credential_id,
                instance_id=instance_id,
                label=label,
                permission_mode=permission_mode,
                token_hash=token_hash,
                created_at=created_at,
                created_by=created_by,
                principal_id=principal_id,
            ),
            token=token,
        )

    @staticmethod
    def _insert_credential_conn(
        conn: sqlite3.Connection,
        record: RuntimeCredentialRecord,
    ) -> None:
        conn.execute(
            """
            INSERT INTO runtime_credentials(
                credential_id,
                instance_id,
                label,
                permission_mode,
                token_hash,
                created_at,
                created_by,
                revoked_at,
                principal_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.credential_id,
                record.instance_id,
                record.label,
                _serialize_permission_mode(record.permission_mode),
                record.token_hash,
                record.created_at,
                record.created_by,
                record.revoked_at,
                record.principal_id,
            ),
        )

    @staticmethod
    def _validate_bootstrap_claim_conn(
        conn: sqlite3.Connection,
        instance_id: str,
        bootstrap_secret_hash: str,
    ) -> None:
        prior_claim = conn.execute(
            """
            SELECT 1
            FROM runtime_bootstrap_claims
            WHERE bootstrap_secret_hash = ? AND instance_id = ?
            LIMIT 1
            """,
            (bootstrap_secret_hash, instance_id),
        ).fetchone()
        if prior_claim is not None:
            raise BootstrapClaimRefusedError(
                "runtime_bootstrap.secret_already_claimed", instance_id=instance_id
            )

        prior_admin = conn.execute(
            """
            SELECT 1
            FROM runtime_credentials
            WHERE instance_id = ? AND permission_mode = ?
            LIMIT 1
            """,
            (instance_id, _serialize_permission_mode(PermissionMode.ADMIN)),
        ).fetchone()
        if prior_admin is not None:
            raise BootstrapClaimRefusedError(
                "runtime_bootstrap.admin_exists", instance_id=instance_id
            )

    @staticmethod
    def _mark_auth_required_conn(
        conn: sqlite3.Connection,
        *,
        updated_at: str,
        reason: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO runtime_auth_state(key, value, updated_at, reason)
            VALUES ('auth_required', 'true', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                reason = excluded.reason
            """,
            (updated_at, reason),
        )

    @staticmethod
    def _validate_recovery_target_conn(
        conn: sqlite3.Connection,
        instance_id: str,
    ) -> None:
        row = conn.execute(
            """
            SELECT 1
            FROM runtime_credentials
            WHERE instance_id = ? AND permission_mode = ?
            LIMIT 1
            """,
            (instance_id, _serialize_permission_mode(PermissionMode.ADMIN)),
        ).fetchone()
        if row is None:
            raise RuntimeCredentialRecoveryError(
                f"No ADMIN runtime credential exists for instance_id {instance_id!r}."
            )

    @classmethod
    def _spend_proof_conn(
        cls, conn: sqlite3.Connection, proof_digest: str, record: RuntimeCredentialRecord
    ) -> None:
        """Record one signed consent as spent; a second spend raises IntegrityError."""

        cls._prune_spent_proofs_conn(conn, now=record.created_at)
        conn.execute(
            """
            INSERT INTO runtime_credential_proofs(proof_digest, credential_id, used_at)
            VALUES (?, ?, ?)
            """,
            (proof_digest, record.credential_id, record.created_at),
        )

    @staticmethod
    def _prune_spent_proofs_conn(conn: sqlite3.Connection, *, now: str) -> int:
        """Forget spent consents that can no longer be replayed; return how many.

        A consent is accepted only while its ``issued_at`` is within the skew
        window of the daemon's clock, on either side. One spent at ``u`` was
        issued no later than ``u + skew``, so it stops verifying by
        ``u + 2 * skew``; past that its row guards nothing.
        """

        current = parse_datetime(now)
        assert current is not None
        horizon = current - timedelta(seconds=2 * RUNTIME_CREDENTIAL_PROOF_MAX_SKEW_SECONDS)
        stale = [
            (str(row[0]),)
            for row in conn.execute("SELECT proof_digest, used_at FROM runtime_credential_proofs")
            if (used := parse_datetime(str(row[1]))) is not None and used < horizon
        ]
        conn.executemany("DELETE FROM runtime_credential_proofs WHERE proof_digest = ?", stale)
        return len(stale)

    @staticmethod
    def _ensure_principal_column_conn(conn: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(runtime_credentials)")}
        if "principal_id" not in columns:
            # Migration, never a rebind: every credential minted before
            # credentials named a principal stays unbound (NULL). It keeps its
            # transport authority and can no longer author; the repair is to
            # mint a principal-bound credential and revoke the unbound one.
            conn.execute("ALTER TABLE runtime_credentials ADD COLUMN principal_id TEXT")

    @staticmethod
    def _ensure_bootstrap_claims_per_host_conn(conn: sqlite3.Connection) -> None:
        """Migrate the claim table from one claim per secret to one per host.

        The daemon's bootstrap secret is claimable once per host: a second host
        on the same daemon claims its own first ADMIN credential with the same
        secret, with no restart. The old table keyed claims by the secret alone,
        so one claim on any host used it up for every other one.
        """

        keys = [
            str(row[1])
            for row in sorted(
                (row for row in conn.execute("PRAGMA table_info(runtime_bootstrap_claims)")),
                key=lambda row: int(row[5]),
            )
            if int(row[5]) > 0
        ]
        if keys != ["bootstrap_secret_hash"]:
            return
        conn.executescript(
            """
            ALTER TABLE runtime_bootstrap_claims RENAME TO runtime_bootstrap_claims_per_secret;
            CREATE TABLE runtime_bootstrap_claims (
                bootstrap_secret_hash TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                credential_id TEXT NOT NULL UNIQUE,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (bootstrap_secret_hash, instance_id)
            );
            INSERT INTO runtime_bootstrap_claims
                SELECT bootstrap_secret_hash, instance_id, credential_id, claimed_at
                FROM runtime_bootstrap_claims_per_secret;
            DROP TABLE runtime_bootstrap_claims_per_secret;
            CREATE INDEX IF NOT EXISTS idx_runtime_bootstrap_claims_instance
                ON runtime_bootstrap_claims(instance_id);
            """
        )

    @staticmethod
    def _ensure_recovery_events_table_conn(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_recovery_events (
                created_at TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                credential_id TEXT NOT NULL,
                uid INTEGER NOT NULL,
                hostname TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _fetch_record_row(
        conn: sqlite3.Connection,
        instance_id: str,
        credential_id: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT
                credential_id,
                instance_id,
                label,
                permission_mode,
                token_hash,
                created_at,
                created_by,
                revoked_at,
                principal_id
            FROM runtime_credentials
            WHERE instance_id = ? AND credential_id = ?
            """,
            (instance_id, credential_id),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RuntimeCredentialRecord:
        return RuntimeCredentialRecord(
            credential_id=row["credential_id"],
            instance_id=row["instance_id"],
            label=row["label"],
            permission_mode=_parse_permission_mode(row["permission_mode"]),
            token_hash=row["token_hash"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            revoked_at=row["revoked_at"],
            principal_id=row["principal_id"],
        )


_runtime_credential_store: RuntimeCredentialStore | None = None


def get_runtime_credential_store() -> RuntimeCredentialStore:
    """Return the process-global runtime credential store."""
    global _runtime_credential_store
    if _runtime_credential_store is None:
        _runtime_credential_store = RuntimeCredentialStore(
            get_registry().state_root / "daemon" / "runtime_credentials.db"
        )
    return _runtime_credential_store


def reset_runtime_credential_store() -> None:
    """Clear the process-global runtime credential store cache. Used by tests."""
    global _runtime_credential_store
    _runtime_credential_store = None


def list_runtime_credential_instance_ids(db_path: Path) -> list[str]:
    """List distinct instance ids present in a runtime credentials DB.

    Read-only helper for local recovery target selection; raises the
    recovery error types so callers never handle sqlite exceptions.
    """
    try:
        with sqlite3.connect(db_path, timeout=0.0) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT instance_id
                FROM runtime_credentials
                ORDER BY instance_id
                """
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_sqlite_busy(exc):
            raise RuntimeCredentialRecoveryBusyError(
                "Runtime credentials DB is locked. Stop the Cruxible daemon serving "
                "this state dir before running recover-admin."
            ) from exc
        raise RuntimeCredentialRecoveryError(
            f"Could not read runtime credentials DB {db_path}: {exc}"
        ) from exc
    return [str(row[0]) for row in rows]


def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message
