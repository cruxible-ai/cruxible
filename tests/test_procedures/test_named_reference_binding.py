"""Admission binds names once; authored bytes remain a reproducibility anchor."""

from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.names import name_procedure
from cruxible_core.procedures.reference_binding import (
    bind_procedure_references,
    replay_procedure_references,
)
from tests.test_procedures.test_procedure_artifacts import _artifact, _definition, _digest


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("unrelated_count", [10, 100])
def test_admission_resolves_each_name_once_without_population_reads(named, unrelated_count):
    authored = _artifact(_definition())
    if named:
        authored = name_procedure(authored)
    original = render_procedure(authored)
    digest = procedure_artifact_digest(authored).tagged
    accepted = AcceptedProcedure(
        path=procedure_path(authored.identity.name), procedure=authored, artifact_digest=digest
    )
    selected_digest = _digest("query-successor")
    sources = {f"unrelated-{n}": object() for n in range(unrelated_count)}
    sources["QueryDefinition:claims-by-status"] = (
        SimpleNamespace(lifecycle=ArtifactLifecycle()),
        selected_digest,
    )
    reads = []

    def lookup(identity: ArtifactIdentity):
        reads.append(identity.qualified)
        return sources.get(identity.qualified)

    bound = bind_procedure_references(accepted, lookup=lookup)
    assert reads == ["QueryDefinition:claims-by-status"]
    assert bound.artifact_digest == digest
    assert bound.procedure.definition_digest == authored.definition_digest
    assert bound.procedure.definition.nodes[0].query.artifact_digest == selected_digest
    assert render_procedure(bound.authored) == original
    assert render_procedure(authored) == original
    assert all(pin.artifact_digest != _digest("claims-by-status") for pin in bound.procedure.pins)


def test_admission_refuses_a_retired_named_dependency():
    authored = name_procedure(_artifact(_definition()))
    accepted = AcceptedProcedure(
        path=procedure_path(authored.identity.name),
        procedure=authored,
        artifact_digest=procedure_artifact_digest(authored).tagged,
    )
    with pytest.raises(ExecutionError, match="unavailable"):
        bind_procedure_references(
            accepted,
            lookup=lambda identity: (
                SimpleNamespace(lifecycle=ArtifactLifecycle(state="retired")),
                _digest("retired"),
            ),
        )


def test_replay_uses_recorded_versions_and_refuses_substituted_material():
    authored = name_procedure(_artifact(_definition()))
    accepted = AcceptedProcedure(
        path=procedure_path(authored.identity.name),
        procedure=authored,
        artifact_digest=procedure_artifact_digest(authored).tagged,
    )
    recorded = _digest("admission-query")
    admitted = bind_procedure_references(
        accepted,
        lookup=lambda identity: (SimpleNamespace(identity=identity), recorded),
    )
    reads = []

    def exact(identity, digest):
        reads.append((identity.qualified, digest))
        return SimpleNamespace(identity=identity), recorded

    replayed = replay_procedure_references(
        accepted, pins=admitted.procedure.pins, lookup_exact=exact
    )
    assert reads == [("QueryDefinition:claims-by-status", recorded)]
    assert replayed.procedure.pins == admitted.procedure.pins
    assert replayed.procedure.definition == admitted.procedure.definition
    assert render_procedure(replayed.authored) == render_procedure(authored)
    with pytest.raises(ExecutionError, match="material is unavailable"):
        replay_procedure_references(
            accepted,
            pins=admitted.procedure.pins,
            lookup_exact=lambda identity, digest: (
                SimpleNamespace(identity=identity),
                _digest("new-head-query"),
            ),
        )
    with pytest.raises(ExecutionError, match="lacks QueryDefinition"):
        replay_procedure_references(accepted, pins=(), lookup_exact=exact)
