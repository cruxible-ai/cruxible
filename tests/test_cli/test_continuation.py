"""CLI local-mode continuation: --continue tokens on list reads.

Local-mode tokens are bound to the workspace (instance root), the active
config digest, and the monotonic read_revision — replay after a mutation is a
typed stale-continuation error telling the caller to restart.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import cli
from cruxible_core.graph.types import EntityInstance
from cruxible_core.service import service_add_entities


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _chdir_run(runner: CliRunner, directory: Path, args: list[str]) -> object:
    original = os.getcwd()
    try:
        os.chdir(directory)
        return runner.invoke(cli, args)
    finally:
        os.chdir(original)


def _mutate(instance: CruxibleInstance) -> None:
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-MUT",
                properties={"vehicle_id": "V-MUT", "year": 2020, "make": "Honda", "model": "Fit"},
            )
        ],
    )


class TestListEntitiesContinuation:
    def test_round_trip_pages_are_disjoint(
        self, runner: CliRunner, populated_instance: CruxibleInstance
    ) -> None:
        root = populated_instance.root
        page1 = _chdir_run(
            runner, root, ["list", "entities", "--type", "Vehicle", "--limit", "1", "--json"]
        )
        assert page1.exit_code == 0, page1.output
        payload1 = json.loads(page1.output)
        assert payload1["truncated"] is True
        assert isinstance(payload1["read_revision"], int)
        token = payload1["continuation_token"]
        assert token

        page2 = _chdir_run(
            runner,
            root,
            [
                "list",
                "entities",
                "--type",
                "Vehicle",
                "--limit",
                "1",
                "--continue",
                token,
                "--json",
            ],
        )
        assert page2.exit_code == 0, page2.output
        payload2 = json.loads(page2.output)
        ids1 = [item["entity_id"] for item in payload1["items"]]
        ids2 = [item["entity_id"] for item in payload2["items"]]
        assert set(ids1).isdisjoint(ids2)
        assert payload2["offset"] == 1
        assert payload2["truncated"] is False
        assert payload2["continuation_token"] is None

    def test_truncated_table_output_prints_resume_hint(
        self, runner: CliRunner, populated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "entities", "--type", "Vehicle", "--limit", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "Continue with: --continue" in result.output

    def test_stale_token_after_mutation_errors(
        self, runner: CliRunner, populated_instance: CruxibleInstance
    ) -> None:
        root = populated_instance.root
        page1 = _chdir_run(
            runner, root, ["list", "entities", "--type", "Vehicle", "--limit", "1", "--json"]
        )
        token = json.loads(page1.output)["continuation_token"]

        _mutate(populated_instance)

        stale = _chdir_run(
            runner,
            root,
            [
                "list",
                "entities",
                "--type",
                "Vehicle",
                "--limit",
                "1",
                "--continue",
                token,
                "--json",
            ],
        )
        assert stale.exit_code != 0
        assert "Stale continuation token" in stale.output
        assert "Restart the read" in stale.output

    def test_malformed_token_errors(
        self, runner: CliRunner, populated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "entities", "--type", "Vehicle", "--continue", "garbage!!", "--json"],
        )
        assert result.exit_code != 0
        assert "Invalid continuation token" in result.output


class TestListEdgesContinuation:
    def test_round_trip(self, runner: CliRunner, populated_instance: CruxibleInstance) -> None:
        root = populated_instance.root
        page1 = _chdir_run(runner, root, ["list", "edges", "--limit", "1", "--json"])
        assert page1.exit_code == 0, page1.output
        payload1 = json.loads(page1.output)
        assert payload1["truncated"] is True
        token = payload1["continuation_token"]
        assert token

        page2 = _chdir_run(
            runner, root, ["list", "edges", "--limit", "10", "--continue", token, "--json"]
        )
        assert page2.exit_code == 0, page2.output
        payload2 = json.loads(page2.output)
        keys1 = {
            (e["from_id"], e["to_id"], e["relationship_type"], e["edge_key"])
            for e in payload1["items"]
        }
        keys2 = {
            (e["from_id"], e["to_id"], e["relationship_type"], e["edge_key"])
            for e in payload2["items"]
        }
        assert keys1.isdisjoint(keys2)
        assert len(keys1 | keys2) == payload1["total"]


def test_inspect_continue_requires_expanded_read(
    runner: CliRunner, populated_instance: CruxibleInstance
) -> None:
    result = _chdir_run(
        runner,
        populated_instance.root,
        ["entity", "inspect", "--type", "Vehicle", "--id", "V-2024-CIVIC-EX", "--continue", "x"],
    )
    assert result.exit_code != 0
    assert "--continue applies only to the expanded neighborhood read" in result.output
