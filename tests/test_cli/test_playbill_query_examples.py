"""Query authoring converges on the durable coordinator ceremony."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

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
        ["playbill", "authoring", "create", "--example", "query-claims-by-type"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "query_definition"
    definition = payload["query_definition"]
    assert definition["entry"]["subject_kinds"] == ["project.work_item"]
    assert definition["evaluation_policy"]["visible_verdicts"] == ["supported"]
    assert definition["evaluation_policy"]["visible_currency"] == ["current"]
    assert definition["pins"][0]["target"] == {
        "kind": "ClaimType",
        "name": "project.work_item.status",
    }


def test_query_propose_is_a_typed_deprecation_shim() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "query",
            "propose",
            "--example",
            "query-claims-by-type",
        ],
    )

    assert result.exit_code != 0
    assert "playbill.write_surface_deprecated" in result.output
    assert "playbill authoring create --example query-claims-by-type" in result.output
