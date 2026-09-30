"""`Playbill.query`, its QueryResult, and the World's typed where/select sugar."""

from __future__ import annotations

from datetime import UTC, datetime
from keyword import iskeyword
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import Playbill
from cruxible_client import contracts as api
from cruxible_client.authoring.compact_query import (
    CompactQuery,
    QueryNameError,
    QueryResult,
    keyword_field,
    keyword_name,
    parse_where,
)
from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from tests.test_client.test_playbill_sdk_world import (
    _COORDINATE,
    SEVERITY,
    _claim_type,
    _mypy,
    _workspace,
    _WorldClient,
)

WHEN = datetime(2026, 9, 7, 12, tzinfo=UTC)


class _QueryClient(_WorldClient):
    """A daemon answering query in two pages of one row each."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[PlaybillQueryRequestV1] = []

    def query_playbill(
        self, _instance_id: str, *, request: PlaybillQueryRequestV1
    ) -> api.PlaybillQueryResult:
        self.requests.append(request)
        second = request.cursor == "page-2"
        subject_id = "cve-2" if second else "cve-1"
        return api.PlaybillQueryResult(
            kind="sec.vulnerability",
            columns=(api.PlaybillQueryColumnV1(name="severity", predicate=SEVERITY, type="enum"),),
            rows=(
                {
                    "subject": f"sec.vulnerability/{subject_id}",
                    "subject_id": subject_id,
                    "severity": "high",
                    "flags": ["stale"] if second else [],
                },
            ),
            truncated=not second,
            next_cursor=None if second else "page-2",
            receipt=api.PlaybillQueryReceiptV1(
                mode="inline",
                spec_digest="sha256:" + "6" * 64,
                coordinate=_COORDINATE.model_dump(mode="json"),  # type: ignore[arg-type]
                evaluation_time=WHEN,
            ),
        )


@pytest.fixture
def connection(tmp_path: Path) -> tuple[Playbill, _QueryClient]:
    _workspace(tmp_path)
    client = _QueryClient()
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: WHEN,
    )
    return playbill, client


def test_query_returns_a_result_page_that_continues_and_prints(
    connection: tuple[Playbill, _QueryClient],
) -> None:
    playbill, client = connection

    result = playbill.query(
        "sec.vulnerability",
        where=[{"field": "severity", "eq": "high"}],
        select=["severity"],
        follow=[("affects_package", "pkg")],
    )

    assert isinstance(result, QueryResult)
    assert result.rows[0]["subject_id"] == "cve-1"
    assert result.truncated is True
    request = client.requests[0]
    assert request.kind == "sec.vulnerability"
    assert request.where[0].field == "severity" and request.where[0].value == "high"
    assert request.follow[0].as_ == "pkg"
    assert request.at is None
    assert request.evaluation_time == WHEN

    following = result.next_page()
    assert following is not None
    assert [row["subject_id"] for row in following] == ["cve-2"]
    assert client.requests[1].cursor == "page-2"
    assert client.requests[1].at is not None
    assert following.next_page() is None
    table = following.table()
    assert "subject" in table.splitlines()[0] and "flags" in table.splitlines()[0]
    assert "sec.vulnerability/cve-2  high      stale" in table


def test_query_follows_backwards_from_a_tuple_a_mapping_or_the_model(
    connection: tuple[Playbill, _QueryClient],
) -> None:
    playbill, client = connection

    playbill.query(
        "dev.roadmap_item",
        follow=[
            ("dev.batch.delivers", "batch", "reverse"),
            ("refines", "parent"),
            {"field": "governs", "as": "decision", "direction": "reverse"},
            api.QueryFollowV1(field="blocks", as_="blocker", direction="reverse"),
        ],
    )

    assert [(item.field, item.as_, item.direction) for item in client.requests[0].follow] == [
        ("dev.batch.delivers", "batch", "reverse"),
        ("refines", "parent", "forward"),
        ("governs", "decision", "reverse"),
        ("blocks", "blocker", "reverse"),
    ]
    with pytest.raises(ValueError, match="direction"):
        playbill.query("dev.roadmap_item", follow=[("x", "y", "sideways")])  # type: ignore[list-item]


def test_world_where_and_select_build_the_same_query(
    connection: tuple[Playbill, _QueryClient],
) -> None:
    playbill, client = connection
    world = playbill.world()

    query = world.sec.vulnerability.where(
        severity="high", severity__ne="low", subject_id__contains="cve"
    ).select("severity")

    assert isinstance(query, CompactQuery)
    request = query.request()
    # The World's short names go to the wire as full predicates, which the
    # daemon's field-naming rule always resolves as themselves.
    assert [(item.field, item.operator, item.value) for item in request.where] == [
        (SEVERITY, "eq", "high"),
        (SEVERITY, "ne", "low"),
        ("subject_id", "contains", "cve"),
    ]
    assert request.select == (SEVERITY,)
    assert world.sec.vulnerability.select("severity").order_by("-severity").request().order_by == (
        f"-{SEVERITY}",
    )
    assert [row["subject_id"] for row in query] == ["cve-1", "cve-2"]
    sent = client.requests[0]
    assert sent.at is not None and sent.at.git_oid == world.coordinate.git_oid
    # An enum member minted from the World is the same value.
    minted = world.sec.vulnerability.where(severity=world.sec.vuln.severity.high).request()
    assert minted.where[0].value == "high"


@pytest.mark.parametrize(
    ("build", "message", "nearest"),
    [
        (lambda kind: kind.where(sevrity="high"), "has no field 'sevrity'", "severity"),
        (lambda kind: kind.where(severity="hihg"), "not a member", "high"),
        (lambda kind: kind.where(severity__lt="high"), "does not apply", None),
        (lambda kind: kind.where(severity__bogus="high"), "names no operator", None),
        (lambda kind: kind.select("sevrity"), "has no field", "severity"),
    ],
)
def test_world_sugar_refuses_wrong_names_before_the_wire(
    connection: tuple[Playbill, _QueryClient],
    build: Any,
    message: str,
    nearest: str | None,
) -> None:
    playbill, client = connection
    kind = playbill.world().sec.vulnerability

    with pytest.raises(QueryNameError, match=message) as refused:
        build(kind)

    if nearest is not None:
        assert nearest in refused.value.nearest
    assert client.requests == []


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("adoption_state=adopted", ("adoption_state", "eq", "adopted")),
        ("state!=done", ("state", "ne", "done")),
        ("count<=3", ("count", "lte", "3")),
        ("release in OSS v1, Post-v1", ("release", "in", ("OSS v1", "Post-v1"))),
        ("note exists", ("note", "exists", True)),
        ("note !exists", ("note", "exists", False)),
        ("title~ledger", ("title", "contains", "ledger")),
    ],
)
def test_cli_where_expressions(expression: str, expected: tuple[str, str, object]) -> None:
    parsed = parse_where(expression)
    assert (parsed.field, parsed.operator, parsed.value) == expected


def test_a_type_checker_reads_where_as_typed_keywords(
    connection: tuple[Playbill, _QueryClient], tmp_path: Path
) -> None:
    playbill, _client = connection
    project = tmp_path / "query-project"
    project.mkdir()
    (project / "world.pyi").write_text(playbill.world().stub(), encoding="utf-8")
    (project / "queries.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "from world import World\n"
        "\n"
        "\n"
        "def queries(world: World) -> None:\n"
        '    world.sec.vulnerability.where(severity="high", severity__in=["low"]).run()\n'
        '    world.sec.vulnerability.where(severity="hihg")\n'
        '    world.sec.vulnerability.where(sevrity="high")\n'
        '    world.sec.vulnerability.where(severity__lt="high")\n'
        '    world.sec.vulnerability.select("severity", "subject_id").limit(5)\n'
        '    world.sec.vulnerability.select("sevrity")\n',
        encoding="utf-8",
    )

    report = _mypy(project, "queries.py")
    assert "queries.py:7:" not in report, report
    for line in (8, 9, 10, 12):
        assert f"queries.py:{line}: error:" in report, report
    assert "queries.py:11:" not in report, report


# ---------------------------------------------------------------------------
# Field leaves the generated where() signature must escape
# ---------------------------------------------------------------------------

_ESCAPED_LEAVES = ("self", "class", "status", "status__ne", "note_")


class _ReservedLeafClient(_QueryClient):
    """A kind whose predicate leaves collide with `self`, keywords and suffixes."""

    def list_playbill_claim_types(
        self, _instance_id: str, *, at: Any = None
    ) -> api.PlaybillClaimTypeList:
        self.claim_type_list_calls += 1
        return api.PlaybillClaimTypeList(
            coordinate=at or self.coordinate,
            claim_types=[_claim_type(f"sec.vulnerability.{leaf}") for leaf in _ESCAPED_LEAVES],
        )


def test_a_stub_for_reserved_leaves_compiles_and_type_checks(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _ReservedLeafClient()
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client, instance_id="inst_world", workspace=tmp_path, clock=lambda: WHEN
    )
    world = playbill.world()
    rendered = world.stub()
    compile(rendered, "world.pyi", "exec")

    project = tmp_path / "reserved-project"
    project.mkdir()
    (project / "world.pyi").write_text(rendered, encoding="utf-8")
    (project / "reserved.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "from world import World\n"
        "\n"
        "\n"
        "def queries(world: World) -> None:\n"
        "    world.sec.vulnerability.where(\n"
        '        self_="high", class___ne="low", status="high", status__ne_="low",\n'
        '        status__ne="low", note____in=["high"],\n'
        "    )\n"
        '    world.sec.vulnerability.where(self_="hihg")\n',
        encoding="utf-8",
    )
    report = _mypy(project, "reserved.py")
    assert "reserved.py:7:" not in report and "reserved.py:8:" not in report, report
    assert "reserved.py:11: error:" in report, report

    # The escaped keywords the stub declares are the ones where() reads.
    request = world.sec.vulnerability.where(  # type: ignore[attr-defined]
        self_="high",
        class___ne="low",
        status="high",
        status__ne_="low",
        status__ne="low",
        note____in=["high"],
    ).request()
    kind = "sec.vulnerability"
    assert [(item.field, item.operator, item.value) for item in request.where] == [
        (f"{kind}.self", "eq", "high"),
        (f"{kind}.class", "ne", "low"),
        (f"{kind}.status", "eq", "high"),
        (f"{kind}.status__ne", "eq", "low"),
        (f"{kind}.status", "ne", "low"),
        (f"{kind}.note_", "in", ("high",)),
    ]


@pytest.mark.parametrize(
    "field", ["self", "class", "status", "status__ne", "note_", "_", "__x", "a__b__in", "match"]
)
@pytest.mark.parametrize("operator", ["eq", "ne", "in", "exists", "contains"])
def test_where_keyword_escape_round_trips(field: str, operator: str) -> None:
    spelled = keyword_name(field)
    assert spelled is not None and spelled.isidentifier() and not iskeyword(spelled)
    key = spelled if operator == "eq" else f"{spelled}__{operator}"
    assert keyword_field(key) == (field, operator)
