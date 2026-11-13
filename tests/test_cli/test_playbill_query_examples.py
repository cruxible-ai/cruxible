"""Query authoring converges on the durable coordinator ceremony."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_core.cli.main import cli


def test_query_coordinator_example_is_local_and_directly_creatable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("an example must not call the daemon")),
    )

    result = CliRunner().invoke(
        cli,
        ["authoring", "example", "query-claims-by-type"],
    )

    assert result.exit_code == 0, result.output
    # stdout is the one JSON document; an example's note goes to stderr.
    payload = json.loads(result.stdout)
    assert payload["kind"] == "query_definition"
    definition = payload["query_definition"]
    assert definition["entry"]["subject_kinds"] == ["project.work_item"]
    assert definition["evaluation_policy"]["visible_verdicts"] == ["supported"]
    assert definition["evaluation_policy"]["visible_currency"] == ["current"]
    # One Query format: the Query names the ClaimTypes it reads and carries no
    # digest pins for the author to fill.
    assert definition["artifact_format"] == "cruxible-query-definition-v3"
    assert "pins" not in definition
    assert QueryDefinition.model_validate(definition).identity.name == (
        "project.work_items_by_status"
    )
