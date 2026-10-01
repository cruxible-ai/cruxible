"""`cruxible playbill query [KIND] --where ...` beside the query list/get/run leaves."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from cruxible_core.cli.main import cli

PREFIX = ["--server-url", "http://server", "--instance-id", "inst_query"]
COORDINATE = {
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))


class _StubClient:
    def __init__(self) -> None:
        self.requests: list[PlaybillQueryRequestV1] = []
        self.listed = 0

    def query_playbill(
        self, instance_id: str, *, request: PlaybillQueryRequestV1
    ) -> contracts.PlaybillQueryResult:
        assert instance_id == "inst_query"
        self.requests.append(request)
        return contracts.PlaybillQueryResult(
            kind=request.kind,
            columns=(
                contracts.PlaybillQueryColumnV1(name="task_title", type="string"),
                contracts.PlaybillQueryColumnV1(
                    name="refines", type="subject:dev.roadmap_item", cardinality="many"
                ),
            ),
            rows=(
                {
                    "subject": "dev.roadmap_item/cold-start",
                    "subject_id": "cold-start",
                    "task_title": "Make a restart fast",
                    "refines": ["dev.roadmap_item/perf", "dev.roadmap_item/ops"],
                    "flags": ["stale"],
                },
            ),
            truncated=True,
            next_cursor="CURSOR",
            notes=("showing 12 of 13 predicates; left out: x",),
            receipt=contracts.PlaybillQueryReceiptV1(
                mode="inline",
                spec_digest="sha256:" + "5" * 64,
                coordinate=COORDINATE,  # type: ignore[arg-type]
                evaluation_time=datetime(2026, 9, 28, tzinfo=UTC),
            ),
        )

    def list_playbill_query_definitions(self, instance_id: str) -> Any:
        self.listed += 1
        return contracts.PlaybillQueryDefinitionList(
            coordinate=contracts.PlaybillAcceptedCoordinate(**COORDINATE), query_definitions=[]
        )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    client = _StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, [*PREFIX, "playbill", "query", *args])


def test_compact_query_prints_a_table_of_values_flags_and_the_next_command(
    stub: _StubClient,
) -> None:
    result = _run(
        "dev.roadmap_item",
        "--where",
        "adoption_state=adopted",
        "--where",
        "implementation_state!=completed",
        "--where",
        "proposed_release in OSS v1,Post-v1",
        "--where",
        "note exists",
        "--where",
        "task_title~restart",
        "--select",
        "task_title,refines",
        "--follow",
        "refines:parent",
        "--order-by",
        "-task_title",
        "--limit",
        "5",
    )

    assert result.exit_code == 0, result.output
    request = stub.requests[0]
    assert request.kind == "dev.roadmap_item"
    assert [(item.field, item.operator, item.value) for item in request.where] == [
        ("adoption_state", "eq", "adopted"),
        ("implementation_state", "ne", "completed"),
        ("proposed_release", "in", ("OSS v1", "Post-v1")),
        ("note", "exists", True),
        ("task_title", "contains", "restart"),
    ]
    assert request.select == ("task_title", "refines")
    assert request.follow[0].field == "refines" and request.follow[0].as_ == "parent"
    assert request.order_by == ("-task_title",)
    assert request.limit == 5
    lines = result.output.splitlines()
    assert lines[0].split() == ["subject", "task_title", "refines", "flags"]
    assert "dev.roadmap_item/cold-start" in lines[1]
    assert "dev.roadmap_item/perf, dev.roadmap_item/ops" in lines[1]
    assert lines[1].rstrip().endswith("stale")
    assert "note: showing 12 of 13 predicates" in result.output
    assert lines[-1].startswith("next: cruxible playbill query dev.roadmap_item --where")
    assert lines[-1].endswith("--cursor CURSOR")


def test_json_gives_the_full_structured_answer(stub: _StubClient) -> None:
    result = _run("--contains", "OSS v1", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["receipt"]["mode"] == "inline"
    assert payload["next_cursor"] == "CURSOR"
    assert stub.requests[0].kind is None and stub.requests[0].contains == "OSS v1"


def test_named_and_spec_modes_forward_params_and_the_spec(
    stub: _StubClient, tmp_path: Path
) -> None:
    from tests.core_support._knowledge_loop_support import work_item_query

    assert (
        _run("--name", "project.roadmap", "--param", "limit=3", "--param", "who=ops").exit_code == 0
    )
    assert stub.requests[0].name == "project.roadmap"
    assert stub.requests[0].params == {"limit": 3, "who": "ops"}

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(work_item_query().model_dump(mode="json")), encoding="utf-8")
    assert _run("--spec", str(spec)).exit_code == 0
    assert stub.requests[1].spec is not None


def test_malformed_input_refuses_with_the_syntax(stub: _StubClient) -> None:
    where = _run("dev.roadmap_item", "--where", "adoption_state")
    assert where.exit_code != 0
    assert "'f=v'" in where.output and "adoption_state=adopted" in where.output

    follow = _run("dev.card", "--follow", "closed_by")
    assert follow.exit_code != 0 and "field:alias" in follow.output

    misplaced = _run("--where", "a=b", "dev.roadmap_item")
    assert misplaced.exit_code != 0 and "put KIND first" in misplaced.output
    assert stub.requests == []


def test_follow_in_follows_a_relation_backwards_in_command_line_order(
    stub: _StubClient,
) -> None:
    result = _run(
        "dev.roadmap_item",
        "--follow-in",
        "dev.batch.delivers:batch",
        "--follow",
        "refines:parent",
        "--follow-in=governs:decision",
        "--select",
        "batch,batch.state",
    )

    assert result.exit_code == 0, result.output
    follow = stub.requests[0].follow
    assert [(item.field, item.as_, item.direction) for item in follow] == [
        ("dev.batch.delivers", "batch", "reverse"),
        ("refines", "parent", "forward"),
        ("governs", "decision", "reverse"),
    ]
    # The next-page command repeats the reverse follows as written; nothing to quote.
    last = result.output.splitlines()[-1]
    assert "--follow-in dev.batch.delivers:batch" in last and "'" not in last

    for bad in ("dev.batch.delivers", ":batch"):
        refused = _run("dev.roadmap_item", "--follow-in", bad)
        assert refused.exit_code != 0
        assert "field:alias" in refused.output and "dev.batch.delivers:batch" in refused.output
        assert "--follow-in" in refused.output
    assert len(stub.requests) == 1


def test_follow_order_falls_back_when_the_raw_arguments_disagree() -> None:
    from cruxible_core.cli.commands.playbill import _follow_order

    parsed = _follow_order(
        ["k", "--contains", "--follow", "--follow-in", "a:b"], ("x:y",), ("a:b",)
    )
    assert parsed == [("--follow", "x:y"), ("--follow-in", "a:b")]
    ordered = _follow_order(["k", "--follow-in", "a:b", "--follow", "x:y"], ("x:y",), ("a:b",))
    assert ordered == [("--follow-in", "a:b"), ("--follow", "x:y")]


def test_the_named_entrypoint_leaves_still_answer(stub: _StubClient) -> None:
    listed = _run("list")
    assert listed.exit_code == 0, listed.output
    assert stub.listed == 1 and stub.requests == []

    helped = _run()
    assert helped.exit_code == 0
    assert "Query accepted state" in helped.output and "run" in helped.output


def test_table_cells_show_exact_content_text_cut_values_and_markers() -> None:
    from cruxible_client.authoring.compact_query import render_query_table
    from cruxible_client.contracts.get_reads import (
        PlaybillExactContentRefV1,
        PlaybillGetTruncatedTextV1,
    )

    digest = "sha256:" + "cd" * 32
    page = contracts.PlaybillQueryResult(
        kind="legal.case",
        columns=(contracts.PlaybillQueryColumnV1(name="ruling", type="exact_content"),),
        rows=(
            {"subject": "legal.case/a", "subject_id": "a", "ruling": "Affirmed.", "flags": []},
            {
                "subject": "legal.case/b",
                "subject_id": "b",
                "ruling": PlaybillGetTruncatedTextV1(value="Reversed " * 60, length=900),
                "flags": [],
            },
            {
                "subject": "legal.case/c",
                "subject_id": "c",
                "ruling": PlaybillExactContentRefV1(
                    exact_content="unavailable", content_digest=digest, length=40
                ),
                "flags": [],
            },
        ),
        receipt=contracts.PlaybillQueryReceiptV1(
            mode="inline",
            spec_digest="sha256:" + "0" * 64,
            coordinate=COORDINATE,  # type: ignore[arg-type]
            evaluation_time=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )

    table = render_query_table(page)

    assert "Affirmed." in table
    assert "Reversed Reversed" in table and "…" in table
    assert "<unavailable 40 bytes sha256:cdcdcdcdcdcd>" in table


def test_status_claims_budgets_and_receipt_reach_the_request(stub: _StubClient) -> None:
    result = _run(
        "dev.roadmap_item",
        "--status",
        "live",
        "--status",
        "retired",
        "--claims",
        "--json",
    )
    assert result.exit_code == 0, result.output
    request = stub.requests[-1]
    assert request.status == ("live", "retired") and request.claims is True

    named = _run(
        "--name",
        "dev.items",
        "--budgets",
        '{"max_results": 7, "max_traversal_depth": 0}',
        "--receipt",
        "full",
        "--json",
    )
    assert named.exit_code == 0, named.output
    request = stub.requests[-1]
    assert request.budgets is not None and request.budgets.max_results == 7
    assert request.receipt == "full"
