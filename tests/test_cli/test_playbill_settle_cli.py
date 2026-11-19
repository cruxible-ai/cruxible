"""`cruxible prediction settle PREDICTION_ID --observation CLM-...` and `prediction list CLAIM`."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.predictions import SettleRequest
from cruxible_core.cli.main import cli

OBSERVATION = "CLM-" + "a" * 32


def _settle(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[Any, list[Any]]:
    sent: list[Any] = []

    class StubClient:
        def settle_prediction(self, instance_id, prediction_id, *, request):  # type: ignore[no-untyped-def]
            sent.append((instance_id, prediction_id, request))
            raise SystemExit(0)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://settle.example.test",
            "--instance-id",
            "inst_settle",
            "prediction",
            "settle",
            *args,
        ],
    )
    return result, sent


def test_settle_sends_only_the_observation_claim_id(monkeypatch: pytest.MonkeyPatch) -> None:
    window = "RSC-" + "b" * 32
    result, sent = _settle(monkeypatch, window, "--observation", f"Claim:{OBSERVATION}")

    assert result.exit_code == 0, result.output
    ((instance_id, prediction_id, request),) = sent
    assert (instance_id, prediction_id) == ("inst_settle", window)
    assert request == SettleRequest(observation=OBSERVATION)
    assert request.contract is None and request.evidence is None


@pytest.mark.parametrize("args", [(), ("--example",)])
def test_settle_without_an_observation_names_the_repair(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    result, sent = _settle(monkeypatch, "status-test", *args)

    assert result.exit_code == 2 and not sent
    if not args:
        assert "--observation CLAIM_ID" in result.output


def test_prediction_list_requires_a_claim_and_queries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_client import contracts

    sent: list[object] = []

    class StubClient:
        def list_predictions(self, instance_id, *, request):  # type: ignore[no-untyped-def]
            sent.append((instance_id, request))
            return contracts.ResolutionContractsResult.model_validate(
                {
                    "coordinate": {
                        "git_oid": "1" * 40,
                        "semantic_root": "sha256:" + "2" * 64,
                        "generation_root": "sha256:" + "3" * 64,
                        "compiler_digest": "sha256:" + "4" * 64,
                    },
                    "contracts": [],
                }
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    common = ["--server-url", "https://settle.example.test", "--instance-id", "inst_settle"]

    missing = CliRunner().invoke(cli, [*common, "prediction", "list"])
    listed = CliRunner().invoke(cli, [*common, "prediction", "list", OBSERVATION])
    gone = CliRunner().invoke(cli, [*common, "resolution-contracts", OBSERVATION])

    assert missing.exit_code == 2 and "Missing argument 'CLAIM'" in missing.output
    assert listed.exit_code == 0, listed.output
    assert "No accepted prediction tests this Claim version." in listed.output
    ((instance_id, request),) = sent
    assert instance_id == "inst_settle" and request.hypothesis == OBSERVATION
    assert gone.exit_code == 2
