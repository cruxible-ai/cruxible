"""A reverse follow answers the same on HTTP, the client transport, MCP, the CLI and the SDK."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient, Playbill, contracts
from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from cruxible_client.contracts.errors import ReadRefusalError
from cruxible_core.cli.main import cli
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from tests.core_support._knowledge_loop_support import EVALUATION_TIME, SUBJECT_KIND, seed_claims
from tests.core_support._relation_query_support import BATCH_KIND, DELIVERS, seed_relations

SURFACES = ("transport", "mcp-local", "mcp-remote", "cli", "sdk")
EXPECTED = [
    ("wi-42", f"{BATCH_KIND}/b-1", "open", None),
    ("wi-42", f"{BATCH_KIND}/b-2", "closed", None),
    ("wi-43", f"{BATCH_KIND}/b-1", "open", f"{SUBJECT_KIND}/wi-42"),
]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Any:
    instance, owner = seed_claims(tmp_path_factory.mktemp("reverse-surfaces"))
    seed_relations(instance, owner)
    return instance


@pytest.fixture
def served(
    seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CruxibleClient, str]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    playbill_api.get_playbill_manager().clear()
    http = TestClient(create_app())
    instance_id = seeded.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: seeded)
    client = CruxibleClient(base_url="http://testserver")
    client._client.close()
    client._client = http  # type: ignore[assignment]
    return client, instance_id


def _playbill(client: CruxibleClient, instance_id: str, tmp_path: Path) -> Playbill:
    return Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id=instance_id,
        workspace=tmp_path,
        clock=lambda: datetime.fromisoformat(EVALUATION_TIME),
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_every_surface_follows_a_relation_backwards(
    served: tuple[CruxibleClient, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    surface: str,
) -> None:
    from cruxible_core.mcp import handlers

    client, instance_id = served
    follow = [
        {"field": DELIVERS, "as": "batch", "direction": "reverse"},
        {"field": "parent", "as": "up"},
    ]
    select = ["batch", "batch.state", "up"]
    if surface == "transport":
        result = client.query_playbill(
            instance_id,
            request=PlaybillQueryRequestV1.model_validate(
                {
                    "kind": SUBJECT_KIND,
                    "follow": follow,
                    "select": select,
                    "evaluation_time": EVALUATION_TIME,
                }
            ),
        )
    elif surface == "cli":
        from cruxible_core.cli.commands import playbill as commands

        monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(client, instance_id))
        invoked = CliRunner().invoke(
            cli,
            [
                "playbill",
                "query",
                SUBJECT_KIND,
                "--follow",
                f"^{DELIVERS}:batch",
                "--follow",
                "parent:up",
                "--select",
                ",".join(select),
                "--evaluation-time",
                EVALUATION_TIME,
                "--json",
            ],
        )
        assert invoked.exit_code == 0, invoked.output
        result = contracts.PlaybillQueryResult.model_validate(json.loads(invoked.output))
    elif surface == "sdk":
        result = (
            _playbill(client, instance_id, tmp_path)
            .query(
                SUBJECT_KIND,
                follow=[("delivers", "batch", "reverse"), ("parent", "up")],
                select=select,
            )
            .page
        )
    else:
        monkeypatch.setattr(
            handlers, "_get_client", lambda: None if surface == "mcp-local" else client
        )
        result = handlers.handle_playbill_query(
            instance_id,
            kind=SUBJECT_KIND,
            follow=[contracts.QueryFollowV1.model_validate(item) for item in follow],
            select=select,
            evaluation_time=EVALUATION_TIME,
        )
    assert (
        sorted(
            (row["subject_id"], row["batch"], row["batch.state"], row["up"]) for row in result.rows
        )
        == EXPECTED
    )
    assert [column.name for column in result.columns] == select


@pytest.mark.parametrize("surface", SURFACES)
def test_every_surface_pages_reverse_rows_and_refuses_a_wrong_predicate(
    served: tuple[CruxibleClient, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    surface: str,
) -> None:
    from cruxible_core.mcp import handlers

    client, instance_id = served
    reverse = {"field": DELIVERS, "as": "batch", "direction": "reverse"}

    def page(cursor: str | None, follow: dict[str, str] = reverse) -> Any:
        if surface == "cli":
            from cruxible_core.cli.commands import playbill as commands

            monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(client, instance_id))
            args = [
                "playbill",
                "query",
                SUBJECT_KIND,
                "--follow",
                f"^{follow['field']}:{follow['as']}",
                "--select",
                "batch",
                "--limit",
                "2",
                "--evaluation-time",
                EVALUATION_TIME,
                "--json",
            ]
            invoked = CliRunner().invoke(cli, [*args, *(["--cursor", cursor] if cursor else [])])
            assert invoked.exit_code == 0, invoked.output
            return contracts.PlaybillQueryResult.model_validate(json.loads(invoked.output))
        if surface == "sdk":
            return (
                _playbill(client, instance_id, tmp_path)
                .query(SUBJECT_KIND, follow=[follow], select=["batch"], limit=2, cursor=cursor)
                .page
            )
        if surface == "transport":
            return client.query_playbill(
                instance_id,
                request=PlaybillQueryRequestV1.model_validate(
                    {
                        "kind": SUBJECT_KIND,
                        "follow": [follow],
                        "select": ["batch"],
                        "limit": 2,
                        "cursor": cursor,
                        "evaluation_time": EVALUATION_TIME,
                    }
                ),
            )
        monkeypatch.setattr(
            handlers, "_get_client", lambda: None if surface == "mcp-local" else client
        )
        return handlers.handle_playbill_query(
            instance_id,
            kind=SUBJECT_KIND,
            follow=[contracts.QueryFollowV1.model_validate(follow)],
            select=["batch"],
            limit=2,
            cursor=cursor,
            evaluation_time=None if cursor else EVALUATION_TIME,
        )

    first = page(None)
    assert first.truncated is True and first.next_cursor is not None
    second = page(first.next_cursor)
    assert second.truncated is False and second.next_cursor is None
    rows = [(row["subject_id"], row["batch"]) for row in (*first.rows, *second.rows)]
    assert sorted(rows) == [(item[0], item[1]) for item in EXPECTED]

    wrong = {"field": "project.batch.state", "as": "batch", "direction": "reverse"}
    if surface == "cli":
        from cruxible_core.cli.commands import playbill as commands

        monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(client, instance_id))
        invoked = CliRunner().invoke(
            cli,
            ["playbill", "query", SUBJECT_KIND, "--follow", "^project.batch.state:batch"],
        )
        assert invoked.exit_code != 0
        assert "playbill.query.follow_not_incoming" in invoked.output
        assert DELIVERS in invoked.output
        return
    with pytest.raises(ReadRefusalError) as refused:
        page(None, wrong)
    assert refused.value.error_code == "playbill.query.follow_not_incoming"
    assert DELIVERS in refused.value.candidates
    assert refused.value.field_path == "follow[0].field"


@pytest.mark.parametrize("surface", SURFACES)
def test_every_surface_orients_with_the_incoming_predicates(
    served: tuple[CruxibleClient, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    surface: str,
) -> None:
    import shlex

    from cruxible_core.mcp import handlers
    from tests.core_support._relation_query_support import GOVERNS, PARENT

    client, instance_id = served
    if surface == "transport":
        result = client.orient_playbill(instance_id, kind=SUBJECT_KIND, surface="mcp")
        marker = f'follow=[{{"field": "{DELIVERS}", "as": "batch", "direction": "reverse"}}]'
    elif surface == "cli":
        from cruxible_core.cli.commands import playbill as commands

        monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(client, instance_id))
        invoked = CliRunner().invoke(cli, ["playbill", "orient", "--kind", SUBJECT_KIND, "--json"])
        assert invoked.exit_code == 0, invoked.output
        result = contracts.PlaybillOrientResultV1.model_validate(json.loads(invoked.output))
        marker = f"--follow '^{DELIVERS}:batch'"
        text = CliRunner().invoke(cli, ["playbill", "orient", "--kind", SUBJECT_KIND]).output
        assert f"Incoming (follow with ^): {DELIVERS}, {GOVERNS}, {PARENT}" in text
    elif surface == "sdk":
        result = _playbill(client, instance_id, tmp_path).orient(kind=SUBJECT_KIND)
        marker = f'pb.query(kind="{SUBJECT_KIND}", follow=[{{"field": "{DELIVERS}"'
    else:
        monkeypatch.setattr(
            handlers, "_get_client", lambda: None if surface == "mcp-local" else client
        )
        result = handlers.handle_playbill_orient(instance_id, kind=SUBJECT_KIND)
        marker = f'cruxible_playbill_query(kind="{SUBJECT_KIND}", follow=[{{"field": "{DELIVERS}"'

    assert result.kind_detail is not None
    assert result.kind_detail.incoming == (DELIVERS, GOVERNS, PARENT)
    (suggestion,) = [line for line in result.next if marker in line]
    if surface == "cli":
        # The suggested command runs as written and answers the reverse follow.
        argv = shlex.split(suggestion)[1:] + ["--evaluation-time", EVALUATION_TIME, "--json"]
        ran = CliRunner().invoke(cli, argv)
        assert ran.exit_code == 0, ran.output
        rows = json.loads(ran.output)["rows"]
        assert sorted((row["subject_id"], row["batch"]) for row in rows) == [
            (item[0], item[1]) for item in EXPECTED
        ]
