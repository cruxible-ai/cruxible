"""A bearer credential acts as exactly one principal, minted only with its authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.signing import (
    LocalEd25519ApprovalSigner,
    sign_runtime_credential_mint,
)
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.credentials import (
    RuntimeCredentialStore,
    get_runtime_credential_store,
)


def _owner_key(private_key_path: Path) -> Path:
    # The conftest owner custody sits beside the reviewer custody it yields.
    return private_key_path.parent.parent / "owner-custody" / "operator.ed25519"


def _bearer(principal_id: str | None, instance_id: str, mode: PermissionMode) -> str:
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label=f"{principal_id or 'unbound'}-token",
        permission_mode=mode,
        principal_id=principal_id,
    )
    return created.token


def _mint(client: TestClient, instance_id: str, token: str, **body: object) -> object:
    return client.post(
        f"/api/v1/{instance_id}/runtime/credentials",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_an_admin_token_alone_cannot_mint_in_another_principals_name(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    unbound_admin = _bearer(None, instance_id, PermissionMode.ADMIN)
    operator_admin = _bearer("operator", instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

    for token in (unbound_admin, operator_admin):
        refused = _mint(
            client, instance_id, token, principal_id="reviewer", permission_mode="governed_write"
        )
        assert refused.status_code == 403  # type: ignore[attr-defined]
        body = refused.json()  # type: ignore[attr-defined]
        assert body["error_code"] == "runtime_credential.principal_authority_required"
        assert (
            "cruxible credential mint --principal-id reviewer --key-dir DIR --mode governed_write"
            in body["message"]
        )
        assert body["repair"]["operation"] == "credential.mint"

    # A request that already acts as the principal carries its authority.
    own = _mint(
        client, instance_id, operator_admin, principal_id="operator", permission_mode="read_only"
    )
    assert own.status_code == 200, own.text  # type: ignore[attr-defined]
    assert own.json()["credential"]["principal_id"] == "operator"  # type: ignore[attr-defined]


def test_the_principals_signed_consent_mints_once_and_the_credential_acts_as_it(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, reviewer_key = playbill_http
    admin = _bearer(None, instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    proof = sign_runtime_credential_mint(
        instance_id=instance_id,
        principal_id="reviewer",
        permission_mode="governed_write",
        label="reviewer agent",
        private_key_path=reviewer_key,
        forbidden_roots=(),
    )
    body = {
        "principal_id": "reviewer",
        "permission_mode": "governed_write",
        "label": "reviewer agent",
        "principal_proof": proof.model_dump(mode="json"),
    }

    minted = _mint(client, instance_id, admin, **body)
    replayed = _mint(client, instance_id, admin, **body)

    assert minted.status_code == 200, minted.text  # type: ignore[attr-defined]
    credential = minted.json()  # type: ignore[attr-defined]
    assert credential["credential"]["principal_id"] == "reviewer"
    assert credential["credential"]["label"] == "reviewer agent"
    assert replayed.status_code == 409  # type: ignore[attr-defined]
    assert replayed.json()["error_code"] == "runtime_credential.principal_proof_replayed"  # type: ignore[attr-defined]

    who = client.get(
        f"/api/v1/{instance_id}/playbill/whoami",
        headers={"Authorization": f"Bearer {credential['token']}"},
    ).json()
    assert who["actor_id"] == "reviewer"
    assert who["actor_id_source"] == "runtime_credential"
    assert who["credential_label"] == "reviewer agent"
    assert who["authenticated"] is True


def test_a_consent_signed_by_another_key_or_for_other_terms_is_refused(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, instance_id, reviewer_key = playbill_http
    admin = _bearer(None, instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    stranger = generate_client_principal_key(
        tmp_path / "stranger", principal_id="reviewer", kind="ordinary", forbidden_roots=()
    )
    forged = sign_runtime_credential_mint(
        instance_id=instance_id,
        principal_id="reviewer",
        permission_mode="admin",
        label="reviewer",
        private_key_path=stranger.private_key_path,
        forbidden_roots=(),
    )
    narrower = sign_runtime_credential_mint(
        instance_id=instance_id,
        principal_id="reviewer",
        permission_mode="read_only",
        label="reviewer",
        private_key_path=reviewer_key,
        forbidden_roots=(),
    )

    wrong_key = _mint(
        client,
        instance_id,
        admin,
        principal_id="reviewer",
        permission_mode="admin",
        principal_proof=forged.model_dump(mode="json"),
    )
    wider_terms = _mint(
        client,
        instance_id,
        admin,
        principal_id="reviewer",
        permission_mode="admin",
        principal_proof=narrower.model_dump(mode="json"),
    )

    for refused in (wrong_key, wider_terms):
        assert refused.status_code == 403  # type: ignore[attr-defined]
        assert (
            refused.json()["error_code"]  # type: ignore[attr-defined]
            == "runtime_credential.principal_proof_invalid"
        )


def test_minting_for_an_unregistered_principal_names_principal_add(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    admin = _bearer("operator", instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

    refused = _mint(
        client, instance_id, admin, principal_id="ghost", permission_mode="governed_write"
    )

    assert refused.status_code == 403  # type: ignore[attr-defined]
    assert refused.json()["error_code"] == "cruxible.identity.principal_absent"  # type: ignore[attr-defined]
    assert refused.json()["repair"]["operation"] == "cruxible.principal.add"  # type: ignore[attr-defined]


def test_an_unbound_credential_keeps_transport_authority_but_cannot_author(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    unbound = _bearer(None, instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    headers = {"Authorization": f"Bearer {unbound}"}

    listed = client.get(
        f"/api/v1/{instance_id}/playbill/orient", params={"section": "principals"}, headers=headers
    )
    who = client.get(f"/api/v1/{instance_id}/playbill/whoami", headers=headers).json()
    refused = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/sha256:{'0' * 64}/withdraw",
        json={"reason": "unbound"},
        headers=headers,
    )

    assert listed.status_code == 200, listed.text
    assert who["actor_id"] is None
    assert who["actor_id_source"] == "unbound_credential"
    assert who["principal_registration_status"] is None
    assert refused.status_code == 403, refused.text
    assert refused.json()["error_code"] == "cruxible.identity.credential_unbound"
    assert "cruxible credential mint --principal-id ID --key-dir DIR" in refused.json()["message"]


def _accept_principal_change(
    client: TestClient, instance_id: str, principal: PrincipalRecord, *, owner_key: Path
) -> None:
    proposed = client.post(
        f"/api/v1/{instance_id}/playbill/principals/proposals",
        json={
            "principal": principal.model_dump(mode="json"),
            "proposal_name": f"change-{principal.principal_id}",
        },
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["admission"]["proposal_id"]
    challenge = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "operator"},
    ).json()
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="operator",
        private_key_path=owner_key,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    approved = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    activated = client.post(f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text


def test_a_recovery_principal_never_holds_a_credential(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, instance_id, reviewer_key = playbill_http
    rescue = generate_client_principal_key(
        tmp_path / "rescue", principal_id="rescue", kind="recovery", forbidden_roots=()
    )
    _accept_principal_change(
        client, instance_id, rescue.principal, owner_key=_owner_key(reviewer_key)
    )
    admin = _bearer(None, instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    proof = sign_runtime_credential_mint(
        instance_id=instance_id,
        principal_id="rescue",
        permission_mode="governed_write",
        label="rescue",
        private_key_path=rescue.private_key_path,
        forbidden_roots=(),
    )

    refused = _mint(
        client,
        instance_id,
        admin,
        principal_id="rescue",
        permission_mode="governed_write",
        principal_proof=proof.model_dump(mode="json"),
    )

    assert refused.status_code == 403  # type: ignore[attr-defined]
    body = refused.json()  # type: ignore[attr-defined]
    assert body["error_code"] == "runtime_credential.principal_not_ordinary"
    assert body["repair"] == {
        "operation": "cruxible.orient",
        "arguments": {"section": "principals"},
    }


def test_revoking_a_principal_revokes_its_credentials(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, reviewer_key = playbill_http
    reviewer_token = _bearer("reviewer", instance_id, PermissionMode.GOVERNED_WRITE)
    listing = client.get(
        f"/api/v1/{instance_id}/playbill/orient", params={"section": "principals"}
    ).json()
    reviewer = next(
        PrincipalRecord.model_validate(item)
        for item in listing["principals"]
        if item["principal_id"] == "reviewer"
    )
    _accept_principal_change(
        client,
        instance_id,
        reviewer.model_copy(update={"status": "revoked"}),
        owner_key=_owner_key(reviewer_key),
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

    refused = client.get(
        f"/api/v1/{instance_id}/playbill/whoami",
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )

    assert refused.status_code == 403
    assert refused.json()["error_code"] == "cruxible.identity.principal_revoked"
    (record,) = [
        item
        for item in get_runtime_credential_store().list_for_instance(instance_id)
        if item.principal_id == "reviewer"
    ]
    assert record.revoked_at is not None


def test_credentials_minted_before_principal_binding_stay_unbound(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_credentials.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE runtime_credentials (
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
        conn.execute(
            "INSERT INTO runtime_credentials VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("rcred_legacy", "inst_a", "manager", "admin", "hash", "2026-09-01", None, None),
        )

    store = RuntimeCredentialStore(db_path)

    record = store.get("rcred_legacy")
    assert record is not None
    # Never silently rebound from its label, even though a principal "manager" may exist.
    assert record.label == "manager"
    assert record.principal_id is None


def test_minting_on_an_auth_off_daemon_refuses_without_latching_auth(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, reviewer_key = playbill_http
    proof = sign_runtime_credential_mint(
        instance_id=instance_id,
        principal_id="reviewer",
        permission_mode="governed_write",
        label="reviewer",
        private_key_path=reviewer_key,
        forbidden_roots=(),
    )

    refused = client.post(
        f"/api/v1/{instance_id}/runtime/credentials",
        json={
            "principal_id": "reviewer",
            "permission_mode": "governed_write",
            "principal_proof": proof.model_dump(mode="json"),
        },
    )

    assert refused.status_code == 409
    body = refused.json()
    assert body["error_code"] == "runtime_credential.auth_off"
    assert "cruxible server start --auth" in body["message"]
    assert body["repair"] == {"operation": "server.start", "arguments": {"auth": True}}
    store = get_runtime_credential_store()
    assert store.list_for_instance(instance_id) == []
    assert store.is_auth_required() is False


def test_minting_forgets_spent_consents_past_the_replay_window(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, reviewer_key = playbill_http
    admin = _bearer(None, instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    store = get_runtime_credential_store()
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany(
            "INSERT INTO runtime_credential_proofs VALUES (?, ?, ?)",
            [
                ("sha256:old", "rcred_old", "2026-01-01T00:00:00+00:00"),
                ("sha256:recent", "rcred_recent", "2999-01-01T00:00:00+00:00"),
            ],
        )
    proof = sign_runtime_credential_mint(
        instance_id=instance_id,
        principal_id="reviewer",
        permission_mode="read_only",
        label="reviewer",
        private_key_path=reviewer_key,
        forbidden_roots=(),
    )

    minted = _mint(
        client,
        instance_id,
        admin,
        principal_id="reviewer",
        permission_mode="read_only",
        principal_proof=proof.model_dump(mode="json"),
    )

    assert minted.status_code == 200, minted.text  # type: ignore[attr-defined]
    with sqlite3.connect(store.db_path) as conn:
        kept = {
            row[0] for row in conn.execute("SELECT proof_digest FROM runtime_credential_proofs")
        }
    assert "sha256:old" not in kept
    assert "sha256:recent" in kept
    assert len(kept) == 2  # the recent row and the consent just spent


def _rotate(
    client: TestClient, instance_id: str, token: str, credential_id: str, **body: object
) -> object:
    return client.post(
        f"/api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate",
        json=body or None,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_rotating_a_bound_credential_needs_its_principals_authority(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, reviewer_key = playbill_http
    store = get_runtime_credential_store()
    reviewer = store.create_credential(
        instance_id=instance_id,
        label="reviewer",
        permission_mode=PermissionMode.GOVERNED_WRITE,
        principal_id="reviewer",
    )
    unbound_admin = _bearer(None, instance_id, PermissionMode.ADMIN)
    operator_admin = _bearer("operator", instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

    for token in (unbound_admin, operator_admin):
        refused = _rotate(client, instance_id, token, reviewer.record.credential_id)
        assert refused.status_code == 403, refused.text  # type: ignore[attr-defined]
        assert (
            refused.json()["error_code"]  # type: ignore[attr-defined]
            == "runtime_credential.principal_authority_required"
        )
        assert "token" not in refused.text or refused.json().get("token") is None  # type: ignore[attr-defined]
    current = store.get(reviewer.record.credential_id)
    assert current is not None and current.revoked_at is None

    # Another admin may still revoke it; it never receives a credential for it.
    proof = sign_runtime_credential_mint(
        instance_id=instance_id,
        principal_id="reviewer",
        permission_mode="governed_write",
        label="reviewer",
        private_key_path=reviewer_key,
        forbidden_roots=(),
    )
    # A rotation cannot be undone: it previews, then commits that preview.
    previewed = _rotate(
        client,
        instance_id,
        unbound_admin,
        reviewer.record.credential_id,
        principal_proof=proof.model_dump(mode="json"),
    )
    assert previewed.status_code == 200, previewed.text  # type: ignore[attr-defined]
    assert previewed.json()["status"] == "would_rotate"  # type: ignore[attr-defined]
    consented = _rotate(
        client,
        instance_id,
        unbound_admin,
        reviewer.record.credential_id,
        principal_proof=proof.model_dump(mode="json"),
        dry_run=False,
        at=previewed.json()["coordinate"]["digest"],  # type: ignore[attr-defined]
    )
    assert consented.status_code == 200, consented.text  # type: ignore[attr-defined]
    rotated = consented.json()  # type: ignore[attr-defined]
    assert rotated["credential"]["principal_id"] == "reviewer"
    own = _rotate(client, instance_id, rotated["token"], rotated["credential"]["credential_id"])
    assert own.status_code == 403  # governed_write cannot manage credentials at all
    revoke_url = (
        f"/api/v1/{instance_id}/runtime/credentials/{rotated['credential']['credential_id']}/revoke"
    )
    admin_headers = {"Authorization": f"Bearer {operator_admin}"}
    revoke_preview = client.post(revoke_url, headers=admin_headers)
    assert revoke_preview.status_code == 200, revoke_preview.text
    revoked = client.post(
        revoke_url,
        json={"dry_run": False, "at": revoke_preview.json()["coordinate"]["digest"]},
        headers=admin_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"


_UNGUARDED_WRITES = (
    ("/playbill/bodies", {"content_base64": "Ym9keQ=="}),
    ("/playbill/ledger/mirror", {"url": "https://mirror.example.test/ledger.git"}),
    ("/playbill/ledger/publish", {"timeout": 0}),
    ("/playbill/claim-attestations/recover", {}),
    ("/playbill/claim-types/upgrade", {}),
    ("/playbill/claim-types/upgrade", {"dry_run": True}),
)


def test_an_unbound_credential_is_refused_on_every_instance_write(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    unbound = _bearer(None, instance_id, PermissionMode.ADMIN)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

    for path, body in _UNGUARDED_WRITES:
        refused = client.post(
            f"/api/v1/{instance_id}{path}",
            json=body,
            headers={"Authorization": f"Bearer {unbound}"},
        )
        assert refused.status_code == 403, (path, refused.text)
        assert refused.json()["error_code"] == "cruxible.identity.credential_unbound", path
    mirror = client.get(
        f"/api/v1/{instance_id}/playbill/ledger/clone-url",
        headers={"Authorization": f"Bearer {unbound}"},
    )
    assert "mirror.example.test" not in mirror.text


def test_an_unregistered_claim_is_refused_on_every_instance_write(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    from cruxible_client.contracts.principals import PRINCIPAL_ID_HEADER

    client, instance_id, _reviewer_key = playbill_http

    for path, body in _UNGUARDED_WRITES:
        refused = client.post(
            f"/api/v1/{instance_id}{path}", json=body, headers={PRINCIPAL_ID_HEADER: "mallory"}
        )
        assert refused.status_code == 403, (path, refused.text)
        assert refused.json()["error_code"] == "cruxible.identity.principal_absent", path


#: Doors that take the request's actor through ``_write_actor_context`` (runs,
#: Line arming and dispatch). A read-tier Line dispatch included: every one must
#: give an unbound credential the typed refusal, not a generic 401.
_ACTOR_DOORS = (
    ("/playbill/lines/missing/dispatch", {}, PermissionMode.READ_ONLY),
    ("/playbill/lines/missing/dispatch", {}, PermissionMode.ADMIN),
    ("/playbill/lines/missing/arm", None, PermissionMode.ADMIN),
    ("/playbill/lines/missing/disarm", None, PermissionMode.ADMIN),
)


@pytest.mark.parametrize(("path", "body", "mode"), _ACTOR_DOORS)
def test_an_unbound_credential_gets_the_typed_refusal_at_every_actor_door(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, object] | None,
    mode: PermissionMode,
) -> None:
    """Regression (review P2): a read-tier dispatch raised a plain AuthenticationError."""

    client, instance_id, _reviewer_key = playbill_http
    unbound = get_runtime_credential_store().create_credential(
        instance_id=instance_id, label="manager", permission_mode=mode, principal_id=None
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

    refused = client.post(
        f"/api/v1/{instance_id}{path}",
        json=body,
        headers={"Authorization": f"Bearer {unbound.token}"},
    )

    assert refused.status_code == 403, (path, refused.text)
    answer = refused.json()
    assert answer["error_code"] == "cruxible.identity.credential_unbound", path
    assert answer["repair"] == {
        "operation": "credential.mint",
        "arguments": {"unbound_credential_id": unbound.record.credential_id},
    }
    # MCP tool errors carry the message: the code and the runnable repair.
    assert "cruxible credential mint --principal-id ID --key-dir DIR" in answer["message"]
    # The SDK reads the same envelope back as a coded error, not an auth failure.
    from cruxible_client.errors import AuthenticationError as ClientAuthenticationError
    from cruxible_client.errors import ErrorResponse, response_to_error

    sdk_error = response_to_error(403, ErrorResponse.model_validate(answer))
    assert not isinstance(sdk_error, ClientAuthenticationError)
    assert getattr(sdk_error, "error_code", None) == "cruxible.identity.credential_unbound"
    assert getattr(sdk_error, "repair").operation == "credential.mint"


def test_every_actor_boundary_refuses_an_unbound_credential_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared boundary every run, Line, prediction and curation door calls."""

    from cruxible_core.errors import PrincipalRefusedError
    from cruxible_core.runtime import playbill_api
    from cruxible_core.server.auth import ResolvedAuthContext

    monkeypatch.setattr(playbill_api, "is_server_auth_enabled", lambda: True)
    monkeypatch.setattr(
        playbill_api,
        "get_current_auth_context",
        lambda: ResolvedAuthContext(
            credential_id="cred-unbound",
            credential_label="manager",
            credential_type="runtime_credential",
            instance_scope="inst",
            role=None,
            effective_permission_mode=PermissionMode.ADMIN,
            principal_id=None,
        ),
    )
    for boundary in (
        playbill_api._write_actor_context,
        playbill_api._curation_actor,
        playbill_api._actor_id,
    ):
        with pytest.raises(PrincipalRefusedError) as refused:
            boundary("inst")
        assert refused.value.error_code == "cruxible.identity.credential_unbound"
        assert refused.value.repair is not None
        assert refused.value.repair.arguments == {"unbound_credential_id": "cred-unbound"}  # type: ignore[union-attr]
