"""`playbill settle PREDICTION_ID --observation CLM-...` builds a Claim-ID settle request."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.predictions import PlaybillSettleRequestV2
from cruxible_core.cli.main import cli

OBSERVATION = "CLM-" + "a" * 32


def _settle(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[Any, list[Any]]:
    sent: list[Any] = []

    class StubClient:
        def settle_playbill_prediction(self, instance_id, prediction_id, *, request):  # type: ignore[no-untyped-def]
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
            "playbill",
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
    assert request == PlaybillSettleRequestV2(observation=OBSERVATION)
    assert request.contract is None and request.evidence is None


@pytest.mark.parametrize("args", [(), ("--example",)])
def test_settle_without_an_observation_names_the_repair(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    result, sent = _settle(monkeypatch, "status-test", *args)

    assert result.exit_code == 2 and not sent
    if not args:
        assert "--observation CLAIM_ID" in result.output
