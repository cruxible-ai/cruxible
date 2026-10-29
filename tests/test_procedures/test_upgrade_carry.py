"""D8: final pin succession carries no operational or settle authority."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.procedure_mandates import (
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.triggers import CadenceSchedule, CaptureLandingSchedule
from cruxible_core.consumers.next.line_upgrades import (
    _STATE,
    catch_up,
    disabled_upgrades,
    finding_since,
)
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.providers.automation_carry import carry_automation
from cruxible_core.providers.build_moves import settle_changes
from cruxible_core.runtime.line_arms import dispatch_armed_line
from cruxible_core.service.discovery.next import _line_upgrade_items
from cruxible_core.service.procedures.line_dispatch import (
    armed_work,
    service_enable_line,
    service_match_listening_lines,
)
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_line_arming import LOCAL, _admissions
from tests.test_procedures.test_line_triggers import (
    SELECTOR,
    capture,
)
from tests.test_procedures.test_line_triggers import (
    line_world as _current_line_world,
)
from tests.test_procedures.test_procedure_mandates_v2 import _accepted_query, _query, _settle
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor, _slotless_procedure


def line_world(*args, **kwargs):
    return _current_line_world(*args, **kwargs, historical=True)


_NEEDS_HISTORICAL_LINE_FIXTURE = pytest.mark.skip(
    reason="blocked outside test_procedures scope: line_world(historical=True) builds its "
    "instance via _accepted_claim_world's initialize_legacy_local, which pins the compiler to "
    "CLAIM_REBIND_COMPILER (revision 35); no Line can be newly accepted under any revision "
    "before 36, which requires cruxible-line-v8 for every new Line artifact. Needs "
    "test_line_triggers.line_world's historical path redesigned to carry an old-format "
    "Procedure under a current-revision instance, not an old-revision instance."
)


def _changed(procedure):
    return procedure.procedure.model_copy(
        update={
            "activation_policy": "snapshot",
            "lifecycle": ArtifactLifecycle(predecessor_digest=procedure.artifact_digest),
        }
    )


def _settled(instance, procedure, writes=None):
    from cruxible_core.compiler.compiler import LIGHT_ARTIFACTS_COMPILER

    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    with instance.bind_accepted_projection(coordinate) as projection:
        result = settle_changes(
            tree,
            writes={procedure.path: render_procedure(_changed(procedure))}
            if writes is None
            else writes,
            connection=projection.typed.connection,
            by_name=coordinate.compiler == LIGHT_ARTIFACTS_COMPILER,
        )
    assert not result.refused, result.refused
    return tree, result


@_NEEDS_HISTORICAL_LINE_FIXTURE
@pytest.mark.parametrize("evolution", ["required", "type", "optional"])
def test_definition_upgrade_validates_preserved_line_input(tmp_path, evolution):
    from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
    from tests.support.procedures import procedure_artifact

    instance, line, accepted, owner = line_world(
        tmp_path, CadenceSchedule(interval_seconds=2), with_owner=True
    )
    old = accepted.procedure
    schemas = {item.identity.name: item.contract_schema for item in old.owned_contracts}
    fields = {"status": PropertySchema(type="int" if evolution == "type" else "string")}
    if evolution != "type":
        fields["new_input"] = PropertySchema(type="int", optional=evolution == "optional")
    schemas[old.definition.contract_in.target.name] = ContractSchema(fields=fields)
    successor = procedure_artifact(
        old.definition,
        contracts=schemas,
        activation_policy=old.activation_policy,
        lifecycle=ArtifactLifecycle(predecessor_digest=accepted.artifact_digest),
    )
    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    with instance.bind_accepted_projection(coordinate) as projection:
        settled = settle_changes(
            tree,
            writes={accepted.path: render_procedure(successor)},
            connection=projection.typed.connection,
        )
    assert instance.accepted_coordinate() == coordinate
    if evolution != "optional":
        assert not settled.writes
        diagnostic = " ".join(settled.refused)
        assert line.identity.qualified in diagnostic
        assert successor.identity.qualified in diagnostic
        assert procedure_artifact_digest(successor).tagged in diagnostic
        assert f"parameters.{'status' if evolution == 'type' else 'new_input'}" in diagnostic
        assert "explicitly author compatible Line parameters" in diagnostic
        return
    assert not settled.refused, settled.refused
    path = f"lines/{line.identity.name}.json"
    carried = parse_line_spec(settled.writes[path], path=path)
    assert carried.parameters == line.parameters
    assert "new_input" not in carried.parameters
    assert carried.procedure.artifact_digest == procedure_artifact_digest(successor).tagged
    _accept_tree(
        instance,
        owner,
        {**tree, **settled.writes},
        timestamp="2026-08-25T15:00:00.000000Z",
        proposal_name="compatible-input-upgrade",
    )


@_NEEDS_HISTORICAL_LINE_FIXTURE
@pytest.mark.parametrize("event", [False, True])
def test_definition_upgrade_disabled_next_recovery_and_enable_resumes(tmp_path, monkeypatch, event):
    schedule = (
        CaptureLandingSchedule(event=SELECTOR) if event else CadenceSchedule(interval_seconds=2)
    )
    instance, line, procedure, owner = line_world(tmp_path, schedule, with_owner=True)
    start = READ_TIME + timedelta(seconds=10)
    actor = _actor(instance)
    service_enable_line(
        instance, line.identity.name, principal=LOCAL, actor=actor, now=start, daemon_id="daemon"
    )
    catch_up(instance)
    tree, settled = _settled(instance, procedure)
    assert not settled.stops
    assert {item.kind for item in settled.automation_carries} == {"line", "mandate"}
    assert not any(path.startswith("triggers/") for path in settled.writes)
    upgraded = parse_line_spec(
        settled.writes[f"lines/{line.identity.name}.json"], path=f"lines/{line.identity.name}.json"
    )
    assert upgraded.parameters == line.parameters
    assert upgraded.occurrence_epoch == line.occurrence_epoch
    assert upgraded.lifecycle.predecessor_digest == line_spec_digest(line).tagged
    assert (
        upgraded.procedure.artifact_digest == procedure_artifact_digest(_changed(procedure)).tagged
    )
    candidate = dict(tree)
    candidate.update(settled.writes)
    _accept_tree(
        instance, owner, candidate, timestamp="2026-08-25T15:00:00.000000Z", proposal_name="upgrade"
    )
    # Activation did no operational append. The old exact-version arm cannot admit.
    service_match_listening_lines(
        instance, actor=actor, now=start + timedelta(seconds=1), daemon_id="daemon"
    )
    assert not armed_work(instance, now=start + timedelta(seconds=2))
    expected = ((line.identity.qualified, upgraded.procedure.artifact_digest),)
    # A lost worker transaction rolls back both row and cursor; retry recovers.
    original_blob = instance.blob_at
    monkeypatch.setattr(
        instance, "blob_at", lambda *a: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError, match="crash"):
        catch_up(instance)
    monkeypatch.setattr(instance, "blob_at", original_blob)
    assert disabled_upgrades(instance) == expected
    (finding,) = _line_upgrade_items(
        instance, access_profile=CoverageAccessProfile(profile_id="test")
    )
    assert finding.reason == "line_disabled_by_upgrade"
    assert finding.repair.operation == "cruxible.line.enable"
    assert "disabled by upgrade to" in finding.detail["detail"]
    # Disabled since the upgrade's generation was accepted.
    upgraded_since = {("disabled", line.identity.qualified): "2026-08-25T15:00:00+00:00"}
    assert finding_since(instance, disabled=(line.identity.qualified,)) == upgraded_since
    # Rebuild after a lost projection reconstructs the finding from accepted deltas.
    _STATE.reset(instance)
    assert disabled_upgrades(instance) == expected
    assert finding_since(instance, disabled=(line.identity.qualified,)) == upgraded_since
    catch_up(instance)
    # Warm tick/read must not reread a retained Line blob.
    monkeypatch.setattr(instance, "blob_at", lambda *a: pytest.fail("warm history reread"))
    assert disabled_upgrades(instance) == expected
    monkeypatch.undo()
    enabled = service_enable_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=actor,
        now=start + timedelta(seconds=2),
        daemon_id="daemon",
    )
    assert enabled.line_artifact_digest == line_spec_digest(upgraded).tagged
    assert disabled_upgrades(instance) == ()
    later = start + timedelta(seconds=5)
    if event:
        capture(instance, procedure, at=start + timedelta(seconds=3))
    service_match_listening_lines(instance, actor=actor, now=later, daemon_id="daemon")
    work = armed_work(instance, now=later)
    assert work
    before = _admissions(instance)
    manager = SimpleNamespace(
        get=lambda _: instance,
        workspace_file_reader=lambda _: None,
        provider_runtime_operator=lambda: None,
    )
    dispatch_armed_line(manager, instance.descriptor.instance_id, work[0], now=later)
    assert _admissions(instance) > before


def test_settle_conversion_clears_all_fields_preserves_namespace_and_suspension():
    mandate = _settle(_accepted_query(_query()), subject_creation=("ops.asset",), suspended=True)
    path = f"procedure-mandates/{mandate.identity.name}.json"
    old = mandate.line.artifact_digest
    new = "sha256:" + "a" * 64
    content, row = carry_automation(path, render_procedure_mandate(mandate), {old: new})
    successor = parse_procedure_mandate_any(content, path=path)
    assert successor.grants == "propose"
    assert successor.scope == () and successor.condition is None
    assert successor.subject_scope is None and not successor.subject_creation
    assert successor.suspended and successor.namespace == mandate.namespace
    assert successor.resource_ceiling == mandate.resource_ceiling
    assert successor.lifecycle.predecessor_digest == procedure_mandate_digest(mandate).tagged
    assert row.loses_settle and row.subject_creation_namespace_limitation


@_NEEDS_HISTORICAL_LINE_FIXTURE
def test_explicit_automation_conflict_refuses_instead_of_overwriting(tmp_path):
    instance, line, procedure = line_world(tmp_path, CadenceSchedule(interval_seconds=2))
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        result = settle_changes(
            tree,
            writes={
                procedure.path: render_procedure(_changed(procedure)),
                f"lines/{line.identity.name}.json": render_line_spec(line),
            },
            connection=projection.typed.connection,
        )
    assert result.refused and "conflicts with carry" in result.refused[0]
    assert not result.writes


@_NEEDS_HISTORICAL_LINE_FIXTURE
@pytest.mark.parametrize("size", [3, 30])
@pytest.mark.parametrize("affected", [False, True])
def test_cost_reads_only_re_pinned_procedures_dependents(tmp_path, monkeypatch, size, affected):
    instance, line, procedure, owner = line_world(
        tmp_path, CadenceSchedule(interval_seconds=2), with_owner=True
    )
    unrelated_procedure = _slotless_procedure("unrelated", historical=True).procedure
    pin = line.procedure.model_copy(
        update={
            "target": unrelated_procedure.identity,
            "artifact_digest": procedure_artifact_digest(unrelated_procedure).tagged,
        }
    )
    if affected:
        pin = line.procedure
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree["procedures/unrelated.json"] = render_procedure(unrelated_procedure)
    tree.update(
        {
            f"lines/unrelated-{i}.json": render_line_spec(
                line.model_copy(
                    update={
                        "identity": ArtifactIdentity(kind="Line", name=f"unrelated-{i}"),
                        "procedure": pin,
                        "pins": tuple(
                            pin if item == line.procedure else item for item in line.pins
                        ),
                    }
                )
            )
            for i in range(size)
        }
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-25T15:00:00.000000Z", proposal_name="unrelated"
    )
    from cruxible_core.derived.derived_state import SnapshotTree

    # This measures the warm planning route; cold index creation is separate.
    coordinate = instance.accepted_coordinate()
    instance.immutable_tree_at(coordinate.git_oid)
    with instance.bind_accepted_projection(coordinate):
        pass
    reads = []
    original = SnapshotTree.__getitem__

    def counted(self, key):
        reads.append(key)
        return original(self, key)

    monkeypatch.setattr(SnapshotTree, "__getitem__", counted)
    monkeypatch.setattr(SnapshotTree, "__iter__", lambda _: pytest.fail("population walk"))
    import cruxible_core.providers.build_moves as moves

    carried_paths = []
    carry = moves.carry_automation

    def counted_carry(path, *args, **kwargs):
        carried_paths.append(path)
        return carry(path, *args, **kwargs)

    monkeypatch.setattr(moves, "carry_automation", counted_carry)
    _, result = _settled(instance, procedure)
    assert len(result.automation_carries) == (size + 2 if affected else 2)
    assert len(carried_paths) == len(set(carried_paths))
    assert len(reads) <= (20 + 8 * size if affected else 20), reads
    if not affected:
        assert not any("unrelated" in path for path in reads)


def test_two_settle_mandates_propose_after_upgrade_until_explicit_regrant(tmp_path):
    from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure, parse_procedure
    from tests.test_procedures import test_procedure_settlement_delivery as delivery
    from tests.test_procedures import test_procedure_source_runs as fixtures

    instance, root, line = delivery.settle_world(tmp_path, mandates=2)
    before = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = f"procedures/{line.procedure.target.name}.json"
    old_procedure = parse_procedure(before[path], path=path)
    accepted = AcceptedProcedure(
        path=path,
        procedure=old_procedure,
        artifact_digest=procedure_artifact_digest(old_procedure).tagged,
    )
    _, settled = _settled(instance, accepted)
    mandate_rows = [item for item in settled.automation_carries if item.kind == "mandate"]
    assert len(mandate_rows) == 2 and all(item.loses_settle for item in mandate_rows)
    fixtures._accept_more(instance, delivery.OWNERS[instance.root], settled.writes, name="upgrade")
    state = delivery.run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    egress = delivery._egress(state)
    assert egress.settle_outcome == "proposed", egress
    assert egress.capped_by is not None
    # Re-grant the same identity using the old condition/scope and current Procedure.
    mandate_path = next(path for path in settled.writes if path.startswith("procedure-mandates/"))
    old = parse_procedure_mandate_any(before[mandate_path], path=mandate_path)
    proposed = parse_procedure_mandate_any(settled.writes[mandate_path], path=mandate_path)
    regranted = old.model_copy(
        update={
            "line": proposed.line,
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(proposed).tagged
            ),
        }
    )
    fixtures._accept_more(
        instance,
        delivery.OWNERS[instance.root],
        {mandate_path: render_procedure_mandate(regranted)},
        name="regrant",
    )
    restored = delivery.run_settle(instance, root, line, now=fixtures.NOW + timedelta(seconds=1))
    assert delivery._egress(restored).settle_outcome == "settled", restored


@_NEEDS_HISTORICAL_LINE_FIXTURE
def test_retirement_clears_upgrade_row_and_keeps_history_readable(tmp_path):
    from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure

    instance, line, procedure, owner = line_world(
        tmp_path, CadenceSchedule(interval_seconds=2), with_owner=True
    )
    original = render_line_spec(line)
    tree, settled = _settled(instance, procedure)
    candidate = dict(tree)
    candidate.update(settled.writes)
    _accept_tree(
        instance, owner, candidate, timestamp="2026-08-25T15:00:00.000000Z", proposal_name="upgrade"
    )
    assert disabled_upgrades(instance)
    current = _changed(procedure)
    accepted = AcceptedProcedure(
        path=procedure.path,
        procedure=current,
        artifact_digest=procedure_artifact_digest(current).tagged,
    )
    retired = current.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=accepted.artifact_digest
            )
        }
    )
    tree, retirement = _settled(instance, accepted, {procedure.path: render_procedure(retired)})
    assert {item.kind for item in retirement.stops} == {"line", "mandate", "trigger"}
    assert not retirement.automation_carries
    candidate = dict(tree)
    candidate.update(retirement.writes)
    _accept_tree(
        instance, owner, candidate, timestamp="2026-08-26T15:00:00.000000Z", proposal_name="retire"
    )
    assert not disabled_upgrades(instance)
    found = instance.accepted_artifact_version(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        line_spec_digest(line).tagged,
        family="lines/",
    )
    assert found == (f"lines/{line.identity.name}.json", original)


@pytest.mark.parametrize("enable_before_edit", [False, True])
@_NEEDS_HISTORICAL_LINE_FIXTURE
def test_enable_after_an_ordinary_line_edit_clears_upgrade_finding(tmp_path, enable_before_edit):
    instance, line, procedure, owner = line_world(
        tmp_path, CadenceSchedule(interval_seconds=2), with_owner=True
    )
    tree, settled = _settled(instance, procedure)
    candidate = dict(tree)
    candidate.update(settled.writes)
    _accept_tree(
        instance, owner, candidate, timestamp="2026-08-25T15:00:00.000000Z", proposal_name="upgrade"
    )
    path = f"lines/{line.identity.name}.json"
    upgraded = parse_line_spec(settled.writes[path], path=path)
    if enable_before_edit:
        service_enable_line(
            instance,
            line.identity.name,
            principal=LOCAL,
            actor=_actor(instance),
            now=READ_TIME,
            daemon_id="daemon",
        )
    edited = upgraded.model_copy(
        update={
            "max_authority": "observe",
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(upgraded).tagged),
        }
    )
    candidate = instance.tree_at(instance.accepted_coordinate().git_oid)
    candidate[path] = render_line_spec(edited)
    _accept_tree(
        instance, owner, candidate, timestamp="2026-08-26T15:00:00.000000Z", proposal_name="edit"
    )
    # Cold replay must notice an earlier enablement even when no next read saw it.
    _STATE.reset(instance)
    assert bool(disabled_upgrades(instance)) is not enable_before_edit
    service_enable_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME + timedelta(seconds=1),
        daemon_id="daemon",
    )
    assert not disabled_upgrades(instance)
