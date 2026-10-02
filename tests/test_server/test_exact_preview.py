"""R12 on the served surface: each change previews exactly, writes nothing, and pins.

A preview runs the change's own path up to the commit and must write nothing
ANYWHERE: every store under the test's root is snapshotted around it (the state
root holds the registry, the runtime credential DB, every instance's ledger,
``state.db``, CAS and indexes; custody and workspace directories sit beside
it). A commit carrying the preview's coordinate refuses once accepted state
moved, and a change that cannot be undone commits only with that coordinate.
"""

from __future__ import annotations

import base64
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.signing import sign_runtime_credential_mint
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner
from cruxible_core.runtime import host_api
from cruxible_core.runtime.permissions import PermissionMode, reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry
from tests.support.store_snapshot import assert_writes_nothing


def _api(instance_id: str, path: str) -> str:
    return f"/api/v1/{instance_id}{path}"


@pytest.fixture(autouse=True)
def _no_background_writers() -> Iterator[None]:
    """Stop the daemon's own background workers for the comparison.

    The consumer runner matches due work on its own clock and writes its
    exhaust stores whenever it ticks; it is not the preview, and a snapshot
    taken while it runs would charge its writes to the preview.
    """

    yield
    get_playbill_manager().consumer_runner.close()


def _quiet() -> None:
    get_playbill_manager().consumer_runner.close()


def _warm(client: TestClient, instance_id: str) -> Any:
    """Quiet the background workers, then an ordinary read: only the preview is compared."""

    def warm() -> None:
        _quiet()
        client.get(_api(instance_id, "/playbill/head"))

    return warm


def _ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return dict(response.json())


def _refused(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = dict(response.json())
    assert body["error_code"] == code, body
    assert body["repair"]["arguments"] == {"dry_run": True}, body
    return body


def _document(client: TestClient, instance_id: str, name: str) -> dict[str, Any]:
    stored = _ok(
        client.post(
            _api(instance_id, "/playbill/bodies"),
            json={"content_base64": base64.b64encode(f"{name}\n".encode()).decode("ascii")},
        )
    )
    return DocumentShell(
        identity=f"document:{name}",
        document_kind="design",
        title=name,
        media_type="text/plain",
        body_digest=stored["digest"],
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    ).model_dump(mode="json")


def _move_head(client: TestClient, instance_id: str, reviewer_key: Path) -> None:
    """Accept one ordinary Document change, so the accepted head moves."""

    proposed = _ok(
        client.post(
            _api(instance_id, "/playbill/documents/proposals"),
            json={
                "shell": _document(client, instance_id, "head-mover"),
                "proposal_name": "head-mover",
            },
        )
    )
    proposal_id = proposed["proposal"]["admission"]["proposal_id"]
    base = _api(instance_id, f"/playbill/proposals/{proposal_id}")
    challenge = _ok(client.post(f"{base}/approval-challenge", json={"signer_id": "reviewer"}))
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=reviewer_key,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    _ok(client.post(f"{base}/approvals", json={"attestation": attestation.model_dump(mode="json")}))
    assert _ok(client.post(f"{base}/activate"))["status"] == "accepted"


def _principal(tmp_path: Path, principal_id: str) -> dict[str, Any]:
    material = generate_client_principal_key(
        tmp_path / f"{principal_id}-custody",
        principal_id=principal_id,
        kind="ordinary",
        forbidden_roots=(),
    )
    return material.principal.model_dump(mode="json")


def test_a_principal_change_previews_writes_nothing_and_commits_pinned(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    body = {"principal": _principal(tmp_path, "newcomer"), "proposal_name": "add-newcomer"}
    url = _api(instance_id, "/playbill/principals/proposals")

    preview = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(url, json={**body, "dry_run": True}),
            warm=_warm(client, instance_id),
        )
    )

    assert preview["status"] == "would_propose"
    assert "admission" not in preview["proposal"]
    assert preview["proposal"]["evaluation"]["verdict"] == "candidate"
    at = preview["accepted_coordinate"]["git_oid"]
    committed = _ok(client.post(url, json={**body, "dry_run": False, "at": at}))
    assert committed["status"] == "admitted"
    assert committed["proposal"]["admission"]["proposal_id"]
    _move_head(client, instance_id, _reviewer_key)

    # The same preview coordinate no longer pins anything: state moved.
    stale = client.post(
        url,
        json={
            "principal": _principal(tmp_path, "latecomer"),
            "proposal_name": "add-latecomer",
            "at": at,
        },
    )
    _refused(stale, 409, "playbill.preview.state_moved")


def test_a_document_proposal_previews_and_writes_nothing(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    url = _api(instance_id, "/playbill/documents/proposals")
    body = {
        "shell": _document(client, instance_id, "preview-doc"),
        "proposal_name": "preview-doc",
        "dry_run": True,
    }

    preview = _ok(
        assert_writes_nothing(
            [tmp_path], lambda: client.post(url, json=body), warm=_warm(client, instance_id)
        )
    )

    assert preview["status"] in {"would_propose", "would_block"}
    assert _ok(client.get(_api(instance_id, "/playbill/proposals")))["entries"] == []


def test_decommissioning_previews_by_default_and_commits_only_with_its_coordinate(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    url = _api(instance_id, "/playbill/instance/decommission")

    preview = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(url, json={"reason": "end of trial"}),
            warm=_warm(client, instance_id),
        )
    )

    assert preview["status"] == "would_decommission"
    unconfirmed = client.post(url, json={"reason": "end of trial", "dry_run": False})
    _refused(unconfirmed, 400, "playbill.preview.confirmation_required")
    done = _ok(
        client.post(
            url,
            json={
                "reason": "end of trial",
                "dry_run": False,
                "at": preview["coordinate"]["git_oid"],
            },
        )
    )
    assert done["status"] == "decommissioned"


def test_binding_and_publishing_a_ledger_mirror_preview_and_write_nothing(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    remote = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    url = _api(instance_id, "/playbill/ledger/mirror")

    preview = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(url, json={"url": str(remote)}),
            warm=_warm(client, instance_id),
        )
    )
    assert preview["status"] == "would_publish"
    assert preview["coordinate"] is not None
    _refused(
        client.post(url, json={"url": str(remote), "dry_run": False}),
        400,
        "playbill.preview.confirmation_required",
    )
    _ok(
        client.post(
            url,
            json={"url": str(remote), "dry_run": False, "at": preview["coordinate"]["git_oid"]},
        )
    )
    publish = _api(instance_id, "/playbill/ledger/publish")
    _ok(client.post(publish, json={"timeout": 60}))

    previewed = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(publish, json={"timeout": 0, "dry_run": True}),
            warm=_warm(client, instance_id),
        )
    )
    assert previewed["status"] == "would_publish"


def _git_worktree(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_a_host_attaches_to_a_worktree_after_init_and_previews_first(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    """Q16: an initialized host takes a worktree in place; nothing is rebuilt."""

    client, instance_id, _reviewer_key = playbill_http
    worktree = _git_worktree(tmp_path / "worktree")

    preview = assert_writes_nothing(
        [tmp_path],
        lambda: host_api.playbill_host_workspace_attach(
            instance_id,
            workspace_root=str(worktree),
            workspace_attachment_authorized=True,
            dry_run=True,
        ),
        warm=_warm(client, instance_id),
    )
    assert (preview.status, preview.initialized) == ("would_attach", True)
    assert get_registry().get(instance_id).workspace_root is None  # type: ignore[union-attr]

    attached = host_api.playbill_host_workspace_attach(
        instance_id, workspace_root=str(worktree), workspace_attachment_authorized=True
    )
    assert attached.status == "attached"
    assert get_registry().get(instance_id).workspace_root == str(worktree)  # type: ignore[union-attr]
    again = host_api.playbill_host_workspace_attach(
        instance_id, workspace_root=str(worktree), workspace_attachment_authorized=True
    )
    assert again.status == "already_attached"
    # Let the advisory ref refresh the attach queued finish before comparing.
    get_playbill_manager().get(instance_id).settled_workspace_advertisement()

    detach_preview = assert_writes_nothing(
        [tmp_path],
        lambda: host_api.playbill_host_workspace_detach(
            instance_id, workspace_attachment_authorized=True, dry_run=True
        ),
    )
    assert detach_preview.status == "would_detach"
    assert get_registry().get(instance_id).workspace_root == str(worktree)  # type: ignore[union-attr]


def test_a_worktree_in_another_object_format_is_refused_by_name(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, instance_id, _reviewer_key = playbill_http
    worktree = tmp_path / "sha256-worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", "--object-format=sha256", str(worktree)], check=True)

    with pytest.raises(Exception) as refused:
        host_api.playbill_host_workspace_attach(
            instance_id, workspace_root=str(worktree), workspace_attachment_authorized=True
        )
    assert "object_format" in str(refused.value) or "sha256" in str(refused.value)
    assert get_registry().get(instance_id).workspace_root is None  # type: ignore[union-attr]
    del client


def test_allocating_a_host_previews(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, _instance_id, _reviewer_key = playbill_http

    preview = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(
                "/api/v1/runtime/instances", json={"instance_id": "inst_maybe", "dry_run": True}
            ),
            warm=_quiet,
        )
    )
    assert preview["status"] == "would_create"
    assert get_registry().get("inst_maybe") is None


@pytest.mark.parametrize("instance_id", ["bad/id", "nope", "inst_" + "x" * 80])
def test_a_host_preview_refuses_every_id_its_commit_refuses(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path, instance_id: str
) -> None:
    client, _instance_id, _reviewer_key = playbill_http
    route = "/api/v1/runtime/instances"

    preview = assert_writes_nothing(
        [tmp_path],
        lambda: client.post(route, json={"instance_id": instance_id, "dry_run": True}),
        warm=_quiet,
    )
    commit = client.post(route, json={"instance_id": instance_id})
    assert (preview.status_code, commit.status_code) == (400, 400), (preview.text, commit.text)
    assert preview.json()["error_code"] == commit.json()["error_code"]


def test_a_host_preview_refuses_a_location_another_row_holds(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, _instance_id, _reviewer_key = playbill_http
    registry = get_registry()
    with sqlite3.connect(registry.db_path) as conn:
        conn.execute(
            "INSERT INTO instances(instance_id, backend, location, workspace_root, created_at)"
            " VALUES ('inst_holder', 'governed_daemon', 'instances/inst_taken', NULL, 'x')"
        )
    route = "/api/v1/runtime/instances"

    preview = assert_writes_nothing(
        [tmp_path],
        lambda: client.post(route, json={"instance_id": "inst_taken", "dry_run": True}),
        warm=_quiet,
    )
    commit = client.post(route, json={"instance_id": "inst_taken"})
    assert (preview.status_code, commit.status_code) == (400, 400), (preview.text, commit.text)
    assert "inst_holder" in preview.json()["message"]
    assert preview.json()["message"] == commit.json()["message"]
    assert registry.get("inst_taken") is None


# --- operator credentials (auth on) ---------------------------------------------------

_SECRET = "exact-preview-bootstrap-secret"


@pytest.fixture
def auth_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "auth-state"))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", _SECRET)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    try:
        with TestClient(create_app()) as client:
            # As the daemon's start does before it serves.
            get_runtime_credential_store()
            _quiet()
            yield client
    finally:
        get_playbill_manager().clear()
        reset_runtime_credential_store()
        reset_registry()
        reset_permissions()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_bootstrap_claims_once_per_host_and_previews_claim_nothing(
    auth_daemon: TestClient, tmp_path: Path
) -> None:
    client = auth_daemon
    for instance_id in ("inst_host_one", "inst_host_two"):
        _ok(
            client.post(
                "/api/v1/runtime/instances",
                json={"instance_id": instance_id},
                headers=_bearer(_SECRET),
            )
        )
    claim = "/api/v1/inst_host_one/runtime/bootstrap/claim"

    preview = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(
                claim, json={"bootstrap_secret": _SECRET, "dry_run": True}, headers=_bearer(_SECRET)
            ),
        )
    )
    assert (preview["status"], preview["token"]) == ("would_claim", None)
    one = _ok(client.post(claim, json={"bootstrap_secret": _SECRET}, headers=_bearer(_SECRET)))
    two = _ok(
        client.post(
            "/api/v1/inst_host_two/runtime/bootstrap/claim",
            json={"bootstrap_secret": _SECRET},
            headers=_bearer(_SECRET),
        )
    )
    assert one["token"] and two["token"] and one["token"] != two["token"]


def test_credential_changes_preview_and_irreversible_ones_need_the_coordinate(
    auth_daemon: TestClient, tmp_path: Path
) -> None:
    client = auth_daemon
    instance_id = "inst_credentials"
    _ok(
        client.post(
            "/api/v1/runtime/instances",
            json={"instance_id": instance_id},
            headers=_bearer(_SECRET),
        )
    )
    admin = _ok(
        client.post(
            _api(instance_id, "/runtime/bootstrap/claim"),
            json={"bootstrap_secret": _SECRET},
            headers=_bearer(_SECRET),
        )
    )["token"]
    owner = generate_client_principal_key(
        tmp_path / "owner-custody", principal_id="owner", kind="ordinary", forbidden_roots=()
    )
    _ok(
        client.post(
            _api(instance_id, "/playbill/init"),
            json={"principals": [owner.principal.model_dump(mode="json")]},
            headers=_bearer(admin),
        )
    )
    mint_url = _api(instance_id, "/runtime/credentials")

    def mint_body() -> dict[str, Any]:
        proof = sign_runtime_credential_mint(
            instance_id=instance_id,
            principal_id="owner",
            permission_mode="read_only",
            label="owner",
            private_key_path=owner.private_key_path,
            forbidden_roots=(),
        )
        return {
            "principal_id": "owner",
            "permission_mode": "read_only",
            "principal_proof": proof.model_dump(mode="json"),
        }

    body = mint_body()
    preview = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(mint_url, json={**body, "dry_run": True}, headers=_bearer(admin)),
            warm=lambda: client.get(_api(instance_id, "/playbill/head"), headers=_bearer(admin)),
        )
    )
    assert (preview["status"], preview["token"]) == ("would_mint", None)
    # The preview spent nothing: the same signed consent still mints.
    minted = _ok(client.post(mint_url, json=body, headers=_bearer(admin)))
    assert minted["status"] == "minted" and minted["token"]
    credential_id = minted["credential"]["credential_id"]
    revoke_url = _api(instance_id, f"/runtime/credentials/{credential_id}/revoke")

    revoke_preview = _ok(
        assert_writes_nothing(
            [tmp_path], lambda: client.post(revoke_url, json={}, headers=_bearer(admin))
        )
    )
    assert revoke_preview["status"] == "would_revoke"
    assert revoke_preview["credential"]["revoked_at"] is None
    rotate_preview = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(
                _api(instance_id, f"/runtime/credentials/{credential_id}/rotate"),
                json={"principal_proof": mint_body()["principal_proof"]},
                headers=_bearer(admin),
            ),
        )
    )
    assert (rotate_preview["status"], rotate_preview["token"]) == ("would_rotate", None)
    _refused(
        client.post(revoke_url, json={"dry_run": False}, headers=_bearer(admin)),
        400,
        "playbill.preview.confirmation_required",
    )
    revoked = _ok(
        client.post(
            revoke_url,
            json={"dry_run": False, "at": revoke_preview["coordinate"]["git_oid"]},
            headers=_bearer(admin),
        )
    )
    assert revoked["status"] == "revoked"
    stored = get_runtime_credential_store().get(credential_id)
    assert stored is not None and stored.revoked_at is not None
    assert stored.permission_mode == PermissionMode.READ_ONLY


def test_a_claim_type_proposal_previews_and_writes_nothing(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    from cruxible_core.claims.claim_type_inputs import claim_type_input_template

    client, instance_id, _reviewer_key = playbill_http
    body = {
        "tag": "playbill-claim-type-input-propose-request-v1",
        "input": claim_type_input_template().model_dump(mode="json"),
        "proposal_name": "status-type",
        "dry_run": True,
    }

    preview = _ok(
        assert_writes_nothing(
            [tmp_path],
            lambda: client.post(_api(instance_id, "/playbill/claim-types/proposals"), json=body),
            warm=_warm(client, instance_id),
        )
    )

    assert preview["proposal"]["status"] in {"would_propose", "would_block"}
    assert "admission" not in preview["proposal"]["proposal"]
    assert _ok(client.get(_api(instance_id, "/playbill/proposals")))["entries"] == []
