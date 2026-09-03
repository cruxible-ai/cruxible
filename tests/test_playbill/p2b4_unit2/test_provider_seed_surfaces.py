from __future__ import annotations

import json

import httpx
from click.testing import CliRunner
from mcp.server.fastmcp import FastMCP

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.main import cli
from cruxible_core.mcp.tools import register_tools
from cruxible_core.runtime.permissions import RUNTIME_OPERATION_PERMISSIONS

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


def test_provider_seed_mcp_parity_gap_is_explicit() -> None:
    registered = set(register_tools(FastMCP("provider-seed-gap")))

    assert "cruxible_playbill_provider_seed" in RUNTIME_OPERATION_PERMISSIONS
    assert "cruxible_playbill_provider_seed" not in registered
