"""Each refused runtime bootstrap claim names its own reason and repair."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_client.errors import AuthenticationError as ClientAuthenticationError
from cruxible_client.errors import response_to_error
from cruxible_core.errors import AuthenticationError, BootstrapClaimRefusedError
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server import credentials as credentials_module
from cruxible_core.server.credentials import RuntimeCredentialStore, _hash_token
from cruxible_core.server.errors import error_to_response

EXPECTED_SECRET = "the-daemons-expected-secret"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeCredentialStore:
    monkeypatch.setattr(credentials_module, "_validate_governed_instance_id", lambda _id: None)
    return RuntimeCredentialStore(tmp_path / "runtime_credentials.db")


def _refusal(store: RuntimeCredentialStore, **kwargs: object) -> BootstrapClaimRefusedError:
    arguments: dict[str, object] = {
        "instance_id": "inst_a",
        "bootstrap_secret": EXPECTED_SECRET,
        "expected_bootstrap_secret": EXPECTED_SECRET,
    }
    arguments.update(kwargs)
    with pytest.raises(BootstrapClaimRefusedError) as caught:
        store.claim_bootstrap_credential(**arguments)  # type: ignore[arg-type]
    return caught.value


@pytest.mark.parametrize("expected", [EXPECTED_SECRET, None])
def test_a_wrong_secret_is_refused_without_revealing_the_expected_one(
    store: RuntimeCredentialStore, expected: str | None
) -> None:
    refusal = _refusal(store, bootstrap_secret="wrong", expected_bootstrap_secret=expected)

    assert refusal.error_code == "runtime_bootstrap.secret_invalid"
    assert isinstance(refusal, AuthenticationError)
    assert EXPECTED_SECRET not in str(refusal)
    assert "Repair:" in str(refusal)


def test_a_claimed_secret_is_refused_as_already_claimed(store: RuntimeCredentialStore) -> None:
    store.claim_bootstrap_credential(
        instance_id="inst_a",
        bootstrap_secret=EXPECTED_SECRET,
        expected_bootstrap_secret=EXPECTED_SECRET,
    )

    refusal = _refusal(store)

    assert refusal.error_code == "runtime_bootstrap.secret_already_claimed"
    assert "recover-admin" in str(refusal)


def test_an_instance_with_an_admin_is_refused_as_already_bootstrapped(
    store: RuntimeCredentialStore,
) -> None:
    store.create_credential(
        instance_id="inst_a", label="admin", permission_mode=PermissionMode.ADMIN
    )

    refusal = _refusal(store)

    assert refusal.error_code == "runtime_bootstrap.admin_exists"
    assert "already bootstrapped" in str(refusal)
    assert "credential recover-admin" in str(refusal)


def test_a_claim_race_is_refused_as_a_retryable_conflict(
    store: RuntimeCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = store.prepare_bootstrap_credential(
        instance_id="inst_a",
        bootstrap_secret=EXPECTED_SECRET,
        expected_bootstrap_secret=EXPECTED_SECRET,
    )
    # Two claims for the same host: the secret is claimable once per host.
    second = store.prepare_bootstrap_credential(
        instance_id="inst_a",
        bootstrap_secret=EXPECTED_SECRET,
        expected_bootstrap_secret=EXPECTED_SECRET,
    )
    store.claim_prepared_bootstrap_credential(first, bootstrap_secret=EXPECTED_SECRET)
    # The second claim validated before the first committed; its insert races.
    monkeypatch.setattr(
        RuntimeCredentialStore, "_validate_bootstrap_claim_conn", staticmethod(lambda *a: None)
    )

    with pytest.raises(BootstrapClaimRefusedError) as caught:
        store.claim_prepared_bootstrap_credential(second, bootstrap_secret=EXPECTED_SECRET)

    assert caught.value.error_code == "runtime_bootstrap.claim_conflict"
    assert "retry" in str(caught.value)


@pytest.mark.parametrize(
    ("code", "operation"),
    [
        ("runtime_bootstrap.secret_invalid", "credential.claim-bootstrap"),
        ("runtime_bootstrap.secret_already_claimed", "credential.recover-admin"),
        ("runtime_bootstrap.admin_exists", "credential.recover-admin"),
        ("runtime_bootstrap.claim_conflict", "credential.claim-bootstrap"),
    ],
)
def test_each_refusal_is_a_401_with_its_code_and_repair(code: str, operation: str) -> None:
    status, body = error_to_response(
        BootstrapClaimRefusedError(code, instance_id="inst_a")  # type: ignore[arg-type]
    )

    assert status == 401
    assert body.error_code == code
    assert body.repair == RepairOperationV1(
        operation=operation, arguments={"instance_id": "inst_a"}
    )
    client_error = response_to_error(status, body)
    assert isinstance(client_error, ClientAuthenticationError)
    assert getattr(client_error, "error_code") == code
    assert str(client_error).startswith(f"{code}: ")


def test_the_secret_is_claimable_once_per_host(store: RuntimeCredentialStore) -> None:
    """Q16: a second host claims its own first ADMIN credential, with no restart."""

    first = store.claim_bootstrap_credential(
        instance_id="inst_a",
        bootstrap_secret=EXPECTED_SECRET,
        expected_bootstrap_secret=EXPECTED_SECRET,
    )
    second = store.claim_bootstrap_credential(
        instance_id="inst_b",
        bootstrap_secret=EXPECTED_SECRET,
        expected_bootstrap_secret=EXPECTED_SECRET,
    )

    assert (first.record.instance_id, second.record.instance_id) == ("inst_a", "inst_b")
    assert _refusal(store, instance_id="inst_b").error_code == (
        "runtime_bootstrap.secret_already_claimed"
    )


def test_a_per_secret_claim_table_migrates_to_one_claim_per_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credentials_module, "_validate_governed_instance_id", lambda _id: None)
    db_path = tmp_path / "daemon" / "runtime_credentials.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE runtime_bootstrap_claims (
                bootstrap_secret_hash TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                credential_id TEXT NOT NULL UNIQUE,
                claimed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO runtime_bootstrap_claims VALUES (?, ?, ?, ?)",
            (_hash_token(EXPECTED_SECRET), "inst_a", "rcred_old", "2026-09-30T00:00:00Z"),
        )
    conn.close()

    migrated = RuntimeCredentialStore(db_path)

    assert _refusal(migrated, instance_id="inst_a").error_code == (
        "runtime_bootstrap.secret_already_claimed"
    )
    claimed = migrated.claim_bootstrap_credential(
        instance_id="inst_b",
        bootstrap_secret=EXPECTED_SECRET,
        expected_bootstrap_secret=EXPECTED_SECRET,
    )
    assert claimed.record.instance_id == "inst_b"
