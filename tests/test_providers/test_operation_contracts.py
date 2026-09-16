"""Interface compatibility, invocation boundaries and frozen graph verification."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import ProcedureDefinitionV4, ProcedureDefinitionV5
from cruxible_client.contracts.provider_contracts import (
    ProviderOperationContractV1,
    read_provider_operation_contract,
)
from cruxible_core.procedures.execution import ProcedureExecutor
from cruxible_core.providers.provider_classifiers import ProviderBucketClassifierRegistry
from tests.core_support._p2b1_support import install_demo_classifier
from tests.test_procedures.test_procedure_execution import _Authority, _Contracts
from tests.test_providers.test_provider_invocation_journal import (
    _accepted_one_provider,
    _Invoker,
    _prepared_v5,
)


def operation():
    return ProviderOperationContractV1.model_validate(
        {
            "input": {"fields": {"size": {"type": "int"}}},
            "output": {"fields": {"size": {"type": "int"}}},
        }
    )


def call_procedure(*, repeat=False, payload=None):
    accepted = _accepted_one_provider(repeat=repeat)
    raw = accepted.procedure.definition.model_dump(mode="json", by_alias=True)
    raw["graph_format"] = 5
    if repeat:
        raw["nodes"][0]["body"][0]["operation"] = "call"
        if payload is not None:
            raw["nodes"][0]["body"][0]["spec"] = payload
    else:
        raw["nodes"][0]["kind"] = "call"
        if payload is not None:
            raw["nodes"][0]["input"] = payload
    definition = ProcedureDefinitionV5.model_validate(raw)
    procedure = accepted.procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    return accepted.model_copy(
        update={
            "procedure": procedure,
            "artifact_digest": procedure_artifact_digest(procedure).tagged,
        }
    )


@pytest.mark.parametrize("repeat", [False, True])
def test_new_call_grammar_does_not_reinterpret_old_provider_graphs(repeat):
    old = _accepted_one_provider(repeat=repeat).procedure.definition
    old_bytes = canonical_bytes(old.model_dump(mode="json", by_alias=True))
    old_digest = compute_procedure_definition_digest(old)
    current = call_procedure(repeat=repeat).procedure.definition
    assert compute_procedure_definition_digest(current) != old_digest
    assert (
        canonical_bytes(
            ProcedureDefinitionV4.model_validate_json(old_bytes).model_dump(
                mode="json", by_alias=True
            )
        )
        == old_bytes
    )
    assert (
        compute_procedure_definition_digest(ProcedureDefinitionV4.model_validate_json(old_bytes))
        == old_digest
    )
    with pytest.raises(ValidationError):
        ProcedureDefinitionV5.model_validate(
            {**old.model_dump(mode="json", by_alias=True), "graph_format": 5}
        )


class BadOutput(_Invoker):
    def invoke_provider(self, **kwargs):
        outcome = super().invoke_provider(**kwargs)
        return replace(
            outcome, envelope=outcome.envelope.model_copy(update={"output": {"size": "bad"}})
        )


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("case", ["valid", "invalid-input", "invalid-output"])
def test_calls_enforce_interfaces_before_invocation_and_before_progress(tmp_path, repeat, case):
    accepted = call_procedure(
        repeat=repeat, payload={"size": "bad"} if case == "invalid-input" else None
    )
    prepared, fixture = _prepared_v5(accepted, tmp_path, operation_contract=operation())
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    invoker = BadOutput() if case == "invalid-output" else _Invoker()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    if case == "valid":
        assert result.status == "succeeded"
        assert len(invoker.calls) == 1
    else:
        assert result.status != "succeeded"
        assert result.output is None
        assert len(invoker.calls) == (0 if case == "invalid-input" else 1)


def test_interface_contract_is_required_not_inferred_from_effect_class():
    with pytest.raises(ValueError, match="declare"):
        read_provider_operation_contract(canonical_bytes({"effect_class": "external_read"}).hex())


def test_interface_cannot_silently_discard_unknown_schema_rules():
    raw = {
        "contracts": {
            "input": {"fields": {"x": {"type": "int", "maximum": 5}}},
            "output": {"fields": {}},
        }
    }
    with pytest.raises(ValidationError, match="maximum"):
        read_provider_operation_contract(canonical_bytes(raw).hex())


def test_contract_compatibility_ignores_documentation_and_type_aliases():
    from cruxible_client.contracts.procedures.contract_schema import ContractSchema
    from cruxible_client.contracts.provider_contracts import operation_schema_shape

    documented = ContractSchema.model_validate(
        {
            "description": "The input size",
            "fields": {
                "size": {"type": "integer", "description": "How many items", "indexed": True},
            },
        }
    )
    assert operation_schema_shape(documented) == operation_schema_shape(operation().input)


@pytest.mark.parametrize(
    "case", ["match", "wrong-input", "wrong-output", "source", "effect", "mutation"]
)
def test_specialization_and_carried_schemas_match_before_execution(case):
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
    from cruxible_client.contracts.procedures.artifacts import (
        ProcedureArtifactV2,
        ProcedureOwnedContractV1,
        check_provider_node_contract,
        procedure_owned_contract_digest,
    )
    from cruxible_client.contracts.procedures.models import SourceNodeV4
    from tests.core_support._p2b1_support import accepted_interface

    schema = operation().input
    owned = ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name="size"), schema=schema
    )
    pin = ArtifactPin(
        role="contract-in",
        target=owned.identity,
        artifact_digest=procedure_owned_contract_digest(owned).tagged,
    )
    out_pin = pin.model_copy(update={"role": "contract-out"})
    old = call_procedure().procedure
    node = old.definition.nodes[0].model_copy(update={"contract_in": pin, "contract_out": out_pin})
    definition = old.definition.model_copy(
        update={"contract_in": pin, "contract_out": out_pin, "nodes": (node,)}
    )
    procedure = ProcedureArtifactV2(
        identity=old.identity,
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=tuple(
            sorted(
                (pin, out_pin, node.provider, node.interface),
                key=lambda p: (p.role, p.target.qualified),
            )
        ),
        owned_contracts=(owned,),
        activation_policy="drain",
    )
    interface = accepted_interface()
    declaration = operation().model_dump(mode="json")
    if case in {"wrong-input", "wrong-output"}:
        declaration[case.removeprefix("wrong-")] = {"fields": {"size": {"type": "string"}}}
    if case in {"source", "mutation"}:
        node = SourceNodeV4(
            node_id="source",
            provider=node.provider,
            interface=node.interface,
            interface_digest=node.interface_digest,
            implementation_digest=node.implementation_digest,
            capture_contract=pin.model_copy(
                update={
                    "role": "capture-contract",
                    "target": ArtifactIdentity(kind="CaptureContract", name="observation"),
                }
            ),
            request={"size": 1},
            as_="result",
        )
    if case == "mutation":
        declaration["output"] = "playbill-provider-result-to-external-capture-v1"
    effect = "external_mutation" if case == "mutation" else "external_read"
    interface = interface.model_copy(
        update={
            "registration": interface.registration.model_copy(
                update={
                    "interface_bytes_hex": canonical_bytes(
                        {"contracts": declaration, "effect_class": effect}
                    ).hex(),
                    "effect_class": "pure" if case == "effect" else effect,
                }
            )
        }
    )
    if case == "match":
        assert check_provider_node_contract(node, interface, procedure) == operation()
    else:
        with pytest.raises(ValueError):
            check_provider_node_contract(node, interface, procedure)


def test_previous_compiler_cannot_project_the_call_grammar():
    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
    from cruxible_core.compiler.compiler import (
        UPGRADE_COMPILER,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.compiler.projection_artifacts import parse_projection_tree

    procedure = call_procedure().procedure
    with pytest.raises(ProjectionFormatError, match="graph-v5"):
        parse_projection_tree(
            {procedure_path(procedure.identity.name): render_procedure(procedure)},
            artifact_kinds=artifact_kinds_for_compiler(UPGRADE_COMPILER),
            registry=projection_registry_for_compiler(UPGRADE_COMPILER),
        )


def test_web_interface_classification_matches_the_recorded_fixtures():
    from cruxible_client.contracts.provider_interfaces import (
        AcceptedProviderInterfaceRegistrationV1,
        provider_interface_digest,
        provider_interface_path,
    )
    from cruxible_core.providers.web_fetch import (
        WebFetchBucketClassifier,
        web_fetch_interface_registration,
    )

    registration = web_fetch_interface_registration()
    accepted = AcceptedProviderInterfaceRegistrationV1(
        registration=registration,
        path=provider_interface_path("web.fetch"),
        artifact_digest=provider_interface_digest(registration).tagged,
    )
    installed = ProviderBucketClassifierRegistry().install(accepted, WebFetchBucketClassifier())
    assert len(installed.results) == 4
    contract = read_provider_operation_contract(registration.interface_bytes_hex)
    assert contract.output == "playbill-provider-result-to-external-capture-v1"
