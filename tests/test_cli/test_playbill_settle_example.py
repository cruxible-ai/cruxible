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
EVENT = {
    "run_id": "RUN-anchor",
    "partition_id": "run:anchor",
    "sequence": 3,
    "record_digest": "sha256:" + "6" * 64,
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("an example must not call the daemon")),
    )


def _settle(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(cli, ["playbill", "settle", *args])


def test_the_binding_a_next_row_carries_fills_the_request_but_its_evidence() -> None:
    result = _settle(
        "--example",
        "status-test",
        "--contract",
        json.dumps(CONTRACT),
        "--trigger-event",
        json.dumps(EVENT),
    )

    assert result.exit_code == 0, result.output
    request = PlaybillSettleRequestV2.model_validate(json.loads(result.output))
    assert request.contract.model_dump(mode="json", exclude={"coordinate": {"tag"}}) == CONTRACT
    assert request.trigger_event is not None
    assert request.trigger_event.model_dump(mode="json") == EVENT
    assert request.evidence.claim.identity.name == "CLM-" + "0" * 32


def test_a_bare_example_names_the_prediction_and_placeholders_the_rest() -> None:
    result = _settle("--example", "status-test")

    assert result.exit_code == 0, result.output
    request = PlaybillSettleRequestV2.model_validate(json.loads(result.output))
    assert request.contract.identity.qualified == "ResolutionContract:status-test"
    assert request.trigger_event is None


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--example", "other", "--contract", json.dumps(CONTRACT)), "different prediction"),
        (("--example", "status-test", "--trigger-event", "{}"), "Invalid settlement binding"),
        (("status-test", "--contract", json.dumps(CONTRACT)), "require --example"),
        (("status-test",), "REQUEST_FILE or --example"),
    ],
)
def test_a_misused_example_is_a_usage_error(args: tuple[str, ...], message: str) -> None:
    result = _settle(*args)

    assert result.exit_code == 2 and message in result.output
