"""The empty interface inventory is a successful read across client and CLI."""

from __future__ import annotations

import httpx
from click.testing import CliRunner

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.main import cli
from tests.test_cli.test_playbill_search import COORDINATE


def _inventory() -> contracts.PlaybillInterfaceInventory:
    return contracts.PlaybillInterfaceInventory(
        tag="playbill-interface-inventory-v1",
        coordinate=COORDINATE,
        provider_status="not_installed",
        interfaces=[],
    )


def test_client_parses_the_inventory_success_variant() -> None:
    class StubTransport:
        def post(self, *_args, **_kwargs):
            return httpx.Response(200, json=_inventory().model_dump(mode="json"))

    client = CruxibleClient(base_url="https://playbill.invalid")
    client._client = StubTransport()  # type: ignore[assignment]

    result = client.discover_playbill("inst_test")

    assert isinstance(result, contracts.PlaybillInterfaceInventory)
    assert result.provider_status == "not_installed"


def test_cli_renders_not_installed_without_treating_it_as_an_error(monkeypatch) -> None:
    class StubClient:
        def discover_playbill(self, instance_id, **_kwargs):
            assert instance_id == "inst_test"
            return _inventory()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_test",
            "playbill",
            "discover",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "No provider interfaces installed."
