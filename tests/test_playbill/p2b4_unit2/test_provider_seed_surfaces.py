from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.fastmcp import FastMCP
from tests.support import provider_seed as provider_seed_support

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.main import cli
from cruxible_core.mcp.tools import register_tools

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

COORDINATE = {
    "tag": "playbill-accepted-coordinate-v1",
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}
RESULT = {
    "tag": "playbill-provider-seed-result-v1",
    "provider_id": "cruxible-provider-workspace",
    "materialization_source": "local",
    "status": "already_current",
    "changed_paths": [],
    "approval_required": False,
    "accepted_coordinate": COORDINATE,
}


def test_sdk_posts_the_empty_seed_request_and_parses_result() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=RESULT)

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible",
        transport=httpx.MockTransport(handler),
    )

    result = client.seed_playbill_provider("inst_seed")

    assert result.status == "already_current"
    assert captured[0].url.path == "/api/v1/inst_seed/playbill/providers/seed"
    assert json.loads(captured[0].content) == {}


def test_cli_seed_names_its_write_target_and_uses_sdk(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class StubClient:
        def seed_playbill_provider(
            self, instance_id: str
        ) -> contracts.PlaybillProviderSeedResultV1:
            calls.append(instance_id)
            return contracts.PlaybillProviderSeedResultV1.model_validate(RESULT)

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://seed.invalid",
            "--instance-id",
            "inst_seed",
            "playbill",
            "provider",
            "seed",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["inst_seed"]
    assert "target: inst_seed @ https://seed.invalid (explicit)" in result.stderr
    assert '"status": "already_current"' in result.stdout


def test_mcp_init_carries_the_seed_decision_and_reports_the_unseeded_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP-first clients must be able to init a daemon with no seed materializations."""

    from cruxible_core.mcp import handlers

    seeds: list[bool] = []

    class StubClient:
        def init_playbill(
            self,
            instance_id: str,
            *,
            principals: list[dict[str, object]],
            operating_profile: str,
            require_independent_approval: bool,
            seed: bool = True,
        ) -> contracts.PlaybillInitResult:
            seeds.append(seed)
            return contracts.PlaybillInitResult.model_validate(UNSEEDED_INIT)

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    unseeded = handlers.handle_playbill_init("inst_no_seed", [], "local", False, seed=False)
    default = handlers.handle_playbill_init("inst_no_seed", [], "local", False)

    assert seeds == [False, True]
    assert unseeded.provider_seed is not None
    assert unseeded.provider_seed.status == "unseeded"
    assert unseeded.provider_seed.repair == (
        "configure_seed_materializations_then_playbill_provider_seed"
    )
    assert default.provider_seed is not None


def test_provider_seed_mcp_parity_gap_is_explicit() -> None:
    """Follow-on card: Provider write family has no MCP parity (declare before exposing)."""

    registered = set(register_tools(FastMCP("provider-seed-gap")))

    assert {name for name in registered if "provider" in name and "seed" in name} == set()
    changelog = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "no MCP parity" in changelog
    reference = (REPOSITORY_ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert "the Provider write family does not yet have MCP parity" in reference


UNSEEDED_INIT = {
    "tag": "playbill-init-v1",
    "instance_id": "inst_no_seed",
    "coordinate": COORDINATE,
    "trust_root": {},
    "recovery_posture": "normal",
    "approval_policy_mode": "self_approval_allowed",
    "workspace_advertisement": {"status": "not_attached", "workspace_path": None},
    "provider_seed": {
        "tag": "playbill-provider-seed-result-v1",
        "provider_id": "cruxible-provider-workspace",
        "materialization_source": "local",
        "status": "unseeded",
        "changed_paths": [],
        "approval_required": False,
        "repair": "configure_seed_materializations_then_playbill_provider_seed",
        "accepted_coordinate": COORDINATE,
    },
}


def test_sdk_init_carries_the_seed_decision_on_every_request() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=UNSEEDED_INIT)

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible",
        transport=httpx.MockTransport(handler),
    )

    seeded = client.init_playbill("inst_no_seed", principals=[])
    assert json.loads(captured[0].content)["seed"] is True

    unseeded = client.init_playbill("inst_no_seed", principals=[], seed=False)
    assert json.loads(captured[1].content)["seed"] is False
    assert unseeded.provider_seed is not None
    assert unseeded.provider_seed.status == "unseeded"
    assert seeded.provider_seed is not None


def test_cli_no_seed_is_an_explicit_opt_out_that_reports_the_repair(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    seeds: list[bool] = []

    class StubClient:
        def init_playbill(
            self,
            instance_id: str,
            *,
            principals: list[dict[str, object]],
            operating_profile: str,
            require_independent_approval: bool,
            seed: bool = True,
        ) -> contracts.PlaybillInitResult:
            seeds.append(seed)
            return contracts.PlaybillInitResult.model_validate(UNSEEDED_INIT)

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://seed.invalid",
            "--instance-id",
            "inst_no_seed",
            "playbill",
            "init",
            "--key-dir",
            str(tmp_path / "custody"),
            "--no-seed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seeds == [False]
    assert "Provider seed: unseeded" in result.stdout
    assert "seed_materializations" in result.stdout
    assert "cruxible playbill provider seed" in result.stdout


def test_absent_provider_checkout_skips_seeding_tests_naming_the_follow_on_card(
    monkeypatch, tmp_path
) -> None:
    """The locator's absent case must skip, never silently pass or hard-fail."""

    monkeypatch.setattr(provider_seed_support, "find_workspace_provider_checkout", lambda: None)
    with pytest.raises(pytest.skip.Exception) as absent:
        provider_seed_support.workspace_provider_checkout()

    reason = str(absent.value)
    assert reason == provider_seed_support.MISSING_CHECKOUT_SKIP_REASON
    assert "CI job with a deploy-key checkout of cruxible-providers" in reason
    with pytest.raises(pytest.skip.Exception):
        provider_seed_support.workspace_seed_materialization()
    with pytest.raises(pytest.skip.Exception):
        provider_seed_support.write_workspace_seed_config(tmp_path)
