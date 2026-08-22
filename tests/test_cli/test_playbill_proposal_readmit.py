"""Readmit client and CLI preserve the typed operation result."""

from __future__ import annotations

import httpx
from click.testing import CliRunner

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.main import cli
from tests.test_cli.test_playbill_search import COORDINATE

SOURCE_ID = "sha256:" + "11" * 32
NEW_ID = "sha256:" + "22" * 32
OPERATION = "sha256:" + "33" * 32


def _result() -> contracts.PlaybillProposalReadmitResult:
    return contracts.PlaybillProposalReadmitResult(
        tag="playbill-proposal-readmit-result-v1",
        source_proposal_id=SOURCE_ID,
        operation_digest=OPERATION,
        proposal=contracts.PlaybillProposalInspection(
            proposal={
                "admission": {"proposal_id": NEW_ID},
                "evaluation": {"verdict": "candidate"},
                "candidate": {},
            },
            accepted_coordinate=COORDINATE,
        ),
    )


def test_client_sends_the_frozen_tag_only_request() -> None:
    calls: list[tuple[str, object]] = []

    class StubTransport:
        def post(self, path, *, json):
            calls.append((path, json))
            return httpx.Response(200, json=_result().model_dump(mode="json"))

    client = CruxibleClient(base_url="https://playbill.invalid")
    client._client = StubTransport()  # type: ignore[assignment]

    assert client.readmit_playbill_proposal("inst_test", SOURCE_ID) == _result()
    assert calls == [
        (
            f"/api/v1/inst_test/playbill/proposals/{SOURCE_ID}/readmit",
            {"tag": "playbill-proposal-readmit-request-v1"},
        )
    ]


def test_cli_reports_the_new_proposal_without_hiding_its_source(monkeypatch) -> None:
    class StubClient:
        def readmit_playbill_proposal(self, instance_id, proposal_id):
            assert (instance_id, proposal_id) == ("inst_test", SOURCE_ID)
            return _result()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_test",
            "playbill",
            "proposal",
            "readmit",
            SOURCE_ID,
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "target: inst_test @ https://playbill.invalid (explicit)",
        f"candidate  {NEW_ID}  from {SOURCE_ID}",
    ]
