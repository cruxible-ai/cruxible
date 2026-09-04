"""Readmit client and CLI preserve the typed operation result."""

from __future__ import annotations

import httpx
import pytest
from click.testing import CliRunner

from cruxible_client import CruxibleClient, contracts
from cruxible_client.contracts.errors import ProposalSelectorAmbiguousError
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
        def resolve_playbill_proposal_selector(self, instance_id, selector):
            assert (instance_id, selector) == ("inst_test", SOURCE_ID)
            return contracts.PlaybillProposalSelectorResultV1(
                selector=selector, proposal_id=SOURCE_ID
            )

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


def test_cli_renders_typed_selector_candidates_and_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def readmit_playbill_proposal(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("selector refusal must precede readmission")

        def resolve_playbill_proposal_selector(self, _instance_id: str, selector: str):
            raise ProposalSelectorAmbiguousError(selector, (SOURCE_ID, NEW_ID))

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
            "sha256:11111111",
        ],
    )

    assert result.exit_code == 1
    assert SOURCE_ID in result.stderr and NEW_ID in result.stderr
    assert "Repair: cruxible playbill proposal list" in result.stderr


def test_proposal_list_rows_match_the_labelled_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def list_playbill_proposals(self, _instance_id: str, *, status: str | None):
            assert status is None
            return contracts.PlaybillProposalList(
                coordinate=COORDINATE,
                status_filter=None,
                entries=[
                    contracts.PlaybillProposalListEntry(
                        proposal_id=SOURCE_ID,
                        actor_id="operator",
                        target_ref="refs/proposals/operator/open",
                        admitted_at="2026-09-02T12:00:00.000000Z",
                        verdict="candidate",
                        candidate_digest="sha256:" + "4" * 64,
                        status="open",
                        terminal_reason=None,
                    ),
                    contracts.PlaybillProposalListEntry(
                        proposal_id=NEW_ID,
                        actor_id="operator",
                        target_ref="refs/proposals/operator/refused",
                        admitted_at="2026-09-02T12:01:00.000000Z",
                        verdict="refused",
                        candidate_digest=None,
                        status="settled",
                        terminal_reason="refused",
                    ),
                ],
            )

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
            "list",
        ],
    )

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "STATUS  TERMINAL_REASON  PROPOSAL_ID  TARGET_REF  COORDINATE_TIME"
    assert lines[1].startswith(f"open  -  {SOURCE_ID}  ")
    assert lines[2].startswith(f"settled  refused  {NEW_ID}  ")


def _withdraw_result(*, already: bool = False) -> contracts.PlaybillProposalWithdrawResult:
    return contracts.PlaybillProposalWithdrawResult(
        proposal_id=SOURCE_ID,
        actor_id="operator",
        reason="its change-set record exceeds the ledger blob ceiling",
        withdrawn_at="2026-09-08T09:00:00.000000Z",
        already_withdrawn=already,
    )


def test_withdraw_client_sends_the_reason_it_records() -> None:
    calls: list[tuple[str, object]] = []

    class StubTransport:
        def post(self, path, *, json):
            calls.append((path, json))
            return httpx.Response(200, json=_withdraw_result().model_dump(mode="json"))

    client = CruxibleClient(base_url="https://playbill.invalid")
    client._client = StubTransport()  # type: ignore[assignment]

    assert (
        client.withdraw_playbill_proposal(
            "inst_test",
            SOURCE_ID,
            reason="its change-set record exceeds the ledger blob ceiling",
        )
        == _withdraw_result()
    )
    assert calls == [
        (
            f"/api/v1/inst_test/playbill/proposals/{SOURCE_ID}/withdraw",
            {
                "tag": "playbill-proposal-withdraw-request-v1",
                "reason": "its change-set record exceeds the ledger blob ceiling",
            },
        )
    ]


def test_withdraw_cli_resolves_the_selector_and_reports_the_recorded_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def resolve_playbill_proposal_selector(self, instance_id, selector):
            assert (instance_id, selector) == ("inst_test", "sha256:11111111")
            return contracts.PlaybillProposalSelectorResultV1(
                selector=selector, proposal_id=SOURCE_ID
            )

        def withdraw_playbill_proposal(self, instance_id, proposal_id, *, reason):
            assert (instance_id, proposal_id) == ("inst_test", SOURCE_ID)
            assert reason == "its change-set record exceeds the ledger blob ceiling"
            return _withdraw_result()

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
            "withdraw",
            "sha256:11111111",
            "--reason",
            "its change-set record exceeds the ledger blob ceiling",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "target: inst_test @ https://playbill.invalid (explicit)",
        f"withdrawn  {SOURCE_ID}  2026-09-08T09:00:00.000000Z",
        "Reason: its change-set record exceeds the ledger blob ceiling",
    ]


def test_withdraw_cli_says_when_the_answer_is_the_earlier_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def resolve_playbill_proposal_selector(self, _instance_id, selector):
            return contracts.PlaybillProposalSelectorResultV1(
                selector=selector, proposal_id=SOURCE_ID
            )

        def withdraw_playbill_proposal(self, _instance_id, _proposal_id, *, reason):
            assert reason == "second thoughts"
            return _withdraw_result(already=True)

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
            "withdraw",
            SOURCE_ID,
            "--reason",
            "second thoughts",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "(already withdrawn)" in result.output
    assert "Reason: its change-set record exceeds the ledger blob ceiling" in result.output
