"""A build move changes each affected Line's fit, leaving its Procedure intact."""

from collections.abc import Mapping

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.named_references import name_provider
from cruxible_client.contracts.procedures.artifacts import NamedProcedureArtifact, render_procedure
from cruxible_client.contracts.procedures.authored import ProcedureSpec, lower_procedure_spec
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import (
    NamedLineSpec,
    line_spec_digest,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import (
    provider_expected_implementation_records,
    provider_path,
    render_provider,
)
from cruxible_core.providers.build_moves import Mover, _rebound_line, build_move, settle_changes
from cruxible_core.providers.builds import indexed_move_pinners
from tests.core_support._identity_support import accept_tree
from tests.core_support._p2b1_support import provider_v2
from tests.core_support._support import initialize_local
from tests.support.provider_builds import as_build
from tests.test_procedures.test_slotted_procedures import slotted


def _other(interface):
    template = provider_v2(registration=interface)
    runtime = template.runtime_artifact.model_copy(
        update={
            "distribution": template.runtime_artifact.distribution.model_copy(
                update={"sha256": "sha256:" + "f" * 64}
            )
        }
    )
    return name_provider(
        as_build(
            template.model_copy(
                update={
                    "runtime_artifact": runtime,
                    "implementations": provider_expected_implementation_records(runtime),
                }
            )
        )
    )


def _unrelated(name):
    payload = lower_authoring_input(
        lower_procedure_spec(
            ProcedureSpec.model_validate(
                {
                    "procedure": name,
                    "input": {},
                    "steps": [{"project": "result", "fields": {}}],
                    "returns": "result",
                }
            )
        )
    )
    definition = NamedProcedureDefinition.model_validate(payload.definition)
    return NamedProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=payload.owned_contracts,
        activation_policy="snapshot",
    )


@pytest.mark.parametrize("unrelated_count", [10, 100])
@pytest.mark.parametrize("explicit", [False, True])
def test_build_move_rebinds_only_the_selected_line_by_fit(tmp_path, unrelated_count, explicit):
    _, procedure, provider, interface = slotted()
    line = NamedLineSpec(
        identity=ArtifactIdentity(kind="Line", name="test.slotted"),
        occurrence_epoch=1,
        procedure=ArtifactNameRef(target=procedure.procedure.identity),
        bindings={"compute": ArtifactNameRef(target=provider.provider.identity)}
        if explicit
        else {},
        parameters={"value": 1},
        max_authority="observe",
        budgets={
            "max_wall_clock_microseconds": 1000000,
            "max_provider_calls": 1,
            "max_capture_bytes": 0,
            "max_items": 0,
        },
        epsilon={"$decimal": "0.1"},
    )
    instance, _ = initialize_local(tmp_path)
    members = {
        procedure.path: render_procedure(procedure.procedure),
        provider.path: render_provider(provider.provider),
        interface.path: render_provider_interface(interface.registration),
        "lines/test.slotted.json": render_line_spec(line),
    }
    for i in range(unrelated_count):
        name = f"unrelated-{i}"
        members[f"procedures/{name}.json"] = render_procedure(_unrelated(name))
    accept_tree(instance, members, name="world")
    other = _other(interface.registration)
    accept_tree(
        instance, {provider_path(other.identity.name): render_provider(other)}, name="new-build"
    )
    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    with instance.bind_accepted_projection(coordinate) as projection:
        selected = indexed_move_pinners(
            projection.typed.connection,
            provider.provider.provider_name,
            other.identity.qualified,
            (line.identity.qualified,),
            include_blueprints=False,
        )
        assert selected == (
            (
                line.identity.qualified,
                "lines/test.slotted.json",
                provider.provider.identity.qualified,
            ),
        )
        assert (
            indexed_move_pinners(
                projection.typed.connection,
                provider.provider.provider_name,
                other.identity.qualified,
                ("Line:absent",),
                include_blueprints=False,
            )
            == ()
        )
        move = build_move(tree, provider.provider.identity.name, other.identity.name)

        class CountedTree(Mapping):
            def __init__(self):
                self.reads = []

            def __getitem__(self, key):
                self.reads.append(key)
                return tree[key]

            def __iter__(self):
                raise AssertionError("A Line rebind never enumerates the instance")

            def __len__(self):
                raise AssertionError("A Line rebind never counts the instance")

        counted = CountedTree()
        rebound = _rebound_line(
            counted, {}, "lines/test.slotted.json", {provider.provider.identity.qualified: move}
        )
        assert len(counted.reads) == 4
        authored = line.model_copy(update={"parameters": {"value": 7}})
        release_rebound = _rebound_line(
            tree,
            {"lines/test.slotted.json": render_line_spec(authored)},
            "lines/test.slotted.json",
            {provider.provider.identity.qualified: move},
        )
        if explicit:
            assert release_rebound is None  # The release's explicit old build is its decision.
        else:
            from_release = parse_line_spec(release_rebound, path="lines/test.slotted.json")
            assert from_release.parameters == {"value": 7}
            assert from_release.bindings["compute"].target == other.identity
            assert from_release.lifecycle.predecessor_digest == line_spec_digest(line).tagged
        result = settle_changes(
            tree,
            writes={},
            connection=projection.typed.connection,
            movers=(
                Mover(
                    line.identity,
                    "lines/test.slotted.json",
                    provider.provider.identity.name,
                    other.identity.name,
                ),
            ),
            moves={provider.provider.identity.qualified: move},
            by_name=True,
        )
    assert not result.refused, result.refused
    assert result.writes == {"lines/test.slotted.json": rebound}
    changed = parse_line_spec(rebound, path="lines/test.slotted.json")
    assert changed.bindings["compute"].target == other.identity
    assert changed.lifecycle.predecessor_digest == line_spec_digest(line).tagged
    assert changed.occurrence_epoch == line.occurrence_epoch
    accept_tree(instance, result.writes, name="rebound")
    assert (
        instance.blob_at(instance.accepted_coordinate().git_oid, procedure.path)
        == members[procedure.path]
    )


def test_slot_interface_index_tracks_retirement_and_revival(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest

    _, accepted, _, interface = slotted()
    instance, _ = initialize_local(tmp_path)
    accept_tree(
        instance,
        {
            accepted.path: render_procedure(accepted.procedure),
            interface.path: render_provider_interface(interface.registration),
        },
        name="slotted",
    )

    def rows():
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            return tuple(
                tuple(row)
                for row in projection.typed.connection.execute(
                    "SELECT source_identity,slot_name,interface_identity "
                    "FROM procedure_provider_slots"
                )
            )

    expected = (
        (
            accepted.procedure.identity.qualified,
            "compute",
            interface.registration.identity.qualified,
        ),
    )
    assert rows() == expected
    retired = accepted.procedure.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=accepted.artifact_digest
            )
        }
    )
    accept_tree(instance, {accepted.path: render_procedure(retired)}, name="retired")
    assert rows() == ()
    revived = retired.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="live", predecessor_digest=procedure_artifact_digest(retired).tagged
            )
        }
    )
    accept_tree(instance, {accepted.path: render_procedure(revived)}, name="revived")
    assert rows() == expected
