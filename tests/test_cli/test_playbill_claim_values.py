"""`playbill claim values` and the `--kind` filters on subject and claim lists."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.claim_reads import (
    ClaimValuesRequestV1,
    ClaimValuesResultV1,
    ClaimValueV1,
)
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
PREFIX = ["--server-url", "http://server", "--instance-id", "inst_values"]


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))


def _row(subject_id: str, value: str) -> ClaimValueV1:
    return ClaimValueV1(
        claim_id=f"project.work_item/{subject_id}/status",
        subject_path=f"subjects/project.work_item/{subject_id}.json",
        subject_id=subject_id,
        predicate="project.work_item.status",
        qualifier=None,
        role="asserted",
        object_kind="literal",
        object=LiteralClaimObject(value=value),
        value=value,
        verdict="accepted",
        status="accepted",
    )


class _StubClient:
    def __init__(self) -> None:
        self.requests: list[ClaimValuesRequestV1] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def read_playbill_claim_values(
        self, instance_id: str, *, request: ClaimValuesRequestV1
    ) -> ClaimValuesResultV1:
        assert instance_id == "inst_values"
        self.requests.append(request)
        return ClaimValuesResultV1(
            coordinate=COORDINATE,  # type: ignore[arg-type]
            evaluation_time=datetime(2026, 9, 1, tzinfo=UTC),
            values=(_row("wi-42", "ready"), _row("wi-43", "blocked")),
        )

    def list_playbill_subjects(self, instance_id: str, **kwargs: Any) -> Any:
        self.calls.append(("subjects", kwargs))
        return contracts.PlaybillSubjectList(coordinate=COORDINATE, subjects=[])

    def list_playbill_claims(self, instance_id: str, **kwargs: Any) -> Any:
        self.calls.append(("claims", kwargs))
        return contracts.PlaybillClaimList(coordinate=COORDINATE, claims=[])


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    client = _StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def test_claim_values_tabulates_a_kind(stub: _StubClient) -> None:
    result = CliRunner().invoke(
        cli,
        [
            *PREFIX,
            "playbill",
            "claim",
            "values",
            "--kind",
            "project.work_item",
            "--predicate",
            "project.work_item.status",
        ],
    )

    assert result.exit_code == 0, result.output
    assert stub.requests[0].subject_kind == "project.work_item"
    assert stub.requests[0].subject_paths == ()
    assert stub.requests[0].predicates == ("project.work_item.status",)
    assert "wi-42  project.work_item.status  ready  accepted  accepted" in result.output
    assert "wi-43  project.work_item.status  blocked  accepted  accepted" in result.output


def test_claim_values_names_subjects_of_the_kind_and_emits_json(stub: _StubClient) -> None:
    result = CliRunner().invoke(
        cli,
        [
            *PREFIX,
            "playbill",
            "claim",
            "values",
            "--kind",
            "project.work_item",
            "--subject",
            "wi-42",
            "--subject",
            "wi-43",
            "--predicate",
            "project.work_item.status",
            "--predicate",
            "project.work_item.owner",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    request = stub.requests[0]
    assert request.subject_kind is None
    assert request.subject_paths == (
        "subjects/project.work_item/wi-42.json",
        "subjects/project.work_item/wi-43.json",
    )
    assert request.predicates == ("project.work_item.status", "project.work_item.owner")
    payload = json.loads(result.stdout)
    assert [row["subject_id"] for row in payload["values"]] == ["wi-42", "wi-43"]


def test_claim_values_requires_a_predicate(stub: _StubClient) -> None:
    result = CliRunner().invoke(
        cli, [*PREFIX, "playbill", "claim", "values", "--kind", "project.work_item"]
    )

    assert result.exit_code == 2
    assert "--predicate" in result.output
    assert stub.requests == []


def test_subject_and_claim_lists_pass_the_kind_filter(stub: _StubClient) -> None:
    runner = CliRunner()
    subjects = runner.invoke(
        cli, [*PREFIX, "playbill", "subject", "list", "--kind", "project.work_item"]
    )
    claims = runner.invoke(
        cli, [*PREFIX, "playbill", "claim", "list", "--kind", "project.work_item"]
    )

    assert subjects.exit_code == 0, subjects.output
    assert claims.exit_code == 0, claims.output
    assert stub.calls[0] == ("subjects", {"subject_kind": "project.work_item"})
    assert stub.calls[1][0] == "claims"
    assert stub.calls[1][1]["subject_kind"] == "project.work_item"
