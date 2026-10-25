"""Kit planning leaves compatible name references and explicitly stops retire-with."""

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.named_references import name_line, name_trigger
from cruxible_client.contracts.procedures.artifacts import (
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.procedures.models import ConstantNode
from cruxible_client.contracts.procedures.names import name_procedure
from cruxible_client.contracts.triggers import CadenceSchedule, render_trigger, trigger_path
from cruxible_core.providers.build_moves import settle_changes
from cruxible_core.service.kits import _planned_automation
from tests.core_support._identity_support import accept_tree
from tests.core_support._support import initialize_local
from tests.support.lines import line_trigger
from tests.test_procedures.test_line_specs import _line
from tests.test_procedures.test_procedure_artifacts import _artifact, _definition


def test_named_line_is_unchanged_on_compatible_revision_and_stopped_only_on_retire_with(tmp_path):
    definition = _definition()
    procedure = name_procedure(
        _artifact(
            definition.model_copy(
                update={
                    "nodes": (
                        ConstantNode(
                            node_id="result",
                            contract_out=definition.contract_out,
                            fields={},
                            as_="result",
                        ),
                    )
                }
            )
        )
    )
    line = name_line(_line(requested_rung=1)[0]).model_copy(update={"parameters": {}})
    trigger = name_trigger(
        line_trigger(
            "named-clock", line=line.identity.name, schedule=CadenceSchedule(interval_seconds=60)
        )
    )
    ppath = "procedures/triage.json"
    lpath, tpath = line_spec_path(line.identity.name), trigger_path(trigger.identity.name)
    instance, _ = initialize_local(tmp_path)
    accept_tree(
        instance,
        {
            ppath: render_procedure(procedure),
            lpath: render_line_spec(line),
            tpath: render_trigger(trigger),
        },
        name="named-kit-world",
    )
    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    with instance.bind_accepted_projection(coordinate) as projection:
        for retired in (False, True):
            successor = procedure.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(
                        state="retired" if retired else "live",
                        predecessor_digest=procedure_artifact_digest(procedure).tagged,
                    ),
                }
            )
            writes = {ppath: render_procedure(successor)}
            retire_with = {procedure.identity.qualified} if retired else set()
            carries, stops = _planned_automation(
                projection.typed.connection,
                tree,
                (procedure.identity,),
                writes,
                retire_with,
                by_name=True,
            )
            assert carries == ()
            assert {stop.identity for stop in stops} == (
                {line.identity.qualified, trigger.identity.qualified} if retired else set()
            )
            settled = settle_changes(
                tree,
                writes=writes,
                connection=projection.typed.connection,
                retire_with=frozenset(retire_with),
                by_name=True,
            )
            assert settled.refused == []
            assert {stop.identity for stop in settled.stops} == {stop.identity for stop in stops}
            if not retired:
                assert set(settled.writes) == {ppath}
                assert tree[lpath] == render_line_spec(line)
                assert tree[tpath] == render_trigger(trigger)
