"""Line and run cards render identity-era arms: claimed principals and old-format arms.

Regression for the identity x operational-reads merge. Identity added the
``principal_claim`` arm kind and stops arms persisted without provenance with
``arm_requires_rearm``. An old-format record parses under the current model with
a defaulted tag, so the cards read it as the implicit local operator; they must
show it as ``unverified`` instead, and show the stop through the Line card and
orient's arm attention.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
from cruxible_client.contracts.line_dispatch import LineArmPrincipalV1
from cruxible_client.contracts.operational_reads import PlaybillGetLineCardV1
from cruxible_core.runtime import line_arms
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.operational import OperationalViewer
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_line_arming import _armed_world, _match

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=False)


class _LegacyArmPrincipal:
    """An arm principal exactly as code before arm-record provenance persisted it."""

    def __init__(self, record: dict[str, object]) -> None:
        self._record = record

    def model_dump(self, mode: str = "python") -> dict[str, object]:
        return dict(self._record)


def _card(instance: Any, line: Any, when: Any, viewer: OperationalViewer | None = None) -> Any:
    card = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=line.identity.qualified, evaluation_time=when),
        access=_ACCESS,
        viewer=viewer,
    ).card
    assert isinstance(card, PlaybillGetLineCardV1)
    (arm,) = card.arms
    return arm


def test_an_old_format_operator_arm_reads_unverified_then_stopped_for_rearm(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(line_arms, "is_server_auth_enabled", lambda: False)
    legacy = {"kind": "local_operator", "credential_id": None, "label": "line-operator"}
    instance, line, _procedure, start = _armed_world(
        tmp_path,
        principal=_LegacyArmPrincipal(legacy),  # type: ignore[arg-type]
    )

    # Before any recovery pass, the card never calls it the implicit operator.
    before = _card(instance, line, start + timedelta(seconds=1))
    assert before.principal_kind == "unverified"
    assert before.state == "running"

    # A restart's matching pass stops it; the card and orient say why.
    _match(instance, start + timedelta(seconds=2), daemon_id="restarted-daemon")
    when = start + timedelta(seconds=3)
    after = _card(instance, line, when)
    assert after.principal_kind == "unverified"
    assert (after.state, after.stop_reason) == ("stopped", "arm_requires_rearm")
    assert after.detail is not None and "rearm" in after.detail

    answer = service_playbill_orient(instance, evaluation_time=when, surface="cli")
    arms = answer.attention.arms
    assert arms is not None and (arms.running, arms.stopped) == (0, 1)
    assert any("(arm_requires_rearm)" in item for item in arms.needs_attention)


def test_an_old_format_credential_arm_stays_withheld_from_other_readers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = {"kind": "runtime_credential", "credential_id": "cred-old", "label": "manager"}
    instance, line, _procedure, start = _armed_world(
        tmp_path,
        principal=_LegacyArmPrincipal(legacy),  # type: ignore[arg-type]
    )
    _match(instance, start + timedelta(seconds=2), daemon_id="restarted-daemon")
    when = start + timedelta(seconds=3)

    hidden = _card(instance, line, when, OperationalViewer(credential_id="cred-x", admin=False))
    assert hidden.principal_kind == "unverified"
    assert hidden.stop_reason == "arm_requires_rearm"
    assert hidden.armed_by is None and hidden.credential is None and hidden.armed_by_withheld

    shown = _card(instance, line, when, OperationalViewer(credential_id=None, admin=True))
    assert (shown.credential, shown.armed_by) == ("cred-old", "manager")


def test_a_claimed_principal_arm_shows_its_principal_to_every_reader(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim = LineArmPrincipalV1(kind="principal_claim", label="owner")
    instance, line, _procedure, start = _armed_world(tmp_path, principal=claim)

    for viewer in (None, OperationalViewer(credential_id="cred-x", admin=False)):
        arm = _card(instance, line, start + timedelta(seconds=1), viewer)
        assert arm.principal_kind == "principal_claim"
        assert arm.armed_by == "owner" and arm.credential is None
        assert not arm.armed_by_withheld
