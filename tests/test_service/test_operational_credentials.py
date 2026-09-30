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
            principal_id="cred-reader",
            principal_label="reader",
            credential_type="runtime_credential",
            instance_scope="inst",
            role=None,
            effective_permission_mode=PermissionMode.READ_ONLY,
        ),
    )
    monkeypatch.setattr(playbill_api, "get_current_mode", lambda: PermissionMode.READ_ONLY)

    with pytest.raises(RuntimeError):
        playbill_api.playbill_get("inst", request=PlaybillGetRequestV1(ref="Line:x"))

    assert seen["viewer"] == OperationalViewer(credential_id="cred-reader", admin=False)
