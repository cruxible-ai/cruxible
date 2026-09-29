"""`cruxible playbill get REF`: values per kind as text, the whole result as JSON."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.get_reads import (
    PlaybillByteRangeV1,
    PlaybillGetBodyV1,
    PlaybillGetClaimCardV1,
    PlaybillGetRequestV1,
    PlaybillGetResultV1,
    PlaybillGetSubjectCardV1,
    PlaybillGetSubjectClaimV1,
)
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
PREFIX = ["--server-url", "http://server", "--instance-id", "inst_get"]
WHEN = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))


class _StubClient:
    def __init__(self, result: PlaybillGetResultV1) -> None:
        self.result = result
        self.requests: list[PlaybillGetRequestV1] = []

    def playbill_get(self, instance_id: str, *, request: PlaybillGetRequestV1) -> Any:
        assert instance_id == "inst_get"
        self.requests.append(request)
        return self.result


def _stub(monkeypatch: pytest.MonkeyPatch, result: PlaybillGetResultV1) -> _StubClient:
    client = _StubClient(result)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def _result(kind: str, **fields: Any) -> PlaybillGetResultV1:
    return PlaybillGetResultV1(
        ref="ref",
        kind=kind,  # type: ignore[arg-type]
        detail=fields.pop("detail", "summary"),
        coordinate=COORDINATE,
        evaluation_time=WHEN,
        **fields,
    )


def test_a_subject_prints_its_claims_as_an_aligned_value_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _stub(
        monkeypatch,
        _result(
            "subject",
            card=PlaybillGetSubjectCardV1(
                subject="dev.roadmap_item/x",
                kind="dev.roadmap_item",
                lifecycle="live",
                claims=(
                    PlaybillGetSubjectClaimV1(predicate="adoption_state", value="adopted"),
                    PlaybillGetSubjectClaimV1(
                        predicate="task_title", value="Ship it", flags=("stale",)
                    ),
                ),
                incoming_count=2,
                next=("cruxible playbill get dev.roadmap_item/x --detail why",),
            ),
        ),
    )

    result = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "dev.roadmap_item/x"])

    assert result.exit_code == 0, result.output
    assert stub.requests[0].surface == "cli" and stub.requests[0].detail == "summary"
    assert "dev.roadmap_item/x  (live, 2 incoming)" in result.output
    assert "  adoption_state  adopted" in result.output
    assert "  task_title      Ship it  [stale]" in result.output
    assert "next: cruxible playbill get dev.roadmap_item/x --detail why" in result.output


def test_a_claim_prints_its_value_and_verdict_and_json_carries_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = PlaybillGetClaimCardV1(
        claim="CLM-" + "a" * 32,
        subject="dev.roadmap_item/x",
        predicate="adoption_state",
        predicate_full="dev.roadmap_item.adoption_state",
        value="adopted",
        verdict="supported",
        status="accepted",
        revision=2,
        flags=("contested",),
    )
    _stub(monkeypatch, _result("claim", card=card))

    text = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "CLM-aaaaaaaa"])
    raw = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "CLM-aaaaaaaa", "--json"])

    assert text.exit_code == 0, text.output
    assert "dev.roadmap_item/x  adoption_state = adopted" in text.output
    assert "verdict=supported status=accepted revision=2" in text.output
    assert "flags: contested" in text.output
    assert json.loads(raw.output)["card"]["predicate_full"] == "dev.roadmap_item.adoption_state"


def test_body_takes_a_byte_range_and_prints_the_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stub(
        monkeypatch,
        _result(
            "document",
            detail="body",
            body=PlaybillGetBodyV1(
                document="design",
                media_type="text/markdown",
                size=100,
                range=PlaybillByteRangeV1(start=0, end=10),
                text="# Design\n\n",
            ),
        ),
    )

    result = CliRunner().invoke(
        cli,
        [*PREFIX, "playbill", "get", "Document:design", "--detail", "body", "--range", "0:10"],
    )

    assert result.exit_code == 0, result.output
    assert stub.requests[0].range == PlaybillByteRangeV1(start=0, end=10)
    assert result.output.startswith("# Design\n\n")
    assert "next: --range 10:20" in result.output


def test_a_malformed_range_is_a_usage_error_with_an_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, _result("document"))

    result = CliRunner().invoke(
        cli, [*PREFIX, "playbill", "get", "Document:design", "--detail", "body", "--range", "x"]
    )

    assert result.exit_code == 2
    assert "range must be start:end" in result.output
    assert "example: cruxible playbill get" in result.output


def test_other_cards_print_names_whole_and_nested_rows_readably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import (
        PlaybillGetProposalCardV1,
        PlaybillGetProposalChangeV1,
    )

    _stub(
        monkeypatch,
        _result(
            "proposal",
            card=PlaybillGetProposalCardV1(
                proposal="sha256:" + "1" * 64,
                status="open",
                verdict="candidate",
                changes=(
                    PlaybillGetProposalChangeV1(path="documents/design.json", change="create"),
                ),
                next=("cruxible playbill proposal review sha256:" + "1" * 64,),
            ),
        ),
    )

    result = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "sha256:11111111"])

    assert result.exit_code == 0, result.output
    assert "status: open" in result.output
    assert "changes:\n  documents/design.json  create" in result.output
    assert "next: cruxible playbill proposal review sha256:" in result.output
