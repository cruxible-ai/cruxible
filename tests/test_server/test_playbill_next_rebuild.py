"""The operator's repair for the next worker: drop its tables and derive them again (R6)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient, contracts
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.cli.main import cli
from cruxible_core.consumers.next import facts
from cruxible_core.runtime.playbill_manager import get_playbill_manager

ROUTE = "/api/v1/{instance_id}/instance/rebuild-next"
COORDINATE = {
    "tag": "playbill-accepted-coordinate-v1",
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}


def test_the_route_rebuilds_from_nothing_and_clears_the_error(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http
    instance = get_playbill_manager().get(instance_id)

    first = client.post(ROUTE.format(instance_id=instance_id))
    assert first.status_code == 200, first.text
    result = contracts.NextRebuildResult.model_validate(first.json())
    assert (result.instance_id, result.format) == (instance_id, facts.FORMAT)
    assert result.coordinate.git_oid == instance.accepted_coordinate().git_oid
    tables = facts.stored_tables(instance)
    assert tables is not None and len(tables["claim_facts"]) == result.claims

    # Damaged state with a recorded error: the repair replaces both.
    with sqlite3.connect(facts._STATE.path(instance)) as connection:
        connection.execute("UPDATE progress SET last_error='broken',failed=target")
        connection.execute("DELETE FROM claim_facts")
        connection.execute("CREATE TABLE stray(value TEXT)")
    again = client.post(ROUTE.format(instance_id=instance_id))
    assert again.status_code == 200, again.text
    assert contracts.NextRebuildResult.model_validate(again.json()) == result
    assert facts.stored_tables(instance) == tables
    (health,) = facts._PART.health(instance, now=utc_now())
    assert health.state == "running" and health.detail["last_error"] is None


def test_the_client_posts_to_the_rebuild_route() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(
            200,
            json={
                "tag": "playbill-next-rebuild-result-v1",
                "instance_id": "inst_a",
                "coordinate": COORDINATE,
                "format": facts.FORMAT,
                "claims": 3,
                "keys": 5,
                "rows": 2,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    result = client.rebuild_next("inst_a")
    assert seen == ["POST /api/v1/inst_a/instance/rebuild-next"]
    assert (result.claims, result.keys, result.rows) == (3, 5, 2)


def test_the_cli_names_its_target_and_reports_what_it_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class StubClient:
        def rebuild_next(self, instance_id: str) -> contracts.NextRebuildResult:
            calls.append(instance_id)
            return contracts.NextRebuildResult(
                instance_id=instance_id,
                coordinate=contracts.AcceptedCoordinate.model_validate(COORDINATE),
                format=facts.FORMAT,
                claims=3,
                keys=5,
                rows=2,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    args: list[Any] = [
        "--server-url",
        "https://repair.example.test",
        "--instance-id",
        "inst_repair",
        "instance",
        "rebuild-next",
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert calls == ["inst_repair"]
    assert "3 Claims, 5 row keys, 2 rows" in result.output
    assert result.stderr == "target: inst_repair @ https://repair.example.test (explicit)\n"
    as_json = CliRunner().invoke(cli, [*args, "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout)["tag"] == "playbill-next-rebuild-result-v1"
