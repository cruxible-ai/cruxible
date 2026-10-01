"""Regression (review P2-1): a Line or run card never shows another principal's credential."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
from cruxible_client.contracts.operational_reads import (
    PlaybillGetLineCardV1,
    PlaybillGetProcedureRunCardV1,
)
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.operational import OperationalViewer
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_line_arming import CREDENTIAL, _armed_world
from tests.test_procedures.test_line_triggers import capture

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=False)


def _get(instance: Any, ref: str, viewer: OperationalViewer | None = None, **fields: Any):  # type: ignore[no-untyped-def]
    return service_playbill_get(
        instance, request=PlaybillGetRequestV1(ref=ref, **fields), access=_ACCESS, viewer=viewer
    )


@pytest.fixture(scope="module")
def credential_world(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    from cruxible_core.runtime.line_arms import dispatch_armed_line
    from cruxible_core.service.procedures.line_dispatch import armed_work
    from tests.test_procedures.test_line_arming import _credential, _manager, _match

    instance, line, procedure, start = _armed_world(
        tmp_path_factory.mktemp("credential"), principal=CREDENTIAL
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))
    patch = pytest.MonkeyPatch()
    from types import SimpleNamespace

    from cruxible_core.runtime import line_arms

    record = _credential(instance_id=instance.descriptor.instance_id)
    patch.setattr(
        line_arms, "get_runtime_credential_store", lambda: SimpleNamespace(get=lambda _id: record)
    )
    try:
        dispatched = dispatch_armed_line(
            _manager(instance),
            instance.descriptor.instance_id,
            arm,
            now=start + timedelta(seconds=3),
        )
    finally:
        patch.undo()
    assert dispatched is not None
    (run_id,) = [item.run_id for item in dispatched.items if item.run_id is not None]
    return instance, line, run_id, start + timedelta(seconds=4)


def test_another_principals_credential_is_withheld_from_the_line_and_run_cards(
    credential_world,  # type: ignore[no-untyped-def]
) -> None:
    instance, line, run_id, when = credential_world

    for viewer in (None, OperationalViewer(credential_id="cred-someone-else", admin=False)):
        card = _get(instance, line.identity.qualified, viewer, evaluation_time=when).card
        assert isinstance(card, PlaybillGetLineCardV1)
        (arm,) = card.arms
        assert arm.principal_kind == "runtime_credential"
        assert arm.credential is None and arm.armed_by is None and arm.armed_by_withheld
        dumped = str(card.model_dump(mode="json"))
        assert "cred-arm" not in dumped and "line-operator" not in dumped

        run = _get(instance, f"ProcedureRun:{run_id}", viewer).card
        assert isinstance(run, PlaybillGetProcedureRunCardV1) and run.triggered_by is not None
        assert run.triggered_by.armed_by is None
        assert "line-operator" not in str(run.model_dump(mode="json"))


@pytest.mark.parametrize(
    "viewer",
    [
        OperationalViewer(credential_id="cred-arm", admin=False),
        OperationalViewer(credential_id=None, admin=True),
    ],
)
def test_the_arming_credential_or_an_admin_sees_it(credential_world, viewer) -> None:  # type: ignore[no-untyped-def]
    instance, line, _run_id, when = credential_world

    card = _get(instance, line.identity.qualified, viewer, evaluation_time=when).card

    assert isinstance(card, PlaybillGetLineCardV1)
    (arm,) = card.arms
    assert (arm.credential, arm.armed_by) == ("cred-arm", "line-operator")
    assert not arm.armed_by_withheld


def _resolver(credential_id: str) -> str | None:
    return {"cred-arm": "owner", "cred-unbound": None}.get(credential_id)


@pytest.mark.parametrize(
    ("viewer", "visible"),
    [
        # Another credential bound to the arming credential's principal (a
        # rotation's replacement, or a second seat) sees it.
        (
            OperationalViewer(
                credential_id="cred-rotated",
                admin=False,
                principal_id="owner",
                credential_principal=_resolver,
            ),
            True,
        ),
        # A credential bound to another principal does not.
        (
            OperationalViewer(
                credential_id="cred-reviewer",
                admin=False,
                principal_id="reviewer",
                credential_principal=_resolver,
            ),
            False,
        ),
        # An unbound credential never widens, whatever it could resolve.
        (
            OperationalViewer(
                credential_id="cred-unbound", admin=False, credential_principal=_resolver
            ),
            False,
        ),
        # A principal without a resolver (no bearer credential) never widens.
        (OperationalViewer(credential_id=None, admin=False, principal_id="owner"), False),
    ],
    ids=["same_principal", "other_principal", "unbound", "claim"],
)
def test_a_credential_bound_to_the_arming_principal_sees_it_and_no_one_else(
    credential_world,  # type: ignore[no-untyped-def]
    viewer: OperationalViewer,
    visible: bool,
) -> None:
    instance, line, run_id, when = credential_world

    card = _get(instance, line.identity.qualified, viewer, evaluation_time=when).card
    assert isinstance(card, PlaybillGetLineCardV1)
    (arm,) = card.arms
    run = _get(instance, f"ProcedureRun:{run_id}", viewer).card
    assert isinstance(run, PlaybillGetProcedureRunCardV1) and run.triggered_by is not None
    if visible:
        assert (arm.credential, arm.armed_by) == ("cred-arm", "line-operator")
        assert run.triggered_by.armed_by == "line-operator" and run.actor == "owner"
    else:
        assert arm.credential is None and arm.armed_by is None and arm.armed_by_withheld
        assert run.triggered_by.armed_by is None and run.actor is None


def test_the_runtime_get_passes_the_authenticated_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import PermissionMode
    from cruxible_core.server.auth import ResolvedAuthContext

    seen: dict[str, Any] = {}

    def service(instance: Any, **values: Any) -> Any:
        seen.update(values)
        raise RuntimeError("stop")

    monkeypatch.setattr("cruxible_core.service.discovery.get.service_playbill_get", service)
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_a, **_k: None)
    monkeypatch.setattr(
        playbill_api,
        "get_playbill_manager",
        lambda: type("M", (), {"get": lambda self, _i: None})(),
    )
    monkeypatch.setattr(playbill_api, "_access", lambda *_a, **_k: _ACCESS)
    monkeypatch.setattr(
        playbill_api,
        "get_current_auth_context",
        lambda: ResolvedAuthContext(
            credential_id="cred-reader",
            credential_label="reader",
            principal_id="reader",
            credential_type="runtime_credential",
            instance_scope="inst",
            role=None,
            effective_permission_mode=PermissionMode.READ_ONLY,
        ),
    )
    monkeypatch.setattr(playbill_api, "get_current_mode", lambda: PermissionMode.READ_ONLY)

    with pytest.raises(RuntimeError):
        playbill_api.playbill_get("inst", request=PlaybillGetRequestV1(ref="Line:x"))

    viewer = seen["viewer"]
    assert viewer == OperationalViewer(
        credential_id="cred-reader", admin=False, principal_id="reader"
    )
    assert viewer.credential_principal is not None


@pytest.mark.parametrize(
    ("credential_type", "credential_id", "expected_credential"),
    [("runtime_credential", "cred-unbound", "cred-unbound"), ("principal_claim", None, None)],
)
def test_an_unbound_credential_or_a_claim_gets_no_principal_widening(
    monkeypatch: pytest.MonkeyPatch,
    credential_type: str,
    credential_id: str | None,
    expected_credential: str | None,
) -> None:
    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import PermissionMode
    from cruxible_core.server.auth import ResolvedAuthContext

    seen: dict[str, Any] = {}

    def service(instance: Any, **values: Any) -> Any:
        seen.update(values)
        raise RuntimeError("stop")

    monkeypatch.setattr("cruxible_core.service.discovery.get.service_playbill_get", service)
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_a, **_k: None)
    monkeypatch.setattr(
        playbill_api,
        "get_playbill_manager",
        lambda: type("M", (), {"get": lambda self, _i: None})(),
    )
    monkeypatch.setattr(playbill_api, "_access", lambda *_a, **_k: _ACCESS)
    monkeypatch.setattr(
        playbill_api,
        "get_current_auth_context",
        lambda: ResolvedAuthContext(
            credential_id=credential_id,
            credential_label=None if credential_id is None else "manager",
            credential_type=credential_type,  # type: ignore[arg-type]
            instance_scope="inst",
            role=None,
            effective_permission_mode=PermissionMode.GOVERNED_WRITE,
            # A claim names a principal; an unbound credential names none.
            principal_id="owner" if credential_type == "principal_claim" else None,
        ),
    )
    monkeypatch.setattr(playbill_api, "get_current_mode", lambda: PermissionMode.GOVERNED_WRITE)

    with pytest.raises(RuntimeError):
        playbill_api.playbill_get("inst", request=PlaybillGetRequestV1(ref="Line:x"))

    viewer = seen["viewer"]
    assert viewer.credential_id == expected_credential
    assert viewer.principal_id is None and viewer.credential_principal is None


@pytest.mark.parametrize("historical", [False, True])
def test_a_run_proof_reads_live_and_withholds_another_principals_credential(
    credential_world,  # type: ignore[no-untyped-def]
    historical: bool,
) -> None:
    """Regression (review r2): a run's proof raised IndexError on its bare RUN- id."""

    instance, _line, run_id, _when = credential_world
    at = instance.accepted_history()[0].oid if historical else None

    hidden = _get(instance, f"ProcedureRun:{run_id}", None, detail="proof", at=at)

    assert hidden.proof is not None and hidden.proof["run_id"] == run_id
    assert hidden.live is not None and hidden.live.fields == ("proof",)
    assert hidden.live.as_of.generation == instance.accepted_history()[-1].sequence
    dumped = str(hidden.proof)
    assert "line-operator" not in dumped and "cred-arm" not in dumped and "owner" not in dumped
    assert hidden.proof["attribution"]["tag"] == "playbill-procedure-run-attribution-withheld-v1"
    assert "actor_id" not in hidden.proof["attribution"]
    assert hidden.proof["receipt"] == {
        "tag": "playbill-procedure-run-receipt-withheld-v1",
        "withheld": "names_the_arming_credential",
    }
    assert hidden.proof["receipt_digest"] is not None

    shown = _get(
        instance,
        f"ProcedureRun:{run_id}",
        OperationalViewer(credential_id="cred-arm", admin=False),
        detail="proof",
        at=at,
    )
    assert shown.proof is not None
    # A credential arm's run acts as the credential's principal, not its label.
    assert shown.proof["attribution"]["actor_id"] == "owner"
    assert shown.proof["receipt"]["attribution"]["actor_id"] == "owner"


def test_the_resolver_names_a_revoked_credentials_principal_on_this_instance_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from cruxible_core.runtime import playbill_api
    from cruxible_core.server import credentials
    from tests.test_procedures.test_line_arming import _credential

    records = {
        "cred-arm": _credential(instance_id="inst", revoked_at="2026-09-02T00:00:00Z"),
        "cred-elsewhere": _credential(credential_id="cred-elsewhere", instance_id="other"),
    }
    monkeypatch.setattr(
        credentials,
        "get_runtime_credential_store",
        lambda: SimpleNamespace(get=records.get),
    )
    resolve = playbill_api._credential_principal_resolver("inst")

    assert resolve("cred-arm") == "owner"
    assert resolve("cred-elsewhere") is None
    assert resolve("cred-missing") is None


_STATUS_VIEWERS = [
    (None, False),
    (OperationalViewer(credential_id="cred-arm", admin=False), True),
    (OperationalViewer(credential_id=None, admin=True), True),
    (
        OperationalViewer(
            credential_id="cred-rotated",
            admin=False,
            principal_id="owner",
            credential_principal=_resolver,
        ),
        True,
    ),
    (
        OperationalViewer(
            credential_id="cred-reviewer",
            admin=False,
            principal_id="reviewer",
            credential_principal=_resolver,
        ),
        False,
    ),
    (
        OperationalViewer(
            credential_id="cred-unbound", admin=False, credential_principal=_resolver
        ),
        False,
    ),
]


@pytest.mark.parametrize(
    ("viewer", "visible"),
    _STATUS_VIEWERS,
    ids=["anonymous", "arming", "admin", "same_principal", "other_principal", "unbound"],
)
def test_run_status_withholds_the_run_actor_exactly_as_the_run_card_does(
    credential_world,  # type: ignore[no-untyped-def]
    viewer: OperationalViewer | None,
    visible: bool,
) -> None:
    """`procedure_run_status` answered every caller the arming principal and its receipt.

    It now applies the cards' rule: the attribution's actor and the receipt
    (which carries it) are withheld from a non-admin caller unless its
    credential is the arming one or is bound to the same principal.
    """

    from cruxible_client.contracts.procedures.results import (
        ProcedureRunAttributionV1,
        ProcedureRunAttributionWithheldV1,
        ProcedureRunReceiptWithheldV1,
    )
    from cruxible_core.service.discovery.runs import procedure_run_status

    instance, _line, run_id, _when = credential_world
    state = procedure_run_status(instance, run_id, viewer=viewer)
    card = _get(instance, f"ProcedureRun:{run_id}", viewer).card
    assert isinstance(card, PlaybillGetProcedureRunCardV1)

    assert state.receipt_digest is not None
    if visible:
        assert isinstance(state.attribution, ProcedureRunAttributionV1)
        assert state.attribution.actor_id == card.actor == "owner"
        assert not isinstance(state.receipt, ProcedureRunReceiptWithheldV1)
    else:
        assert isinstance(state.attribution, ProcedureRunAttributionWithheldV1)
        assert isinstance(state.receipt, ProcedureRunReceiptWithheldV1)
        assert card.actor is None
        dumped = str(state.model_dump(mode="json"))
        assert "owner" not in dumped and "cred-arm" not in dumped
        assert "line-operator" not in dumped


def test_the_runtime_run_status_passes_the_authenticated_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import PermissionMode
    from cruxible_core.server.auth import ResolvedAuthContext

    seen: dict[str, Any] = {}

    def status(instance: Any, run_id: str, **values: Any) -> Any:
        seen.update(values, run_id=run_id)
        raise RuntimeError("stop")

    monkeypatch.setattr("cruxible_core.service.discovery.runs.procedure_run_status", status)
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_a, **_k: None)
    monkeypatch.setattr(
        playbill_api,
        "get_playbill_manager",
        lambda: type("M", (), {"get": lambda self, _i: None})(),
    )
    monkeypatch.setattr(
        playbill_api,
        "get_current_auth_context",
        lambda: ResolvedAuthContext(
            credential_id="cred-reader",
            credential_label="reader",
            principal_id="reader",
            credential_type="runtime_credential",
            instance_scope="inst",
            role=None,
            effective_permission_mode=PermissionMode.READ_ONLY,
        ),
    )
    monkeypatch.setattr(playbill_api, "get_current_mode", lambda: PermissionMode.READ_ONLY)

    with pytest.raises(RuntimeError):
        playbill_api.playbill_procedure_run_status("inst", "RUN-x")

    assert seen["run_id"] == "RUN-x"
    viewer = seen["viewer"]
    assert viewer == OperationalViewer(
        credential_id="cred-reader", admin=False, principal_id="reader"
    )
    assert viewer.credential_principal is not None
