"""next withholds every writing repair from a caller that cannot author, naming the identity repair.

Regression for the identity x operational-reads merge: next withheld a repair
only by tier or MCP profile, so an unbound credential or an unregistered
principal at a high enough tier was handed repairs that every write door
refuses. It now keeps the row and names whoami's refusal in ``repair_requires``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.principals import AuthoringRefusal
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.next import (
    PlaybillNextRepairRequirementV1,
    PlaybillNextRepairV1,
    _CallerView,
)
from tests.test_integration.test_next_caller_view import (
    _approval_row,
    _conflict_with_uncovered_member,
)

UNBOUND = AuthoringRefusal(
    code="playbill.identity.credential_unbound",
    detail="this bearer credential (manager) acts as no principal; repair: mint one",
    repair=RepairOperation(
        operation="credential.mint", arguments={"unbound_credential_id": "cred-1"}
    ),
)


def _view(
    *,
    surface: str = "cli",
    caller_rung: int | None = 3,
    refusal: AuthoringRefusal | None = UNBOUND,
    tools: tuple[str, ...] | None = None,
) -> _CallerView:
    return _CallerView(
        cast(PlaybillInstance, None),
        surface=surface,  # type: ignore[arg-type]
        tools=tools,
        caller_rung=caller_rung,
        authoring_refusal=refusal,
    )


@pytest.mark.parametrize("caller_rung", [3, None], ids=["admin", "in_process"])
def test_an_admin_that_cannot_author_keeps_the_row_with_the_identity_repair(
    caller_rung: int | None,
) -> None:
    row = _conflict_with_uncovered_member()

    (kept,) = _view(caller_rung=caller_rung).render([row])

    assert kept.reason == "claim_conflicted" and kept.repair is None
    requires = kept.repair_requires
    assert requires is not None and requires.because == ("authoring",)
    assert requires.authoring_refusal == UNBOUND
    (finding,) = kept.findings
    assert finding.repair is None and finding.repair_requires is not None
    assert finding.repair_requires.authoring_refusal == UNBOUND


def test_the_identity_gate_joins_the_tier_and_profile_gates() -> None:
    row = _approval_row()

    (kept,) = _view(surface="mcp", caller_rung=1, tools=("cruxible_playbill_next",)).render([row])

    assert kept.repair_requires is not None
    assert kept.repair_requires.because == ("tier", "profile", "authoring")
    dumped = kept.repair_requires.model_dump(mode="json")
    assert dumped["authoring_refusal"]["code"] == "playbill.identity.credential_unbound"
    # The served model reads it back.
    served = contracts.NextRepairRequirement.model_validate(dumped)
    assert served.authoring_refusal is not None
    assert served.authoring_refusal.repair == UNBOUND.repair


def test_a_caller_that_can_author_keeps_every_repair_it_has_the_tier_for() -> None:
    row = _conflict_with_uncovered_member()

    (kept,) = _view(refusal=None).render([row])

    assert kept == row
    assert kept.repair_requires is None


def test_a_repair_that_writes_nothing_is_never_withheld_for_authoring() -> None:
    floor = PlaybillNextRepairV1.model_construct(
        operation="playbill.floor.export", target="Instance:x", required_change="x", arguments={}
    )

    assert _view().requirement(floor) is None


def test_the_requirement_names_the_refusal_exactly_when_authoring_gates_it() -> None:
    base: dict[str, Any] = {
        "operation": "playbill.set",
        "tool": "cruxible_playbill_set",
        "tier": "governed_write",
    }
    with pytest.raises(ValidationError):
        PlaybillNextRepairRequirementV1(**base, because=("authoring",))
    with pytest.raises(ValidationError):
        PlaybillNextRepairRequirementV1(**base, because=("tier",), authoring_refusal=UNBOUND)
    tier_only = PlaybillNextRepairRequirementV1(**base, because=("tier",))
    assert "authoring_refusal" not in tier_only.model_dump(mode="json")


def test_the_cli_hint_leads_with_the_identity_repair() -> None:
    from cruxible_core.cli.commands.playbill import _next_requirement_hint

    requires = contracts.NextRepairRequirement.model_validate(
        PlaybillNextRepairRequirementV1(
            operation="playbill.set",
            tool="cruxible_playbill_set",
            tier="governed_write",
            because=("tier", "authoring"),
            authoring_refusal=UNBOUND,
        ).model_dump(mode="json")
    )

    hint = _next_requirement_hint(requires)

    assert hint.startswith("cruxible_playbill_set needs a caller that can author")
    assert "playbill.identity.credential_unbound" in hint and "mint one" in hint
    assert hint.endswith("and the governed_write tier")


@pytest.mark.parametrize(
    ("code", "passed"),
    [
        ("playbill.identity.credential_unbound", True),
        ("playbill.identity.principal_absent", True),
        # The tier is next's own gate; whoami's tier refusal is not repeated.
        ("playbill.identity.permission_insufficient", False),
    ],
)
def test_the_runtime_passes_whoamis_refusal_less_the_tier(
    monkeypatch: pytest.MonkeyPatch, code: str, passed: bool
) -> None:
    from types import SimpleNamespace

    from cruxible_core.runtime import playbill_api

    refusal = UNBOUND.model_copy(update={"code": code})
    monkeypatch.setattr(
        playbill_api,
        "playbill_whoami",
        lambda _instance_id: SimpleNamespace(authoring_refusal=refusal),
    )
    seen: dict[str, Any] = {}

    def service(instance: Any, **values: Any) -> Any:
        seen.update(values)
        raise RuntimeError("stop")

    monkeypatch.setattr(playbill_api, "service_playbill_next", service)
    monkeypatch.setattr(playbill_api, "validate_playbill_next_request", lambda request: request)
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_a, **_k: None)
    monkeypatch.setattr(
        playbill_api,
        "get_playbill_manager",
        lambda: SimpleNamespace(
            get=lambda _i: None,
            provider_runtime_operator=lambda: SimpleNamespace(
                lane_status=lambda: ("available", None, None)
            ),
            consumer_runner=SimpleNamespace(running=False),
        ),
    )

    with pytest.raises(RuntimeError):
        playbill_api.playbill_next("inst", request={})

    assert seen["caller_authoring_refusal"] == (refusal if passed else None)
