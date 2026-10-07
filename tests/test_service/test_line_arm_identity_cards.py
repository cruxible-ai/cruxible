"""Line and run cards render identity-era arms: a claimed principal is shown to every reader.

Regression for the identity x operational-reads merge, which added the
``principal_claim`` arm kind.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
from cruxible_client.contracts.operational_reads import GetLineCard
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.operational import OperationalViewer
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_line_arming import _armed_world

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=False)


def _card(instance: Any, line: Any, when: Any, viewer: OperationalViewer | None = None) -> Any:
    card = service_playbill_get(
        instance,
        request=GetRequest(ref=line.identity.qualified, evaluation_time=when),
        access=_ACCESS,
        viewer=viewer,
    ).card
    assert isinstance(card, GetLineCard)
    (arm,) = card.enablements
    return arm


def test_a_claimed_principal_arm_shows_its_principal_to_every_reader(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim = LineEnablementPrincipal(kind="principal_claim", label="owner")
    instance, line, _procedure, start = _armed_world(tmp_path, principal=claim)

    for viewer in (None, OperationalViewer(credential_id="cred-x", admin=False)):
        arm = _card(instance, line, start + timedelta(seconds=1), viewer)
        assert arm.principal_kind == "principal_claim"
        assert arm.enabled_by == "owner" and arm.credential is None
        assert not arm.enabled_by_withheld
