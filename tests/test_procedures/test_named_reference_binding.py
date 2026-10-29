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


def authored_policy_call(*, named=False):
    """Literal Call input through slot ``act``; expected pins are not derived from a conversion."""
    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.procedures.artifacts import (
        NamedProcedureArtifact,
        ProcedureArtifact,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import ProcedureDefinition
    from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition
    from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
    from cruxible_client.contracts.providers import AcceptedProvider, provider_digest, provider_path
    from cruxible_client.contracts.shapes import contract_digest
    from tests.support.operation_world import VALUE, operation, operation_build
    from tests.test_procedures.test_named_reference_closure import authored_procedure

    base = authored_procedure(name="effectful", query=None, named=named)
    build = operation_build(operations=(operation("act", effect="external_mutation"),))
    provider = AcceptedProvider(
        path=provider_path(build.identity.name),
        provider=build,
        artifact_digest=provider_digest(build).tagged,
    )
    effect = ArtifactPin.model_validate(
        {
            "role": "effect-policy",
            "target": {"kind": "EffectPolicy", "name": "test.policy"},
            "artifact_digest": "sha256:" + "e" * 64,
        }
    )

    def reference(target, role, digest):
        value = {"target": target.model_dump()}
        if not named:
            value.update(role=role, artifact_digest=digest)
        return value

    value = {"name": "test.value", "version": 1, "digest": contract_digest(VALUE).tagged}
    node = {
        "kind": "call",
        "node_id": "call",
        "as": "result",
        "input": {"value": 1},
        "slot": "act",
        "effect_policy": reference(effect.target, "effect-policy", effect.artifact_digest),
    }
    raw = base.definition.model_dump(mode="json", by_alias=True)
    raw["nodes"] = [node]
    raw["slots"] = {"act": {"in": value, "out": value, "effect": "external_mutation"}}
    raw["budget"]["max_provider_calls"] = raw["hard_caps"]["max_provider_calls"] = 1
    definition = (NamedProcedureDefinition if named else ProcedureDefinition).model_validate(raw)
    payload = {
        **base.model_dump(),
        "definition": definition,
        "definition_digest": compute_procedure_definition_digest(definition).tagged,
    }
    if not named:
        payload["pins"] = sorted(
            [*base.pins, effect],
            key=lambda p: (p.role, p.target.qualified),
        )
    artifact = (NamedProcedureArtifact if named else ProcedureArtifact).model_validate(payload)
    binding = LineBinding(
        build=binding_pin(build.identity, provider.artifact_digest), operation="act"
    )
    return artifact, provider, binding, effect


def test_call_effect_policy_pin_survives_binding_and_exact_replay_without_an_owner_lookup():
    from cruxible_client.contracts.provider_operations import closure_lookup
    from tests.support.operation_world import VALUE

    artifact, provider, binding, effect = authored_policy_call()
    original = render_procedure(artifact)
    accepted = AcceptedProcedure(
        path=procedure_path(artifact.identity.name),
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
    )
    material = {provider.provider.identity: (provider.provider, provider.artifact_digest)}
    bindings = {"act": binding}
    reads = []

    def lookup(identity):
        reads.append(identity)
        assert identity.kind != "EffectPolicy", "Registry material has no ledger owner"
        return material.get(identity)

    bound = bind_procedure_references(
        accepted, lookup=lookup, bindings=bindings, contracts=closure_lookup((VALUE,))
    )
    assert set(reads) == set(material)
    assert bound.procedure.definition.nodes[0].effect_policy == effect
    assert effect in bound.procedure.pins
    assert render_procedure(bound.authored) == original
    reads.clear()

    def exact(identity, digest):
        result = lookup(identity)
        assert result[1] == digest
        return result

    replayed = replay_procedure_references(
        accepted, pins=bound.procedure.pins, lookup_exact=exact, bindings=bindings
    )
    assert replayed.procedure.definition.nodes[0].effect_policy == effect
    assert replayed.procedure.pins == bound.procedure.pins
    assert set(reads) == set(material)
    assert render_procedure(replayed.authored) == original
    with pytest.raises(ExecutionError, match="registry pins differ"):
        replay_procedure_references(
            accepted,
            pins=tuple(
                p.model_copy(update={"artifact_digest": "sha256:" + "f" * 64}) if p == effect else p
                for p in bound.procedure.pins
            ),
            lookup_exact=exact,
            bindings=bindings,
        )


@pytest.mark.parametrize(
    "kind",
    [
        "EnvironmentManifest",
        "ExhaustReducer",
        "LandingFilter",
        "Policy",
        "ReceiptSetManifest",
        "Reducer",
    ],
)
def test_deferred_registry_envelope_pins_are_not_reinterpreted_as_names(kind):
    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.procedures.artifacts import ProcedureArtifact
    from tests.test_procedures.test_named_reference_closure import authored_procedure

    artifact = authored_procedure(query=None, named=False)
    extra = ArtifactPin.model_validate(
        {
            "role": "registry",
            "target": {"kind": kind, "name": "test.material"},
            "artifact_digest": "sha256:" + "d" * 64,
        }
    )
    artifact = ProcedureArtifact.model_validate(
        {
            **artifact.model_dump(),
            "pins": [*(p.model_dump() for p in artifact.pins), extra.model_dump()],
        }
    )
    accepted = AcceptedProcedure(
        path=procedure_path(artifact.identity.name),
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
    )

    def no_lookup(*args):
        raise AssertionError("Owned contracts and exact registry pins require no ledger lookup")

    bound = bind_procedure_references(accepted, lookup=no_lookup)
    assert extra in bound.procedure.pins
    replayed = replay_procedure_references(
        accepted, pins=bound.procedure.pins, lookup_exact=no_lookup, bindings={}
    )
    assert replayed.procedure.pins == bound.procedure.pins


def test_name_only_effect_policy_is_refused_by_admission_law(tmp_path):
    from cruxible_client.contracts.providers import render_provider
    from cruxible_core.proposals.proposals import evaluate_proposal_tree
    from tests.core_support._support import initialize_local
    from tests.support.operation_world import contract_files

    artifact, provider, _binding, _ = authored_policy_call(named=True)
    instance, _ = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    parent = {
        **instance.tree_at(current.git_oid),
        **contract_files(),
        provider.path: render_provider(provider.provider),
    }
    result = evaluate_proposal_tree(
        base_tree=parent,
        current_tree=parent,
        proposed_tree={
            **parent,
            procedure_path(artifact.identity.name): render_procedure(artifact),
        },
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-10-25T04:00:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert result.candidate is None
    assert any(
        d.code == "cruxible.change_set.unresolved_pin" and "EffectPolicy" in d.message
        for d in result.diagnostics
    )


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
        accepted, pins=admitted.procedure.pins, lookup_exact=exact, bindings={}
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
            bindings={},
        )
    with pytest.raises(ExecutionError, match="lacks QueryDefinition"):
        replay_procedure_references(accepted, pins=(), lookup_exact=exact, bindings={})


def test_v3_artifact_and_admission_graph_digests_reproduce_from_compact_named_wire():
    import json

    from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
    from cruxible_client.contracts.procedures.artifacts import (
        parse_procedure,
        procedure_artifact_digest,
        render_procedure,
    )
    from cruxible_client.contracts.procedures.graph import (
        compute_procedure_definition_digest,
        compute_procedure_node_digests,
    )
    from cruxible_client.contracts.procedures.names import name_procedure
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

    original = name_procedure(_artifact(_definition()))
    wire = render_procedure(original)
    restored = parse_procedure(wire, path="procedures/" + original.identity.name + ".json")
    assert procedure_artifact_digest(original) == typed_digest(
        ArtifactDigest,
        "cruxible-procedure-v3",
        json.loads(wire),
    )
    assert procedure_artifact_digest(restored) == procedure_artifact_digest(original)
    assert compute_procedure_definition_digest(
        restored.definition
    ) == compute_procedure_definition_digest(original.definition)
    assert compute_procedure_node_digests(restored.definition) == compute_procedure_node_digests(
        original.definition
    )
