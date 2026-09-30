"""A configured principal ID on an auth-off daemon: a checked claim, never a credential."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_client.contracts.principals import PRINCIPAL_ID_HEADER
from cruxible_client.errors import ConfigError as ClientConfigError
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import PermissionMode, reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry

INSTANCE = "inst_principal_claims"


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path]]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    registered = get_registry().create_governed_instance_with_id(INSTANCE)
    with TestClient(create_app()) as client:
        yield client, Path(registered.record.location)
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def _principal(tmp_path: Path, managed: Path, principal_id: str) -> dict[str, object]:
    material = generate_client_principal_key(
        tmp_path / f"{principal_id}-custody",
        principal_id=principal_id,
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    return material.principal.model_dump(mode="json")


def _init(client: TestClient, tmp_path: Path, managed: Path, *, claim: str | None) -> object:
    headers = {} if claim is None else {PRINCIPAL_ID_HEADER: claim}
    return client.post(
        f"/api/v1/{INSTANCE}/playbill/init",
        json={"principals": [_principal(tmp_path, managed, "alice")]},
        headers=headers,
    )


def test_init_under_a_claimed_principal_makes_it_the_owner_with_no_secret(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, managed = daemon

    initialized = _init(client, tmp_path, managed, claim="alice")

    assert initialized.status_code == 200, initialized.text  # type: ignore[attr-defined]
    who = client.get(
        f"/api/v1/{INSTANCE}/playbill/whoami", headers={PRINCIPAL_ID_HEADER: "alice"}
    ).json()
    assert who["actor_id"] == "alice"
    assert who["actor_id_source"] == "principal_claim"
    assert who["authenticated"] is False
    assert who["credential_label"] is None
    assert who["principal_registration_status"] == "active"


def test_init_names_the_owner_mismatch_and_the_command_that_repairs_it(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, managed = daemon

    refused = _init(client, tmp_path, managed, claim=None)

    assert refused.status_code == 403  # type: ignore[attr-defined]
    body = refused.json()  # type: ignore[attr-defined]
    assert body["error_code"] == "playbill.identity.init_owner_mismatch"
    assert "cruxible playbill init --principal-id ID --key-dir DIR" in body["message"]
    assert body["repair"]["operation"] == "playbill.init"


def test_an_unregistered_claim_reads_but_is_refused_every_write(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    mallory = {PRINCIPAL_ID_HEADER: "mallory"}

    listed = client.get(f"/api/v1/{INSTANCE}/playbill/principals", headers=mallory)
    who = client.get(f"/api/v1/{INSTANCE}/playbill/whoami", headers=mallory)
    withdrawn = client.post(
        f"/api/v1/{INSTANCE}/playbill/proposals/sha256:{'0' * 64}/withdraw",
        json={"reason": "not mine"},
        headers=mallory,
    )
    created = _create(client, mallory)

    # Reads stay open: an agent reads while its registration awaits activation.
    assert listed.status_code == 200, listed.text
    assert who.status_code == 200, who.text
    assert who.json()["actor_id"] == "mallory"
    assert who.json()["principal_registration_status"] == "absent"
    for refused in (withdrawn, created):
        assert refused.status_code == 403  # type: ignore[attr-defined]
        body = refused.json()  # type: ignore[attr-defined]
        assert body["error_code"] == "playbill.identity.principal_absent"
        assert "cruxible playbill principal add mallory --key-dir DIR" in body["message"]
        assert body["repair"] == {
            "operation": "playbill.principal.add",
            "arguments": {"principal_id": "mallory"},
        }


def test_a_malformed_claim_is_refused_before_it_names_anyone(
    daemon: tuple[TestClient, Path],
) -> None:
    client, _managed = daemon

    refused = client.get(
        f"/api/v1/{INSTANCE}/playbill/whoami", headers={PRINCIPAL_ID_HEADER: "Not An ID"}
    )

    assert refused.status_code == 400
    assert refused.json()["error_code"] == "playbill.identity.principal_claim_invalid"


def test_with_auth_on_a_claim_may_only_repeat_the_credentials_principal(
    daemon: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _managed = daemon
    created = get_runtime_credential_store().create_credential(
        instance_id=INSTANCE,
        label="alice",
        permission_mode=PermissionMode.READ_ONLY,
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    headers = {"Authorization": f"Bearer {created.token}", PRINCIPAL_ID_HEADER: "bob"}

    refused = client.get(f"/api/v1/{INSTANCE}/playbill/whoami", headers=headers)

    assert refused.status_code == 401
    assert refused.json()["error_code"] == "playbill.identity.principal_claim_mismatch"


def test_the_client_sends_its_configured_principal_and_refuses_a_malformed_one(
    tmp_path: Path,
) -> None:
    socket_path = str(tmp_path / "unused.sock")
    client = CruxibleClient(socket_path=socket_path, principal_id="alice")
    try:
        assert client._client._client.headers[PRINCIPAL_ID_HEADER] == "alice"
    finally:
        client.close()
    with pytest.raises(ClientConfigError, match="CRUXIBLE_PRINCIPAL_ID"):
        CruxibleClient(socket_path=socket_path, principal_id="Not An ID")


def _create(client: TestClient, headers: dict[str, str]) -> object:
    from tests.test_authoring.test_authoring_preflight import _self_source_payload

    return client.post(
        f"/api/v1/{INSTANCE}/playbill/authoring/intents",
        json={
            "tag": "playbill-authoring-intent-create-request-v1",
            "payload": _self_source_payload().model_dump(mode="json"),
        },
        headers=headers,
    )


def test_whoami_says_whether_the_actor_can_author_and_create_refuses_with_the_same_repair(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]

    anonymous = client.get(f"/api/v1/{INSTANCE}/playbill/whoami").json()
    refused = _create(client, {})
    alice = client.get(
        f"/api/v1/{INSTANCE}/playbill/whoami", headers={PRINCIPAL_ID_HEADER: "alice"}
    ).json()
    created = _create(client, {PRINCIPAL_ID_HEADER: "alice"})

    assert anonymous["can_author"] is False
    refusal = anonymous["authoring_refusal"]
    assert refusal["code"] == "playbill.identity.principal_unconfigured"
    assert "CRUXIBLE_PRINCIPAL_ID" in refusal["detail"]
    assert "active principals: alice" in refusal["detail"]
    assert refused.status_code == 403  # type: ignore[attr-defined]
    body = refused.json()  # type: ignore[attr-defined]
    assert body["error_code"] == refusal["code"]
    assert body["repair"] == refusal["repair"]
    assert alice["can_author"] is True
    assert alice["authoring_refusal"] is None
    assert created.status_code == 200, created.text  # type: ignore[attr-defined]


def test_a_read_only_credential_is_told_it_cannot_author_and_how_to_get_the_tier(
    daemon: tuple[TestClient, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    reader = get_runtime_credential_store().create_credential(
        instance_id=INSTANCE,
        label="alice-reader",
        permission_mode=PermissionMode.READ_ONLY,
        principal_id="alice",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

    who = client.get(
        f"/api/v1/{INSTANCE}/playbill/whoami",
        headers={"Authorization": f"Bearer {reader.token}"},
    ).json()

    assert who["can_author"] is False
    assert who["authoring_refusal"]["code"] == "playbill.identity.permission_insufficient"
    assert who["authoring_refusal"]["repair"] == {
        "operation": "credential.mint",
        "arguments": {"principal_id": "alice", "permission_mode": "governed_write"},
    }


def test_authoring_create_refuses_a_non_principal_before_any_work(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.errors import PrincipalRefusedError
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.test_authoring.test_authoring_preflight import _self_source_payload

    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    instance = get_playbill_manager().get(INSTANCE)

    class _UntouchedStore:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"a refused actor reached the intent store ({name})")

    coordinator = AuthoringIntentCoordinator(instance=instance, store=_UntouchedStore())  # type: ignore[arg-type]

    with pytest.raises(PrincipalRefusedError) as refused:
        coordinator.create(
            actor=AuthenticatedActor(actor_id="mallory"),
            payload=_self_source_payload(),
            canonical_timestamp="2026-09-29T00:00:00.000000Z",
        )

    assert refused.value.error_code == "playbill.identity.principal_absent"
    assert "cruxible playbill principal add mallory --key-dir DIR" in str(refused.value)


_OPERATIONAL_SECTIONS = (
    "runs",
    "running",
    "lines",
    "captures",
    "capture_contracts",
    "predictions",
    "mandates",
)


def test_an_unregistered_claim_reads_orient_and_every_operational_section(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """Reads stay open for any claim; orient's you block names the shared refusal."""

    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    mallory = {PRINCIPAL_ID_HEADER: "mallory"}

    base = client.get(f"/api/v1/{INSTANCE}/playbill/orient", headers=mallory)
    assert base.status_code == 200, base.text
    you = base.json()["you"]
    assert you["can_author"] is False
    assert you["authoring_refusal"]["code"] == "playbill.identity.principal_absent"
    for section in _OPERATIONAL_SECTIONS:
        page = client.get(
            f"/api/v1/{INSTANCE}/playbill/orient", params={"section": section}, headers=mallory
        )
        assert page.status_code == 200, (section, page.text)
        assert page.json()["section"] == section


@pytest.mark.parametrize(
    ("headers", "code", "repair"),
    [
        (
            {PRINCIPAL_ID_HEADER: "mallory"},
            "playbill.identity.principal_absent",
            {"operation": "playbill.principal.add", "arguments": {"principal_id": "mallory"}},
        ),
        (
            {},
            "playbill.identity.principal_unconfigured",
            {
                "operation": "playbill.principal.list",
                "arguments": {"configure": "CRUXIBLE_PRINCIPAL_ID"},
            },
        ),
    ],
    ids=["unregistered_claim", "implicit_operator"],
)
@pytest.mark.parametrize("dry_run", [True, False])
def test_the_write_verbs_refuse_a_caller_that_cannot_author_with_the_whoami_repair(
    daemon: tuple[TestClient, Path],
    tmp_path: Path,
    headers: dict[str, str],
    code: str,
    repair: dict[str, object],
    dry_run: bool,
) -> None:
    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    who = client.get(f"/api/v1/{INSTANCE}/playbill/whoami", headers=headers).json()
    assert who["authoring_refusal"]["code"] == code

    change = {"subject": "project.work_item/wi-1", "field": "status", "value": "ready"}
    requests = {
        "set": {"because": "b", "dry_run": dry_run, **change},
        "write": {
            "because": "b",
            "dry_run": dry_run,
            "accept": "if_allowed",
            "changes": [{"op": "set", **change}],
        },
        "retire": {
            "because": "b",
            "dry_run": dry_run,
            "target": {"subject": change["subject"], "field": "status"},
        },
    }
    for verb, body in requests.items():
        refused = client.post(f"/api/v1/{INSTANCE}/playbill/{verb}", json=body, headers=headers)
        assert refused.status_code == 403, (verb, refused.text)
        answer = refused.json()
        assert answer["error_code"] == code, verb
        assert answer["repair"] == repair == who["authoring_refusal"]["repair"], verb
