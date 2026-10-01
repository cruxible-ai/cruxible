"""Equivalence over a varied history: every (base, head) delta rebuilds the full floor exactly.

The history mixes Claim changes, retirements, contenders, Subject-valued edges
moved in place (fan-in to old and new targets), a new ClaimType, long text
that appears and goes away, evidence-cited Claims and a Document revised in
place. For every pair of generations the delta applied to the base floor is
byte-identical to the full floor at the head; the deltas are the same whether
the index was cold, warm or advanced in one coalesced step; and applying one
twice changes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.floor_apply import apply_floor_delta
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    document_path,
    render_document,
)
from cruxible_client.contracts.floor import PlaybillFloorDeltaV1
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor_delta import (
    advance_floor_index,
    service_playbill_floor_delta,
)
from tests.core_support._write_support import KIND, report_evidence, seed_write_surface
from tests.test_floor.test_floor_index import _set, _write, accept_edit, add_lead_field

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"


def _document(instance: PlaybillInstance, body: bytes, previous: DocumentShell | None) -> Any:
    shell = DocumentShell(
        identity="document:design-note",
        document_kind="design",
        title="Design note",
        media_type="text/markdown",
        body_digest=instance.store_document_body(body).digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1 if previous is None else 2),
        **({} if previous is None else {"predecessor_digest": document_digest(previous).tagged}),
    )
    accept_edit(
        instance,
        f"design-note-{1 if previous is None else 2}",
        {document_path("design-note"): render_document(shell)},
    )
    return shell


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("floor-delta-properties")
    (root / "instance").mkdir()
    instance, _owner = seed_write_surface(root / "instance")
    workspace = root / "workspace"
    _write(instance, _set(WI1, "status", "ready"), _set(WI1, "title", "One"))
    first = _write(instance, _set(WI2, "status", "ready"))
    _write(instance, _set(WI2, "status", "blocked"))
    _write(instance, _set(WI2, "status", "done", contend=True), at=first.coordinate.git_oid)
    add_lead_field(instance)
    _write(instance, _set(WI1, "lead", WI2), _set(WI3, "lead", WI2))
    _write(instance, _set(WI3, "ruling", "".join(f"Clause {i}.\n" for i in range(60))))
    _write(instance, _set(WI1, "measured", 3, evidence=report_evidence(workspace, "Count: 3")))
    note = _document(instance, b"# Note\n\nFirst.\n", None)
    _write(
        instance,
        _set(WI1, "lead", WI3),
        {"op": "add", "subject": WI2, "field": "governs", "value": WI1},
    )
    _document(instance, b"# Note\n\nSecond.\n", note)
    _write(instance, _set(WI3, "ruling", "Short now.\n"))
    _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "measured"}})
    _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "title"}})
    return {"instance": instance, "head": len(instance.accepted_history()) - 1}


def _coordinate(instance: PlaybillInstance, generation: int) -> AcceptedCoordinate:
    with instance.accepted_history_reader() as history:
        oid = history.generation(generation).git_oid
    return AcceptedCoordinate.from_internal(instance.coordinate_for_oid(oid))


def _delta(
    instance: PlaybillInstance, head: int, base: int | None, renderer: str | None
) -> PlaybillFloorDeltaV1:
    return service_playbill_floor_delta(
        instance,
        head=_coordinate(instance, head),
        base_generation=base,
        base_renderer=renderer,
    )


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_every_delta_rebuilds_the_full_floor_byte_for_byte(
    world: dict[str, Any], tmp_path: Path
) -> None:
    instance: PlaybillInstance = world["instance"]
    head = world["head"]
    fulls: dict[int, dict[str, bytes]] = {}
    renderer = _delta(instance, head, None, None).renderer
    for generation in range(head + 1):
        directory = tmp_path / f"full-{generation}"
        apply_floor_delta(directory, _delta(instance, generation, None, None))
        fulls[generation] = _tree(directory)
    # Every file family moved somewhere in this history.
    paths = set().union(*fulls.values())
    assert any(path.endswith(".txt") for path in paths)
    assert any(b"incoming:" in content for tree in fulls.values() for content in tree.values())
    assert any(b"Document:design-note" in tree.get("sources/INDEX", b"") for tree in fulls.values())
    for target in range(head + 1):
        for base in range(target + 1):
            directory = tmp_path / f"apply-{base}-{target}"
            apply_floor_delta(directory, _delta(instance, base, None, None))
            delta = _delta(instance, target, base, renderer)
            first = apply_floor_delta(directory, delta)
            assert first.status in {"applied", "unchanged"}, (base, target)
            assert _tree(directory) == fulls[target], (base, target)
            again = apply_floor_delta(directory, delta)
            assert again.status == "unchanged" and _tree(directory) == fulls[target]


def test_deltas_are_the_same_cold_warm_and_coalesced(world: dict[str, Any]) -> None:
    instance: PlaybillInstance = world["instance"]
    head = world["head"]
    renderer = _delta(instance, head, None, None).renderer
    pairs = [(base, target) for target in range(head + 1) for base in range(target + 1)]

    def every() -> list[PlaybillFloorDeltaV1]:
        return [_delta(instance, target, base, renderer) for base, target in pairs]

    warm = every()
    instance.floor_current_memo.clear()
    cold = every()
    instance.floor_current_memo.clear()
    advance_floor_index(instance, _coordinate(instance, 2))
    advance_floor_index(instance)
    coalesced = every()
    assert warm == cold == coalesced
