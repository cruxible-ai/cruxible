"""The write verbs pass the same authoring gate as authoring create, dry run included.

Regression for the identity x write-ext merge: ``set``/``add``/``retire``/``write``
lower onto the authoring coordinator, but planning, the already-live shortcut and
the dry-run preview all ran before (or without) ``coordinator.create``, so an
actor that can never author got a plan, a ``would_accept`` or an ``accepted``
answer instead of the identity refusal and its repair.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import (
    RetireRequest,
    SetRequest,
    WriteRequest,
)
from cruxible_core.errors import PrincipalRefusedError
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from tests.core_support._write_support import KIND, caller, seed_write_surface

WI1 = f"{KIND}/wi-1"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_write_surface(tmp_path)[0]


def _request(*changes: dict[str, Any], **options: Any) -> WriteRequest:
    return WriteRequest.model_validate(
        {"because": "The writer checked it.", "changes": list(changes), **options}
    )


def _as(actor_id: str) -> WriteCaller:
    return WriteCaller(actor=AuthenticatedActor(actor_id=actor_id), may_activate=True)


def _proposal_count(instance: PlaybillInstance) -> int:
    return len(service_list_playbill_proposals(instance).entries)


_SET = {"op": "set", "subject": WI1, "field": "status", "value": "ready"}
_ADD = {"op": "add", "subject": WI1, "field": "governs", "value": f"{KIND}/wi-2"}


@pytest.mark.parametrize(
    ("actor_id", "code", "operation"),
    [
        # The implicit local operator names no principal: configure one.
        ("operator", "playbill.identity.principal_unconfigured", "playbill.orient"),
        # A configured principal ID nobody registered: an owner adds it.
        ("ghost", "playbill.identity.principal_absent", "playbill.principal.add"),
    ],
)
@pytest.mark.parametrize(
    "options",
    [
        {"dry_run": True},
        {},
        {"accept": "if_allowed"},
        {"accept": "if_allowed", "dry_run": True},
    ],
    ids=["dry_run", "write", "if_allowed", "if_allowed_dry_run"],
)
def test_a_caller_that_cannot_author_is_refused_before_any_work(
    instance: PlaybillInstance,
    actor_id: str,
    code: str,
    operation: str,
    options: dict[str, Any],
) -> None:
    head = instance.accepted_coordinate()
    proposals = _proposal_count(instance)

    for changes in ((_SET,), (_SET, _ADD)):
        with pytest.raises(PrincipalRefusedError) as refused:
            service_playbill_write(
                instance, request=_request(*changes, **options), caller=_as(actor_id)
            )
        assert refused.value.error_code == code
        assert refused.value.repair is not None
        assert refused.value.repair.operation == operation  # type: ignore[union-attr]

    assert instance.accepted_coordinate() == head
    assert _proposal_count(instance) == proposals


def test_an_already_live_value_is_refused_not_answered_accepted(
    instance: PlaybillInstance,
) -> None:
    """The already-done shortcut never reaches the coordinator; the gate precedes it."""

    assert service_playbill_write(instance, request=_request(_SET), caller=caller()).status == (
        "accepted"
    )
    for options in ({}, {"dry_run": True}):
        with pytest.raises(PrincipalRefusedError) as refused:
            service_playbill_write(instance, request=_request(_SET, **options), caller=_as("ghost"))
        assert refused.value.error_code == "playbill.identity.principal_absent"


def test_a_retire_is_gated_like_a_set(instance: PlaybillInstance) -> None:
    accepted = service_playbill_write(instance, request=_request(_SET), caller=caller())
    (change,) = accepted.changes
    assert change.claim is not None
    for dry_run in (True, False):
        with pytest.raises(PrincipalRefusedError) as refused:
            service_playbill_write(
                instance,
                request=_request({"op": "retire", "target": change.claim}, dry_run=dry_run),
                caller=_as("operator"),
            )
        assert refused.value.error_code == "playbill.identity.principal_unconfigured"


def test_an_active_principal_still_writes_and_previews(instance: PlaybillInstance) -> None:
    preview = service_playbill_write(
        instance, request=_request(_SET, dry_run=True), caller=caller()
    )
    assert preview.status == "would_accept", preview
    assert service_playbill_write(instance, request=_request(_SET), caller=caller()).status == (
        "accepted"
    )


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("verb", ["set", "retire", "write"])
def test_the_runtime_refuses_an_unbound_credential_with_the_mint_repair(
    instance: PlaybillInstance,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    dry_run: bool,
) -> None:
    """Through the runtime facade: an unbound credential is refused, dry run or not."""

    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import PermissionMode
    from cruxible_core.server.auth import ResolvedAuthContext

    reached: list[str] = []
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_a, **_k: None)
    # Unbound bearer credentials exist only on a daemon that requires auth.
    monkeypatch.setattr(playbill_api, "is_server_auth_enabled", lambda: True)
    monkeypatch.setattr(
        playbill_api,
        "get_playbill_manager",
        lambda: type("M", (), {"get": lambda self, _i: instance})(),
    )
    monkeypatch.setattr(
        playbill_api,
        "service_playbill_write",
        lambda *_a, **_k: reached.append("service"),
    )
    monkeypatch.setattr(
        playbill_api,
        "get_current_auth_context",
        lambda: ResolvedAuthContext(
            credential_id="cred-unbound",
            credential_label="manager",
            credential_type="runtime_credential",
            instance_scope=instance.descriptor.instance_id,
            role=None,
            effective_permission_mode=PermissionMode.ADMIN,
            principal_id=None,
        ),
    )
    instance_id = instance.descriptor.instance_id
    with pytest.raises(PrincipalRefusedError) as refused:
        if verb == "set":
            playbill_api.playbill_set(
                instance_id,
                request=SetRequest.model_validate(
                    {
                        "because": "b",
                        **{k: v for k, v in _SET.items() if k != "op"},
                        "dry_run": dry_run,
                    }
                ),
            )
        elif verb == "retire":
            playbill_api.playbill_retire(
                instance_id,
                request=RetireRequest.model_validate(
                    {
                        "because": "b",
                        "target": {"subject": WI1, "field": "status"},
                        "dry_run": dry_run,
                    }
                ),
            )
        else:
            playbill_api.playbill_write(instance_id, request=_request(_SET, dry_run=dry_run))
    assert refused.value.error_code == "playbill.identity.credential_unbound"
    assert refused.value.repair is not None
    assert refused.value.repair.operation == "credential.mint"  # type: ignore[union-attr]
    assert refused.value.repair.arguments == {"unbound_credential_id": "cred-unbound"}  # type: ignore[union-attr]
    assert reached == []
