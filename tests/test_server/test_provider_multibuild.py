"""One kit add upgrades both its own and operator Procedures without losing old builds."""

from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.inputs import BlueprintInstanceInput
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    parse_procedure,
    procedure_artifact_digest,
)
from cruxible_client.contracts.provider_installation import (
    ProviderMoveRequest,
    ProviderRetireRequest,
)
from cruxible_client.contracts.triggers import CadenceSchedule
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.support.provider_checkout import ProviderCheckout
from tests.test_server.test_kit_providers import (
    BLUEPRINT,
    PROCEDURE,
    _add,
    _author,
    _build,
    _client,
    _install,
    _package,
    _run,
    _tree,
    _worlds,
)
from tests.test_server.test_playbill_line_run_refusals import _accept_members, _line_mandate


def _instantiate(world, name):
    intent = (
        world.pb.changes(rationale="Use the kit's provider.")
        .procedure(
            definition=BlueprintInstanceInput(
                kind="blueprint_instance",
                name=name,
                blueprint=BLUEPRINT,
                bindings={"increment": "kit-call"},
            )
        )
        .prepare()
    )
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    world.approve(submitted._candidate_status.proposal_id)
    world.pb.refresh()


def _pins(world, name):
    path = f"procedures/{name}.json"
    return tuple(
        pin.target.name
        for pin in parse_procedure(_tree(world)[path], path=path).pins
        if pin.role == "provider"
    )


@pytest.mark.parametrize(
    "keep,concurrent",
    [
        ((), False),
        ((), True),
        (("Procedure:user.increment",), False),
        ((f"Procedure:{PROCEDURE}",), False),
    ],
)
def test_kit_101_to_102_is_one_add_and_keep_build_is_per_procedure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_checkout: ProviderCheckout,
    keep: tuple[str, ...],
    concurrent: bool,
) -> None:
    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        first = _package(tmp_path / "v101", provider_checkout, name="kit-call", increment=1)
        _install(publisher, first)
        _author(publisher)
        v101 = _build(publisher, first, version="1.0.1")
        assert _add(consumer, v101, dry_run=False).status == "accepted"
        consumer.pb.refresh()
        _instantiate(consumer, "user.increment")
        intent = (
            consumer.pb.changes(rationale="Schedule the operator's Procedure.")
            .line(name="user.tick", procedure="user.increment", parameters={"n": 2})
            .trigger(
                name="user.clock", line="user.tick", schedule=CadenceSchedule(interval_seconds=3600)
            )
            .prepare()
        )
        assert not intent.refused, intent.diagnostics
        submitted = intent.submit()
        consumer.approve(submitted._candidate_status.proposal_id)
        procedure_path = "procedures/user.increment.json"
        procedure = parse_procedure(_tree(consumer)[procedure_path], path=procedure_path)
        mandate = _line_mandate(
            AcceptedProcedure(
                path=procedure_path,
                procedure=procedure,
                artifact_digest=procedure_artifact_digest(procedure).tagged,
            )
        )
        _accept_members(
            get_playbill_manager().get(consumer.instance_id),
            consumer.reviewer_key,
            {procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)},
            timestamp="2026-10-15T09:00:00.000000Z",
        )
        old = _pins(consumer, "user.increment")
        old_provider = _tree(consumer)[f"providers/{old[0]}.json"]
        assert _run(consumer, "user.increment", 2) == {"n": 3}

        second = _package(tmp_path / "v102", provider_checkout, name="kit-call", increment=2)
        _install(publisher, second)
        moved = _client(publisher.http).move_provider(
            publisher.instance_id, ProviderMoveRequest(provider="kit-call", dry_run=False)
        )
        assert moved.status == "accepted", moved
        publisher.pb.refresh()
        v102 = _build(publisher, second, version="1.0.2")
        assert v101.manifest.providers[0].build != v102.manifest.providers[0].build
        assert [p for p in v101.contents() if p.startswith("provider-interfaces/")] == [
            p for p in v102.contents() if p.startswith("provider-interfaces/")
        ]
        if concurrent:
            _install(consumer, second)
        before = _tree(consumer)
        preview = _add(consumer, v102, keep_build=keep)
        assert preview.status == "would_propose", preview
        assert _tree(consumer) == before
        assert {item.identity for item in preview.repins} == {
            f"Procedure:{PROCEDURE}",
            "Procedure:user.increment",
        }
        expected_carries = (
            set()
            if "Procedure:user.increment" in keep
            else {"Line:user.tick", "ProcedureMandate:served-line-mandate"}
        )
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
        assert {item.identity for item in added.repins} == {
            f"Procedure:{PROCEDURE}",
            "Procedure:user.increment",
        }
        assert not added.stops
        assert {item.identity for item in added.automation_carries} == expected_carries
        assert all(item.phase == "validated" for item in added.automation_carries)
        after = _tree(consumer)
        assert after["triggers/user.clock.json"] == before["triggers/user.clock.json"]
        if not expected_carries:
            for path in ("lines/user.tick.json", "procedure-mandates/served-line-mandate.json"):
                assert after[path] == before[path]
        else:
            from cruxible_client.contracts.procedure_mandates import parse_procedure_mandate_any
            from cruxible_client.contracts.procedures.line_specs import parse_line_spec

            line = parse_line_spec(after["lines/user.tick.json"], path="lines/user.tick.json")
            carried_mandate = parse_procedure_mandate_any(
                after["procedure-mandates/served-line-mandate.json"],
                path="procedure-mandates/served-line-mandate.json",
            )
            new_procedure = parse_procedure(after[procedure_path], path=procedure_path)
            assert line.procedure.artifact_digest == procedure_artifact_digest(new_procedure).tagged
            assert carried_mandate.procedure == line.procedure
            assert carried_mandate.grants == "propose"
            assert line.occurrence_epoch == 1
            assert any(
                item.kind == "line" and item.enablement == "disabled"
                for item in added.automation_carries
            )
        consumer.pb.refresh()
        for name in (PROCEDURE, "user.increment"):
            kept = f"Procedure:{name}" in keep
            assert _pins(consumer, name) == (old if kept else (v102.manifest.providers[0].build,))
            assert _run(consumer, name, 2) == {"n": 3 if kept else 4}
        assert _tree(consumer)[f"providers/{old[0]}.json"] == old_provider
        catalog = _client(consumer.http).list_provider_packages(consumer.instance_id)
        assert len([row for row in catalog.installed if row.provider == "kit-call"]) == 2
        retired = _client(consumer.http).retire_provider(
            consumer.instance_id, ProviderRetireRequest(build=old[0], dry_run=False)
        )
        assert retired.status == ("blocked" if keep else "retired"), retired
        if keep:
            assert retired.pinned_by == keep
        again = _add(consumer, v102, dry_run=False, keep_build=keep)
        assert again.status == "unchanged", again
    finally:
        for _ in opened:
            pass
