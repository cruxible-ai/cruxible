"""Query authoring examples are local, model-generated, and compose with propose."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_core.cli.main import cli


def test_query_propose_example_is_a_local_canonical_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("an example must not call the daemon")),
    )

    result = CliRunner().invoke(
        cli,
        ["playbill", "query", "propose", "--example", "query-claims-by-type"],
    )

    assert result.exit_code == 0, result.output
    definition = QueryDefinitionV1.model_validate(json.loads(result.output))
    assert definition.entry.subject_kinds == ("project.work_item",)
    assert definition.evaluation_policy.visible_verdicts == ("supported",)
    assert definition.evaluation_policy.visible_currency == ("current",)
    assert definition.pins[0].target.qualified == "ClaimType:project.work_item.status"


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--envelope", "query.json", "--example", "query-claims-by-type"],
    ],
)
def test_query_propose_requires_exactly_one_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    (tmp_path / "query.json").write_text("{}\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["playbill", "query", "propose", *arguments])

    assert result.exit_code == 2
    assert "exactly one of --envelope or --example" in result.output


def test_query_propose_envelope_still_uses_the_served_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = tmp_path / "query.json"
    envelope.write_text('{"artifact_format":"playbill-query-definition-v1"}\n')
    calls: list[tuple[str, dict[str, Any], str]] = []

    class Result:
        def model_dump(self, *, mode: str) -> dict[str, str]:
            assert mode == "json"
            return {"tag": "proposal-result"}

    class Client:
        def propose_playbill_query_definition(
            self,
            instance_id: str,
            *,
            query: dict[str, Any],
            proposal_name: str,
        ) -> Result:
            calls.append((instance_id, query, proposal_name))
            return Result()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", Client)

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_query",
            "playbill",
            "query",
            "propose",
            "--envelope",
            str(envelope),
            "--name",
            "query-proposal",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "inst_query",
            {"artifact_format": "playbill-query-definition-v1"},
            "query-proposal",
        )
    ]
