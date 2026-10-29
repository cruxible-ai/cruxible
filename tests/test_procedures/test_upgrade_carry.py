"""D8: final pin succession carries no operational or settle authority."""

from datetime import timedelta

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.procedure_mandates import (
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_core.providers.automation_carry import carry_automation
from cruxible_core.providers.build_moves import settle_changes
from tests.test_procedures.test_procedure_mandates_v2 import _accepted_query, _query, _settle


def _changed(procedure):
    return procedure.procedure.model_copy(
        update={
            "activation_policy": "snapshot",
            "lifecycle": ArtifactLifecycle(predecessor_digest=procedure.artifact_digest),
        }
    )


def _settled(instance, procedure, writes=None):
    from cruxible_core.compiler.compiler import CURRENT_COMPILER

    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    with instance.bind_accepted_projection(coordinate) as projection:
        result = settle_changes(
            tree,
            writes={procedure.path: render_procedure(_changed(procedure))}
            if writes is None
            else writes,
            connection=projection.typed.connection,
            by_name=coordinate.compiler == CURRENT_COMPILER,
        )
    assert not result.refused, result.refused
    return tree, result


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
