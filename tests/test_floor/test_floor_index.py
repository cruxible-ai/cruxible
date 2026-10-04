"""The floor index: ledger-derived stamps, and incremental renders equal to cold ones."""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.write import PlaybillWriteRequest
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.floor.floor_index import (
    FloorRender,
    advance_floor_index,
    floor_render_from,
)
from tests.core_support._write_support import (
    KIND,
    _claim_type,
    caller,
    seed_write_surface,
)

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"


def _write(instance: PlaybillInstance, *changes: dict[str, Any], **options: Any) -> Any:
    request = PlaybillWriteRequest.model_validate(
        {"because": "The writer checked it.", "changes": list(changes), **options}
    )
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return outcome


def _set(subject: str, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "set", "subject": subject, "field": field, "value": value, **extra}


def accept_edit(instance: PlaybillInstance, name: str, edits: dict[str, bytes]) -> None:
    """Accept one direct tree edit through the ordinary proposal and activation."""

    base = instance.accepted_coordinate()
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**instance.tree_at(base.git_oid), **edits},
        timestamp="2026-09-30T12:00:00.000000Z",
    )
    assert proposed.candidate is not None, [
        (item.code, item.message) for item in proposed.evaluation.diagnostics
    ]
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"


LEAD = _claim_type("lead", object_kind="subject", roles=("normative",), object_kinds=(KIND,))


def add_lead_field(instance: PlaybillInstance) -> None:
    """A single-valued Subject field, so set moves one edge in place."""

    accept_edit(instance, "lead-field", {claim_type_path(LEAD.predicate): render_claim_type(LEAD)})


def _cold(instance: PlaybillInstance, generation: int | None = None) -> FloorRender:
    instance.floor_current_memo.clear()
    if generation is None:
        return advance_floor_index(instance)
    with instance.accepted_history_reader() as history:
        oid = history.generation(generation).git_oid
    return advance_floor_index(instance, instance.coordinate_for_oid(oid))


def _bytes(render: FloorRender) -> dict[str, tuple[bytes, int]]:
    return dict(render.files)


def test_incremental_renders_equal_cold_ones_at_every_generation(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    steps = [
        (_set(WI1, "status", "ready"),),
        (_set(WI2, "title", "Two"), _set(WI1, "ruling", "Text.\nMore text.\n")),
        (_set(WI1, "status", "blocked"),),
        ({"op": "add", "subject": WI1, "field": "governs", "value": WI2},),
        (_set(WI3, "ruling", "".join(f"Clause {i}.\n" for i in range(60))),),
        (_set(WI3, "ruling", "Short now.\n"),),
    ]
    warm: list[FloorRender] = []
    advance_floor_index(instance)
    for changes in steps:
        _write(instance, *changes)
        warm.append(advance_floor_index(instance))
    for render in warm:
        assert _bytes(render) == _bytes(_cold(instance, render.generation)), render.generation
    # A coalesced advance over every step at once is the same floor too.
    instance.floor_current_memo.clear()
    first = _cold(instance, warm[0].generation)
    assert first.generation < warm[-1].generation
    assert _bytes(advance_floor_index(instance)) == _bytes(warm[-1])


def test_a_stamp_moves_exactly_when_a_files_inputs_do(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"), _set(WI2, "status", "done"))
    before = advance_floor_index(instance)
    _write(instance, _set(WI2, "status", "blocked"))
    after = advance_floor_index(instance)
    for path, (content, changed_at) in after.files.items():
        old = before.files.get(path)
        if changed_at <= before.generation:
            assert old == (content, changed_at), path
        else:
            assert old is None or old[0] != content, path
    assert after.files[f"current/{WI2}.yaml"][1] == after.generation
    assert after.files[f"current/{WI1}.yaml"][1] < after.generation


def test_any_generation_renders_from_the_index_in_either_direction(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"))
    _write(instance, _set(WI2, "title", "Two"))
    _write(instance, _set(WI1, "status", "done"), _set(WI3, "title", "Three"))
    head = advance_floor_index(instance)
    for generation in range(head.generation + 1):
        backwards = floor_render_from(instance, head, generation)
        assert _bytes(backwards) == _bytes(_cold(instance, generation)), generation
        forwards = floor_render_from(instance, backwards, head.generation)
        assert _bytes(forwards) == _bytes(head)


def test_a_moved_edge_restamps_its_old_and_new_targets(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    add_lead_field(instance)
    _write(instance, _set(WI1, "lead", WI2))
    first = advance_floor_index(instance)
    assert f"lead <- {WI1}" in first.files[f"current/{WI2}.yaml"][0].decode()
    _write(instance, _set(WI1, "lead", WI3))
    moved = advance_floor_index(instance)
    old_target = moved.files[f"current/{WI2}.yaml"]
    new_target = moved.files[f"current/{WI3}.yaml"]
    assert "incoming:" not in old_target[0].decode()
    assert f"lead <- {WI1}" in new_target[0].decode()
    assert old_target[1] == new_target[1] == moved.generation
    assert _bytes(moved) == _bytes(_cold(instance))
    # Back at the first generation, the edge points where it did.
    back = floor_render_from(instance, moved, first.generation)
    assert _bytes(back) == _bytes(first)


def test_a_new_claim_type_restamps_no_subject_that_does_not_show_it(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "title", "One"), _set(WI2, "status", "done"))
    before = advance_floor_index(instance)
    add_lead_field(instance)
    after = advance_floor_index(instance)
    assert after.generation == before.generation + 1
    assert {
        path: item for path, item in after.files.items() if not path.startswith("changes/")
    } == {path: item for path, item in before.files.items() if not path.startswith("changes/")}
    assert _bytes(after) == _bytes(_cold(instance))


def test_a_field_stamp_follows_its_claim_type_and_its_short_name_shadow() -> None:
    from types import SimpleNamespace

    from cruxible_core.service.floor.floor_index import _Stamps

    inputs: Any = SimpleNamespace(
        latest={
            "claim-types/project.work_item/a.json": 3,
            "claim-types/a/b.json": 7,
            "claim-types/project.work_item/c.json": 2,
        }
    )
    stamps = _Stamps(inputs, frozenset())
    # project.work_item.a.b shows as "a.b" unless the predicate a.b is live: both move it.
    assert stamps.name("project.work_item.a.b", "project.work_item") == 7
    assert stamps.name("project.work_item.c", "project.work_item") == 2
    # A field of another kind keeps its full name and only its own type moves it.
    assert stamps.name("project.work_item.c", "other.kind") == 2
