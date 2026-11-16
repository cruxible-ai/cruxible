"""A configured principal ID on an auth-off daemon: a checked claim, never a credential."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient, contracts
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
        f"/api/v1/{INSTANCE}/init",
        json={"principals": [_principal(tmp_path, managed, "alice")]},
        headers=headers,
    )


def test_init_under_a_claimed_principal_makes_it_the_owner_with_no_secret(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, managed = daemon

    initialized = _init(client, tmp_path, managed, claim="alice")

    assert initialized.status_code == 200, initialized.text  # type: ignore[attr-defined]
    who = client.get(f"/api/v1/{INSTANCE}/whoami", headers={PRINCIPAL_ID_HEADER: "alice"}).json()
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
    assert body["error_code"] == "cruxible.identity.init_owner_mismatch"
    assert "cruxible init --principal-id ID --key-dir DIR" in body["message"]
    assert body["repair"]["operation"] == "cruxible.init"


def test_an_unregistered_claim_reads_but_is_refused_every_write(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    mallory = {PRINCIPAL_ID_HEADER: "mallory"}

    listed = client.get(
        f"/api/v1/{INSTANCE}/orient", params={"section": "principals"}, headers=mallory
    )
    who = client.get(f"/api/v1/{INSTANCE}/whoami", headers=mallory)
    withdrawn = client.post(
        f"/api/v1/{INSTANCE}/proposals/sha256:{'0' * 64}/withdraw",
        json={"reason": "not mine"},
        headers=mallory,
    )
    compiled = _compile(client, mallory)

    # Reads stay open: an agent reads while its registration awaits activation.
    assert listed.status_code == 200, listed.text
    assert who.status_code == 200, who.text
    assert who.json()["actor_id"] == "mallory"
    assert who.json()["principal_registration_status"] == "absent"
    for refused in (withdrawn, compiled):
        assert refused.status_code == 403  # type: ignore[attr-defined]
        body = refused.json()  # type: ignore[attr-defined]
        assert body["error_code"] == "cruxible.identity.principal_absent"
        assert "an owner adds principal mallory with a new key directory" in body["message"]
        assert body["repair"] == {
            "operation": "cruxible.principal.add",
            "arguments": {"principal_id": "mallory"},
        }


def test_the_implicit_operator_may_not_settle_or_close_a_proposal(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """An auth-off request naming no principal acts as ``operator``, which is no
    principal: every door that records who settled or closed a proposal refuses it
    with the whoami repair, before any proposal is read (gapa-05)."""

    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    proposal = f"sha256:{'0' * 64}"
    base = f"/api/v1/{INSTANCE}/proposals/{proposal}"
    attestation = {
        "signer_id": "alice",
        "signing_semantic_root": f"sha256:{'1' * 64}",
        "payload_digest": f"sha256:{'2' * 64}",
        "sig": "3" * 128,
    }

    who = client.get(f"/api/v1/{INSTANCE}/whoami").json()
    refused = {
        "activate": client.post(f"{base}/activate"),
        "approve": client.post(f"{base}/approvals", json={"attestation": attestation}),
        "reject": client.post(f"{base}/reject", json={"reason": "no"}),
        "withdraw": client.post(f"{base}/withdraw", json={"reason": "no"}),
        "readmit": client.post(f"{base}/readmit", json={"dry_run": True}),
    }

    assert who["actor_id_source"] == "local_operator"
    assert who["authoring_refusal"]["code"] == "cruxible.identity.principal_unconfigured"
    for door, response in refused.items():
        assert response.status_code == 403, (door, response.text)
        body = response.json()
        assert body["error_code"] == "cruxible.identity.principal_unconfigured", door
        assert "set CRUXIBLE_PRINCIPAL_ID" in body["message"], door
        assert body["repair"] == who["authoring_refusal"]["repair"], door


def test_a_malformed_claim_is_refused_before_it_names_anyone(
    daemon: tuple[TestClient, Path],
) -> None:
    client, _managed = daemon

    refused = client.get(f"/api/v1/{INSTANCE}/whoami", headers={PRINCIPAL_ID_HEADER: "Not An ID"})

    assert refused.status_code == 400
    assert refused.json()["error_code"] == "cruxible.identity.principal_claim_invalid"


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

    refused = client.get(f"/api/v1/{INSTANCE}/whoami", headers=headers)

    assert refused.status_code == 401
    assert refused.json()["error_code"] == "cruxible.identity.principal_claim_mismatch"


def test_a_client_ceiling_lowers_a_credential_and_never_raises_it(
    daemon: tuple[TestClient, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.principals import EFFECTIVE_PERMISSION_MODE_HEADER

    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    created = get_runtime_credential_store().create_credential(
        instance_id=INSTANCE,
        label="alice",
        permission_mode=PermissionMode.GOVERNED_WRITE,
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    bearer = {"Authorization": f"Bearer {created.token}"}

    def mode(ceiling: str) -> object:
        return client.get(
            f"/api/v1/{INSTANCE}/whoami",
            headers={**bearer, EFFECTIVE_PERMISSION_MODE_HEADER: ceiling},
        )

    lowered, asked_higher = mode("read_only"), mode("admin")

    assert lowered.status_code == 200, lowered.text  # type: ignore[attr-defined]
    assert lowered.json()["credential_permission_mode"] == "read_only"  # type: ignore[attr-defined]
    # A ceiling above the credential is the credential: a client cannot raise itself.
    assert asked_higher.status_code == 200, asked_higher.text  # type: ignore[attr-defined]
    assert asked_higher.json()["credential_permission_mode"] == "governed_write"  # type: ignore[attr-defined]


def test_the_client_sends_its_configured_principal_and_refuses_a_malformed_one(
    tmp_path: Path,
) -> None:
    socket_path = str(tmp_path / "unused.sock")
    client = CruxibleClient(socket_path=socket_path, principal_id="alice")
    try:
        assert client._client._client.headers[PRINCIPAL_ID_HEADER] == "alice"
    finally:
        client.close()
    capped = CruxibleClient(socket_path=socket_path, permission_ceiling="governed_write")
    try:
        assert (
            capped._client._client.headers["X-Cruxible-Effective-Permission-Mode"]
            == "governed_write"
        )
    finally:
        capped.close()
    with pytest.raises(ClientConfigError, match="CRUXIBLE_PRINCIPAL_ID"):
        CruxibleClient(socket_path=socket_path, principal_id="Not An ID")


def _compile(client: TestClient, headers: dict[str, str]) -> object:
    from tests.test_authoring.test_authoring_preflight import _self_source_payload

    return client.post(
        f"/api/v1/{INSTANCE}/authoring/compile",
        json={
            "tag": "cruxible-authoring-intent-compile-request-v4",
            "payload": _self_source_payload().model_dump(mode="json"),
        },
        headers=headers,
    )


def test_whoami_says_whether_the_actor_can_author_and_compile_refuses_with_the_same_repair(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]

    anonymous = client.get(f"/api/v1/{INSTANCE}/whoami").json()
    refused = _compile(client, {})
    alice = client.get(f"/api/v1/{INSTANCE}/whoami", headers={PRINCIPAL_ID_HEADER: "alice"}).json()
    compiled = _compile(client, {PRINCIPAL_ID_HEADER: "alice"})

    assert anonymous["can_author"] is False
    refusal = anonymous["authoring_refusal"]
    assert refusal["code"] == "cruxible.identity.principal_unconfigured"
    assert "CRUXIBLE_PRINCIPAL_ID" in refusal["detail"]
    assert "active principals: alice" in refusal["detail"]
    assert refused.status_code == 403  # type: ignore[attr-defined]
    body = refused.json()  # type: ignore[attr-defined]
    assert body["error_code"] == refusal["code"]
    assert body["repair"] == refusal["repair"]
    assert alice["can_author"] is True
    assert alice["authoring_refusal"] is None
    assert compiled.status_code == 200, compiled.text  # type: ignore[attr-defined]


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
        f"/api/v1/{INSTANCE}/whoami",
        headers={"Authorization": f"Bearer {reader.token}"},
    ).json()

    assert who["can_author"] is False
    assert who["authoring_refusal"]["code"] == "cruxible.identity.permission_insufficient"
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

    assert refused.value.error_code == "cruxible.identity.principal_absent"
    assert "an owner adds principal mallory with a new key directory" in str(refused.value)


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

    base = client.get(f"/api/v1/{INSTANCE}/orient", headers=mallory)
    assert base.status_code == 200, base.text
    you = base.json()["you"]
    assert you["can_author"] is False
    assert you["authoring_refusal"]["code"] == "cruxible.identity.principal_absent"
    for section in _OPERATIONAL_SECTIONS:
        page = client.get(
            f"/api/v1/{INSTANCE}/orient", params={"section": section}, headers=mallory
        )
        assert page.status_code == 200, (section, page.text)
        assert page.json()["section"] == section


@pytest.mark.parametrize(
    ("headers", "code", "repair"),
    [
        (
            {PRINCIPAL_ID_HEADER: "mallory"},
            "cruxible.identity.principal_absent",
            {"operation": "cruxible.principal.add", "arguments": {"principal_id": "mallory"}},
        ),
        (
            {},
            "cruxible.identity.principal_unconfigured",
            {
                "operation": "cruxible.orient",
                "arguments": {"section": "principals", "configure": "CRUXIBLE_PRINCIPAL_ID"},
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
    who = client.get(f"/api/v1/{INSTANCE}/whoami", headers=headers).json()
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
        refused = client.post(f"/api/v1/{INSTANCE}/{verb}", json=body, headers=headers)
        assert refused.status_code == 403, (verb, refused.text)
        answer = refused.json()
        assert answer["error_code"] == code, verb
        assert answer["repair"] == repair == who["authoring_refusal"]["repair"], verb


# Every write whose record names who did it, by operation group: an auth-off
# request naming no principal acts as ``operator``, which is no principal, so each
# is refused with the whoami refusal and repair before it reads or records
# anything (ruling 5, 2026-11-16). Requests are built unvalidated: the refusal
# comes before any field past the preview flags is read.
def _attributed_writes() -> dict[str, dict[str, Callable[[], object]]]:
    from cruxible_client.contracts.attestation_evidence import ClaimAttestationEvidenceRequest
    from cruxible_client.contracts.capture_erasure import CaptureEraseRequest
    from cruxible_client.contracts.claim_attestations import ClaimAttestationAppendRequest
    from cruxible_client.contracts.kits import KitAddRequest, KitBuildRequest, KitRemoveRequest
    from cruxible_client.contracts.predictions import PredictRequest, SettleRequest
    from cruxible_client.contracts.provider_installation import (
        ProviderInstallRequest,
        ProviderMoveRequest,
        ProviderRetireRequest,
    )
    from cruxible_core.runtime import playbill_api as api
    from cruxible_core.service.discovery.curation import PlaybillBlockObserveRequestV1
    from cruxible_core.service.procedures.procedure_runs import LineRunRequest, ProcedureRunRequest

    preview = {"dry_run": True}
    return {
        "evidence": {
            "erase_capture": lambda: api.playbill_erase_capture(
                INSTANCE, CaptureEraseRequest.model_construct(dry_run=True, at=None)
            ),
            "attest_evidence": lambda: api.playbill_prepare_claim_attestation_evidence(
                INSTANCE, request=ClaimAttestationEvidenceRequest.model_construct()
            ),
            "attest_append": lambda: api.playbill_append_claim_attestation(
                INSTANCE, request=ClaimAttestationAppendRequest.model_construct()
            ),
        },
        "predictions": {
            "predict": lambda: api.playbill_predict(
                INSTANCE, request=PredictRequest.model_construct()
            ),
            "settle": lambda: api.playbill_settle_prediction(
                INSTANCE, "pred_x", request=SettleRequest.model_construct()
            ),
        },
        "instance": {
            "decommission": lambda: api.playbill_instance_decommission(
                INSTANCE, reason="done", dry_run=True
            ),
            "rebuild_next": lambda: api.playbill_instance_rebuild_next(INSTANCE),
        },
        "providers": {
            "install": lambda: api.playbill_provider_install(
                INSTANCE, ProviderInstallRequest.model_construct(**preview)
            ),
            "retire": lambda: api.playbill_provider_retire(
                INSTANCE, ProviderRetireRequest.model_construct(**preview)
            ),
            "move": lambda: api.playbill_provider_move(
                INSTANCE, ProviderMoveRequest.model_construct(**preview)
            ),
        },
        "kits": {
            "build": lambda: api.playbill_kit_build(
                INSTANCE, KitBuildRequest.model_construct(automation_template=None)
            ),
            "add": lambda: api.playbill_kit_add(INSTANCE, KitAddRequest.model_construct(**preview)),
            "remove": lambda: api.playbill_kit_remove(
                INSTANCE, KitRemoveRequest.model_construct(**preview)
            ),
        },
        "governance_proposals": {
            "compiler_upgrade": lambda: api.playbill_propose_compiler_upgrade(
                INSTANCE, dry_run=True
            ),
            "principal_change": lambda: api.playbill_propose_principal_change(
                INSTANCE,
                principal=None,  # type: ignore[arg-type]
                proposal_name="p",
                dry_run=True,
            ),
        },
        "measurements": {
            "measure": lambda: api.playbill_procedure_measure(
                INSTANCE,
                "absent",
                request=None,  # type: ignore[arg-type]
            ),
            "dispose": lambda: api.playbill_procedure_dispose(
                INSTANCE,
                "absent",
                request=None,  # type: ignore[arg-type]
            ),
        },
        "inbox_and_blocks": {
            "inbox_ack": lambda: api.playbill_inbox_ack(INSTANCE, "INB-absent"),
            "inbox_dismiss": lambda: api.playbill_inbox_dismiss(
                INSTANCE, "INB-absent", reason="no"
            ),
            "inbox_link": lambda: api.playbill_inbox_link(
                INSTANCE,
                "INB-absent",
                target=None,  # type: ignore[arg-type]
            ),
            "block_declare": lambda: api.playbill_block_declare(
                INSTANCE,
                None,  # type: ignore[arg-type]
            ),
            "block_observe": lambda: api.playbill_block_observe(
                INSTANCE, request=PlaybillBlockObserveRequestV1.model_construct(dry_run=None)
            ),
        },
        "runs": {
            "procedure_run": lambda: api.playbill_procedure_run(
                INSTANCE, "absent", request=ProcedureRunRequest(input={})
            ),
            "action_run": lambda: api.playbill_action_run(
                INSTANCE, request=contracts.ActionRunRequest(action="evidence.sweep")
            ),
            "line_run": lambda: api.playbill_line_run(
                INSTANCE, "sha256:" + "0" * 64, request=LineRunRequest.model_construct()
            ),
            "line_evaluate": lambda: api.playbill_line_evaluate(
                INSTANCE,
                "absent",
                request=contracts.LineEvaluateRequest.model_construct(dry_run=False),
            ),
            "line_dispatch": lambda: api.playbill_line_dispatch(
                INSTANCE, "absent", request=contracts.LineDispatchRequest.model_construct()
            ),
            "runbook_run": lambda: api.playbill_runbook_run(INSTANCE, "absent"),
        },
    }


@pytest.mark.parametrize(
    "group",
    [
        "evidence",
        "predictions",
        "instance",
        "providers",
        "kits",
        "governance_proposals",
        "measurements",
        "inbox_and_blocks",
        "runs",
    ],
)
def test_the_implicit_operator_may_not_make_an_attributed_write(
    daemon: tuple[TestClient, Path], tmp_path: Path, group: str
) -> None:
    from cruxible_core.errors import PrincipalRefusedError

    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    who = client.get(f"/api/v1/{INSTANCE}/whoami").json()
    assert who["actor_id_source"] == "local_operator"

    for door, call in _attributed_writes()[group].items():
        with pytest.raises(PrincipalRefusedError) as refused:
            call()
        assert refused.value.error_code == "cruxible.identity.principal_unconfigured", door
        assert "set CRUXIBLE_PRINCIPAL_ID" in str(refused.value), door
        assert refused.value.repair is not None, door
        assert refused.value.repair.model_dump(mode="json") == who["authoring_refusal"]["repair"]


def test_the_documented_local_operator_acts_stay_open(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """Line enable and disable and Capture uploads are the local operator's own
    acts: they are refused for what they name, never for who asks."""

    from cruxible_client.contracts.uploads import CaptureUploadOpenRequest
    from cruxible_core.errors import PrincipalRefusedError
    from cruxible_core.runtime import playbill_api as api

    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]

    upload = CaptureUploadOpenRequest(filename="a.txt", byte_length=1, sha256="sha256:" + "0" * 64)
    acts: dict[str, Callable[[], object]] = {
        # This daemon sets no upload root, so the open is refused for that instead.
        "upload_open": lambda: api.playbill_capture_upload_open(INSTANCE, upload),
        "line_enable": lambda: api.playbill_line_enable(INSTANCE, "absent", dry_run=True),
        "line_disable": lambda: api.playbill_line_disable(INSTANCE, "absent", dry_run=True),
    }
    for act, call in acts.items():
        with pytest.raises(Exception) as refused:
            call()
        assert not isinstance(refused.value, PrincipalRefusedError), (act, refused.value)


def test_a_principal_registered_as_operator_makes_attributed_writes(
    daemon: tuple[TestClient, Path], tmp_path: Path
) -> None:
    from cruxible_core.runtime import playbill_api as api

    client, managed = daemon
    initialized = client.post(
        f"/api/v1/{INSTANCE}/init",
        json={"principals": [_principal(tmp_path, managed, "operator")]},
    )
    assert initialized.status_code == 200, initialized.text

    rebuilt = api.playbill_instance_rebuild_next(INSTANCE)
    previewed = api.playbill_instance_decommission(INSTANCE, reason="done", dry_run=True)

    assert rebuilt.instance_id == INSTANCE
    assert previewed.status == "would_decommission"
    assert previewed.decommissioned_by == "operator"


def test_every_client_surface_shows_the_attributed_write_refusal_and_its_repair(
    daemon: tuple[TestClient, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK keeps the code and served repair, the CLI prints both, and MCP
    appends the repair in its own spelling, as for authoring."""

    from click.testing import CliRunner
    from mcp.server.fastmcp.exceptions import ToolError

    from cruxible_core.cli.main import cli
    from cruxible_core.mcp.server import _tool_failure_message

    client, managed = daemon
    assert _init(client, tmp_path, managed, claim="alice").status_code == 200  # type: ignore[attr-defined]
    repair = client.get(f"/api/v1/{INSTANCE}/whoami").json()["authoring_refusal"]["repair"]
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = client  # type: ignore[assignment]

    with pytest.raises(Exception) as sdk:
        transport.rebuild_next(INSTANCE)
    assert getattr(sdk.value, "error_code", None) == "cruxible.identity.principal_unconfigured"
    assert getattr(sdk.value, "repair").model_dump(mode="json") == repair

    tool_error = ToolError(str(sdk.value))
    tool_error.__cause__ = sdk.value
    mcp_message = _tool_failure_message("cruxible_instance_rebuild_next", tool_error)
    assert "cruxible.identity.principal_unconfigured" in mcp_message
    assert "Repair:" in mcp_message

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_ID", raising=False)
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            INSTANCE,
            "instance",
            "rebuild-next",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "cruxible.identity.principal_unconfigured" in result.output
    assert "Repair: cruxible orient" in result.output
