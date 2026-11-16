"""S7: a provider build the consumer retired stays retired on kit add; a dependent refuses."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.kits import KitProviderStep
from cruxible_core.errors import RequestRefusedError
from cruxible_core.service import kits

BUILD = "acme-call/b0123456789a"


def _provider(build: str = BUILD) -> Any:
    return SimpleNamespace(
        provider_id="acme-call", package="acme-call", version="1.0.0", build=build
    )


def test_a_retired_build_is_kept_retired_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kits, "_provider_here", lambda *a, **k: ("retired", "sha256:x", None))
    monkeypatch.setattr(kits, "_default_implementers", lambda *a, **k: ())
    steps, missing = kits._provider_steps(
        None, {}, SimpleNamespace(providers=(_provider(),)), contents={}
    )

    assert [(step.action, step.detail) for step in steps] == [("kept_retired", "kept retired")]
    assert missing == []


def test_a_definition_pinning_a_kept_retired_build_refuses_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = ArtifactPin.model_construct(
        target=ArtifactIdentity(kind="Provider", name=BUILD), artifact_digest="sha256:" + "a" * 64
    )
    state = SimpleNamespace(
        identity=ArtifactIdentity(kind="Procedure", name="acme.enrich"), pins=(pin,)
    )
    monkeypatch.setattr(kits, "_artifact_state", lambda path, content: state)
    diff = kits._Diff()
    diff.writes["procedures/acme.enrich.json"] = b"{}"
    step = KitProviderStep(
        provider_id="acme-call",
        package="acme-call",
        version="1.0.0",
        build=BUILD,
        action="kept_retired",
    )

    with pytest.raises(RequestRefusedError) as refused:
        kits._refuse_retired_provider_dependents(
            {"procedures/acme.enrich.json": b"{}"}, diff, (step,)
        )

    assert refused.value.error_code == "cruxible.kit.retired_dependency"
    assert "Procedure:acme.enrich" in str(refused.value) and f"Provider:{BUILD}" in str(
        refused.value
    )


def _state(kind: str, name: str, pins: tuple[Any, ...] = ()) -> Any:
    return SimpleNamespace(identity=ArtifactIdentity(kind=kind, name=name), pins=pins)


@pytest.mark.parametrize("edited_away", [False, True])
def test_a_live_definition_retained_without_a_write_is_checked_against_retirements(
    monkeypatch: pytest.MonkeyPatch, edited_away: bool
) -> None:
    """Review r2: an unchanged or kept dependent of a consumer-retired definition refuses,
    read from the content held here, so a local edit that dropped the need does not."""

    query, plan = "query-definitions/acme.plans.json", "claim-types/acme.account/plan.json"
    states = {
        query: _state("QueryDefinition", "acme.plans"),
        plan: _state("ClaimType", "acme.account.plan"),
    }
    diff = kits._Diff()
    diff.kept_retired[plan] = "ClaimType:acme.account.plan"
    diff.retained_live[query] = (states[query], {"held": True})
    seen: list[dict[str, Any]] = []

    def dependency_order(states_: Any, payloads: Any, *, within: Any, outside: Any) -> Any:
        seen.append(payloads[query])
        yield query, (() if edited_away else (plan,))

    monkeypatch.setattr(kits, "_dependency_order", dependency_order)

    if edited_away:
        kits._refuse_retired_dependencies(diff, {}, states, {query: {"release": True}}, None)
    else:
        with pytest.raises(RequestRefusedError) as refused:
            kits._refuse_retired_dependencies(diff, {}, states, {query: {"release": True}}, None)
        assert "QueryDefinition:acme.plans" in str(refused.value)
        assert "ClaimType:acme.account.plan" in str(refused.value)
    assert seen == [{"held": True}]  # the held content, not the release's


def test_a_retained_definition_pinning_a_kept_retired_build_refuses() -> None:
    pin = ArtifactPin.model_construct(
        target=ArtifactIdentity(kind="Provider", name=BUILD), artifact_digest="sha256:" + "b" * 64
    )
    diff = kits._Diff()
    diff.retained_live["lines/acme.enrich.json"] = (_state("Line", "acme.enrich", (pin,)), {})
    step = KitProviderStep(
        provider_id="acme-call",
        package="acme-call",
        version="1.0.0",
        build=BUILD,
        action="kept_retired",
    )

    with pytest.raises(RequestRefusedError) as refused:
        kits._refuse_retired_provider_dependents({}, diff, (step,))
    assert "Line:acme.enrich" in str(refused.value)
