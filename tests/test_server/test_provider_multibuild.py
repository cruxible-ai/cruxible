"""One kit add moves the kit's and the operator's Lines to a new build, per Line.

A Procedure names no provider build: each Line binds its slots to one exact
build. A kit release shipping a new build of a provider moves every live Line
bound to another build of it, in the same change set, unless ``keep_build``
names that Line; old builds stay installed and runnable.
"""

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
from cruxible_client.contracts.procedure_mandates import parse_procedure_mandate
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.line_specs import line_spec_digest, parse_line_spec
from cruxible_client.contracts.provider_installation import ProviderRetireRequest
from cruxible_client.contracts.triggers import CadenceSchedule
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.support.provider_checkout import ProviderCheckout
from tests.test_server.test_kit_providers import (
    PROCEDURE,
    _add,
    _author,
    _build,
    _client,
    _install,
    _package,
    _tree,
    _worlds,
)
from tests.test_server.test_playbill_line_run_refusals import _accept_members

USER_PROCEDURE = "user.increment"
USER_LINE = "user.tick"
KIT_LINE = "user.kit-tick"
USER_MANDATE = "user-mandate"
KIT_MANDATE = "kit-mandate"


def _author_user_procedure(world) -> None:
    """The operator's own open-slot Procedure, the same shape the kit ships."""

    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.procedures.authored import ProcedureSpec
    from tests.support.provider_installation import LOCAL_CALL_IN, LOCAL_CALL_OUT

    spec = ProcedureSpec.model_validate(
        {
            "procedure": USER_PROCEDURE,
            "input": {"n": "integer"},
            "output": {"n": "integer"},
            "slots": {"increment": {"in": LOCAL_CALL_IN, "out": LOCAL_CALL_OUT, "effect": "pure"}},
            "steps": [{"call": "result", "slot": "increment", "input": {"n": "$input.n"}}],
            "returns": "result",
            "budget": {"wall_clock": "PT30S", "provider_calls": 1},
        }
    )
    intent = (
        world.pb.changes(rationale="The operator's own increment.")
        .procedure(definition=ProcedureInput(kind="procedure", spec=spec))
        .prepare()
    )
    assert not intent.refused, intent.diagnostics
    world.approve(intent.submit(accept="never")._candidate_status.proposal_id)
    world.pb.refresh()


def _mandate(name: str, line: str, caps) -> ProcedureMandateInput:
    from datetime import UTC, datetime

    return ProcedureMandateInput(
        kind="procedure_mandate",
        name=name,
        line_name=line,
        grants="propose",
        resource_ceiling=caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


def _line(world, name: str):
    path = f"lines/{name}.json"
    return parse_line_spec(_tree(world)[path], path=path)


def _bound_build(world, name: str) -> str:
    (binding,) = _line(world, name).bindings.values()
    return binding.build.target.name


def _run(world, line: str):
    from tests.test_server.test_provider_installation import _run_line

    return _run_line(world.instance_id, line)


@pytest.mark.parametrize(
    "keep,concurrent,definition_change",
    [
        ((), False, False),
        ((), True, False),
        ((f"Line:{USER_LINE}",), False, False),
        ((f"Line:{KIT_LINE}",), False, False),
        ((f"Line:{KIT_LINE}",), False, True),
    ],
)
def test_kit_101_to_102_is_one_add_and_keep_build_is_per_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_checkout: ProviderCheckout,
    keep: tuple[str, ...],
    concurrent: bool,
    definition_change: bool,
) -> None:
    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        first = _package(tmp_path / "v101", provider_checkout, name="kit-call", increment=1)
        _install(publisher, first)
        _author(publisher)
        v101 = _build(publisher, first, version="1.0.1")
        old = v101.manifest.providers[0].build
        assert _add(consumer, v101, dry_run=False).status == "accepted"
        consumer.pb.refresh()
        _author_user_procedure(consumer)
        kit_path = f"procedures/{PROCEDURE}.json"
        caps = parse_procedure(_tree(consumer)[kit_path], path=kit_path).definition.hard_caps
        intent = (
            consumer.pb.changes(rationale="Schedule the operator's and the kit's Procedures.")
            .line(
                name=USER_LINE,
                procedure=USER_PROCEDURE,
                bindings={"increment": old},
                parameters={"n": 2},
            )
            .line(
                name=KIT_LINE,
                procedure=PROCEDURE,
                bindings={"increment": old},
                parameters={"n": 2},
            )
            .trigger(
                name="user.clock", line=USER_LINE, schedule=CadenceSchedule(interval_seconds=3600)
            )
            .procedure_mandate(_mandate(USER_MANDATE, USER_LINE, caps))
            .procedure_mandate(_mandate(KIT_MANDATE, KIT_LINE, caps))
            .prepare()
        )
        assert not intent.refused, intent.diagnostics
        consumer.approve(intent.submit(accept="never")._candidate_status.proposal_id)
        consumer.pb.refresh()
        assert _bound_build(consumer, USER_LINE) == _bound_build(consumer, KIT_LINE) == old
        old_provider = _tree(consumer)[f"providers/{old}.json"]
        assert _run(consumer, USER_LINE) == {"n": 3}

        second = _package(tmp_path / "v102", provider_checkout, name="kit-call", increment=2)
        _install(publisher, second)
        if definition_change:
            current = parse_procedure(_tree(publisher)[kit_path], path=kit_path)
            changed = current.model_copy(
                update={
                    "activation_policy": "abort",
                    "lifecycle": ArtifactLifecycle(
                        predecessor_digest=procedure_artifact_digest(current).tagged
                    ),
                }
            )
            _accept_members(
                get_playbill_manager().get(publisher.instance_id),
                publisher.reviewer_key,
                {kit_path: render_procedure(changed)},
                timestamp="2026-10-15T10:00:00.000000Z",
            )
            publisher.pb.refresh()
        v102 = _build(publisher, second, version="1.0.2")
        new = v102.manifest.providers[0].build
        assert old != new
        if concurrent:
            _install(consumer, second)
        before = _tree(consumer)
        preview = _add(consumer, v102, keep_build=keep)
        assert preview.status == "would_propose", preview
        assert _tree(consumer) == before
        # Both Lines bind the old build, so both are re-pin candidates; a kept
        # Line says why it stays.
        assert {
            (item.identity, item.from_build, item.to_build, item.action) for item in preview.repins
        } == {
            (line, old, new, "kept" if line in keep else "successor")
            for line in (f"Line:{USER_LINE}", f"Line:{KIT_LINE}")
        }
        # A moved Line is its own re-pin; a Line over the kit's Procedure successor
        # is carried (disabled) even when its build is kept. Either way each
        # Mandate pinning a changed Line is carried propose-only.
        carried_lines = {f"Line:{KIT_LINE}"} if definition_change else set()
        moved_lines = ({f"Line:{USER_LINE}", f"Line:{KIT_LINE}"} - set(keep)) | carried_lines
        mandates = {f"Line:{USER_LINE}": USER_MANDATE, f"Line:{KIT_LINE}": KIT_MANDATE}
        expected_carries = {
            *carried_lines,
            *(f"ProcedureMandate:{mandates[line]}" for line in moved_lines),
        }
        assert not preview.stops
        assert {item.identity for item in preview.automation_carries} == expected_carries
        assert all(
            item.phase == ("validated" if concurrent else "planned")
            for item in preview.automation_carries
        )
        if concurrent:
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier

            from cruxible_core.errors import RequestRefusedError

            barrier = Barrier(2)

            def add_once():
                barrier.wait(timeout=30)
                try:
                    return _add(
                        consumer,
                        v102,
                        dry_run=False,
                        keep_build=keep,
                        at=preview.coordinate.git_oid,
                    )
                except RequestRefusedError:
                    return None  # The other add moved the preview's accepted coordinate.

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: add_once(), range(2)))
            accepted_results = [
                item for item in outcomes if item is not None and item.status == "accepted"
            ]
            assert len(accepted_results) == 1, outcomes
            added = accepted_results[0]
            assert added.automation_carries == preview.automation_carries
        else:
            added = _add(consumer, v102, dry_run=False, keep_build=keep)
        assert added.status == "accepted", added
        assert {(item.identity, item.action) for item in added.repins} == {
            (item.identity, item.action) for item in preview.repins
        }
        assert not added.stops
        assert {item.identity for item in added.automation_carries} == expected_carries
        assert all(item.phase == "validated" for item in added.automation_carries)
        assert all(
            item.enablement == "disabled"
            for item in added.automation_carries
            if item.kind == "line"
        )
        after = _tree(consumer)
        # Triggers stay byte-identical; a kept Line and its Mandate stay put.
        assert after["triggers/user.clock.json"] == before["triggers/user.clock.json"]
        for line in (USER_LINE, KIT_LINE):
            paths = (f"lines/{line}.json", f"procedure-mandates/{mandates[f'Line:{line}']}.json")
            if f"Line:{line}" not in moved_lines:
                for path in paths:
                    assert after[path] == before[path]
                continue
            line_path, mandate_path = paths
            moved = parse_line_spec(after[line_path], path=line_path)
            previous = parse_line_spec(before[line_path], path=line_path)
            assert moved.lifecycle.predecessor_digest == line_spec_digest(previous).tagged
            assert moved.occurrence_epoch == previous.occurrence_epoch
            procedure_path = f"procedures/{moved.procedure.target.name}.json"
            procedure = parse_procedure(after[procedure_path], path=procedure_path)
            assert moved.procedure.artifact_digest == procedure_artifact_digest(procedure).tagged
            # The Mandate is a same-name propose-only successor pinning the new Line.
            carried = parse_procedure_mandate(after[mandate_path], path=mandate_path)
            assert carried.line.artifact_digest == line_spec_digest(moved).tagged
            assert carried.grants == "propose"
        consumer.pb.refresh()
        for line in (USER_LINE, KIT_LINE):
            kept = f"Line:{line}" in keep
            assert _bound_build(consumer, line) == (old if kept else new)
            assert _run(consumer, line) == {"n": 3 if kept else 4}
        assert _tree(consumer)[f"providers/{old}.json"] == old_provider
        catalog = _client(consumer.http).list_provider_packages(consumer.instance_id)
        assert len([row for row in catalog.installed if row.provider == "kit-call"]) == 2
        retired = _client(consumer.http).retire_provider(
            consumer.instance_id, ProviderRetireRequest(build=old, dry_run=False)
        )
        assert retired.status == ("blocked" if keep else "retired"), retired
        if keep:
            assert retired.pinned_by == keep
        again = _add(consumer, v102, dry_run=False, keep_build=keep)
        assert again.status == "unchanged", again
    finally:
        for _ in opened:
            pass
