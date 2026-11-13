"""``orient(section="graph")``: live Subjects, then the live Claims between them, keyset-paged."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import WriteRequest
from cruxible_core.cli.commands.playbill import _render_orient
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.graph import (
    GRAPH_EDGES_SQL,
    GRAPH_KINDS_SQL,
    GRAPH_NODES_SQL,
)
from cruxible_core.service.discovery.orient import _keyset_cursor, service_playbill_orient
from cruxible_core.service.list_pages import ListCursorMismatch
from tests.core_support._write_support import KIND, caller, seed_write_surface

GOVERNS = f"{KIND}.governs"


def _write(instance: PlaybillInstance, *changes: dict[str, Any]) -> Any:
    request = WriteRequest.model_validate({"because": "Graph.", "changes": list(changes)})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return outcome


def _title(name: str) -> dict[str, Any]:
    return {"op": "set", "subject": f"{KIND}/{name}", "field": "title", "value": name}


def _governs(source: str, target: str) -> dict[str, Any]:
    return {
        "op": "add",
        "subject": f"{KIND}/{source}",
        "field": "governs",
        "value": f"{KIND}/{target}",
    }


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    instance = seed_write_surface(tmp_path)[0]
    _write(instance, *(_title(name) for name in ("a", "b", "c", "d")))
    _write(instance, _governs("a", "b"), _governs("a", "c"), _governs("d", "b"))
    return instance


def _orient(instance: PlaybillInstance, **fields: Any) -> Any:
    return service_playbill_orient(instance, section="graph", **fields)


def _all_pages(instance: PlaybillInstance, *, limit: int, cursor: str | None = None) -> list[Any]:
    pages = [_orient(instance, limit=limit, cursor=cursor)]
    while pages[-1].next_cursor is not None:
        pages.append(_orient(instance, limit=limit, cursor=pages[-1].next_cursor))
    return pages


def _ref(path: str) -> str:
    return path.removeprefix("subjects/").removesuffix(".json")


def _live_subjects(instance: PlaybillInstance) -> list[str]:
    """Every live Subject's ref, by kind then path: the order the graph pages in."""

    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        return [
            _ref(str(path))
            for (path,) in projection.typed.connection.execute(
                "SELECT path FROM subjects WHERE lifecycle='live' ORDER BY subject_kind, path"
            )
        ]


def _live_claims(instance: PlaybillInstance) -> set[tuple[str, str, str, str]]:
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        return {
            (str(identity).removeprefix("Claim:"), str(source), str(predicate), str(target))
            for identity, source, predicate, target in projection.typed.connection.execute(
                "SELECT identity, subject_path, predicate, object_path FROM claims "
                "WHERE object_kind='subject' AND lifecycle='live'"
            )
        }


def test_pages_return_every_node_then_every_edge_once(instance: PlaybillInstance) -> None:
    pages = _all_pages(instance, limit=2)
    nodes = [node for page in pages for node in page.graph.nodes]
    edges = [edge for page in pages for edge in page.graph.edges]

    subjects = _live_subjects(instance)
    assert {f"{KIND}/{name}" for name in "abcd"} <= set(subjects)
    assert [node.subject for node in nodes] == subjects
    assert {node.kind for node in nodes} == {KIND}
    # Edges follow the nodes: no page carries a node after its first edge.
    seen_edge = False
    for page in pages:
        assert not (seen_edge and page.graph.nodes)
        seen_edge = seen_edge or bool(page.graph.edges)
    assert {(edge.source, edge.predicate, edge.target) for edge in edges} == {
        (f"{KIND}/a", GOVERNS, f"{KIND}/b"),
        (f"{KIND}/a", GOVERNS, f"{KIND}/c"),
        (f"{KIND}/d", GOVERNS, f"{KIND}/b"),
    }
    # By target, then Claim; each edge's claim is the stored live Claim's id.
    assert [edge.target for edge in edges] == sorted(edge.target for edge in edges)
    assert {(edge.claim, edge.source, edge.predicate, edge.target) for edge in edges} == {
        (claim, _ref(source), predicate, _ref(target))
        for claim, source, predicate, target in _live_claims(instance)
    }
    assert all(page.graph.totals.subjects == len(subjects) for page in pages)
    assert [page.truncated for page in pages] == [True] * (len(pages) - 1) + [False]
    assert all(page.coordinate == pages[0].coordinate for page in pages)
    rows = sum(len(page.graph.nodes) + len(page.graph.edges) for page in pages)
    assert rows == len(subjects) + 3
    assert all(len(page.graph.nodes) + len(page.graph.edges) <= 2 for page in pages)
    # One page holds everything when the limit allows it.
    whole = _orient(instance, limit=50)
    assert (len(whole.graph.nodes), len(whole.graph.edges)) == (len(subjects), 3)
    assert not whole.truncated and whole.next_cursor is None
    assert whole.next == (f"cruxible get {subjects[0]}",)


def test_a_page_boundary_at_the_last_node_continues_into_the_edges(
    instance: PlaybillInstance,
) -> None:
    count = len(_live_subjects(instance))
    first = _orient(instance, limit=count)
    assert len(first.graph.nodes) == count and not first.graph.edges and first.truncated
    second = _orient(instance, limit=count, cursor=first.next_cursor)
    assert not second.graph.nodes and len(second.graph.edges) == 3
    assert not second.truncated


def test_a_retired_edge_leaves_and_an_earlier_coordinate_still_has_it(
    instance: PlaybillInstance,
) -> None:
    before = _orient(instance, limit=50)
    _write(instance, {"op": "retire", "target": {"subject": f"{KIND}/d", "field": "governs"}})
    after = _orient(instance, limit=50)
    assert [(edge.source, edge.target) for edge in after.graph.edges] == [
        (f"{KIND}/a", f"{KIND}/b"),
        (f"{KIND}/a", f"{KIND}/c"),
    ]
    earlier = _orient(instance, limit=50, at=before.coordinate.git_oid)
    assert earlier.graph == before.graph and earlier.coordinate == before.coordinate


def test_a_cursor_pins_its_coordinate_and_a_foreign_cursor_is_refused(
    instance: PlaybillInstance,
) -> None:
    first = _orient(instance, limit=3)
    assert first.next_cursor is not None
    count = len(_live_subjects(instance))
    _write(instance, _title("e"), _governs("e", "a"))
    rest = _all_pages(instance, limit=3, cursor=first.next_cursor)
    assert all(page.coordinate == first.coordinate for page in rest)
    edges = [edge for page in rest for edge in page.graph.edges]
    assert f"{KIND}/e" not in {node.subject for page in rest for node in page.graph.nodes}
    assert len(edges) == 3
    assert 3 + sum(len(page.graph.nodes) for page in rest) == count
    for key in (["x", "1"], ["edge", "only-two"], ["vertex", "a", "b"]):
        foreign = _keyset_cursor(view="graph", served=first.coordinate, last_key=key)
        with pytest.raises(ListCursorMismatch):
            _orient(instance, cursor=foreign)
    runs = _keyset_cursor(view="runs", served=first.coordinate, last_key=["edge", "", ""])
    with pytest.raises(ListCursorMismatch):
        _orient(instance, cursor=runs)


def test_the_graph_reads_two_indexes_by_their_pinned_plans(instance: PlaybillInstance) -> None:
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection

        def plan(sql: str, args: tuple[object, ...]) -> list[str]:
            return [str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, args)]

        assert plan(GRAPH_KINDS_SQL, ()) == ["SCAN query_subject_counts"]
        assert plan(GRAPH_NODES_SQL, (KIND, "", 5)) == [
            "SEARCH subjects USING COVERING INDEX subjects_by_kind_lifecycle_path "
            "(subject_kind=? AND lifecycle=? AND path>?)"
        ]
        assert plan(GRAPH_EDGES_SQL, ("", "", 5)) == [
            "SEARCH claims USING INDEX claims_by_object_subject ((object_path,identity)>(?,?))"
        ]


def test_the_cli_text_names_each_node_and_edge(instance: PlaybillInstance) -> None:
    result = _orient(instance, limit=50, surface="cli")
    text = _render_orient(result.model_dump(mode="json"))
    count = len(_live_subjects(instance))
    assert f"Graph: subjects={count}  this page: nodes={count} edges=3" in text
    assert f"  {KIND}/a -{GOVERNS}-> {KIND}/b  " in text
    assert "(no graph)" not in text
