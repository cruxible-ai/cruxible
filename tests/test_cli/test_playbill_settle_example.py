"""`playbill settle --example` prints a settlement request with only its evidence left open."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.predictions import PlaybillSettleRequestV2
from cruxible_core.cli.main import cli

CONTRACT = {
    "identity": {"kind": "ResolutionContract", "name": "status-test"},
    "artifact_digest": "sha256:" + "1" * 64,
    "coordinate": {
        "git_oid": "2" * 40,
        "semantic_root": "sha256:" + "3" * 64,
        "generation_root": "sha256:" + "4" * 64,
        "compiler_digest": "sha256:" + "5" * 64,
    },
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("an example must not call the daemon")),
    )


def _settle(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(cli, ["playbill", "settle", *args])


def test_a_bound_window_id_asks_the_daemon_for_its_exact_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_core.cli.commands.playbill import _settle_example

    # What the daemon would serve: any exact request; the CLI only relays it.
    template = _settle_example("status-test")
    asked: list[tuple[str, str]] = []

    class StubClient:
        def example_playbill_settlement(self, instance_id: str, bound: str):  # type: ignore[no-untyped-def]
            asked.append((instance_id, bound))
            return template

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://settle.example.test",
            "--instance-id",
            "inst_settle",
            "playbill",
            "settle",
            "--example",
            "RSC-" + "a" * 32,
        ],
    )

    assert result.exit_code == 0, result.output
    assert asked == [("inst_settle", "RSC-" + "a" * 32)]
    assert PlaybillSettleRequestV2.model_validate(json.loads(result.stdout)) == template


def test_a_bare_example_names_the_prediction_and_placeholders_the_rest() -> None:
    result = _settle("--example", "status-test")

    assert result.exit_code == 0, result.output
    request = PlaybillSettleRequestV2.model_validate(json.loads(result.output))
    assert request.contract.identity.qualified == "ResolutionContract:status-test"
    assert request.trigger_event is None


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("status-test",), "REQUEST_FILE or --example"),
    ],
)
def test_a_misused_example_is_a_usage_error(args: tuple[str, ...], message: str) -> None:
    result = _settle(*args)

    assert result.exit_code == 2 and message in result.output
