"""Call schemas are resolved from interfaces, including authoring and budgets."""

import dataclasses
import hashlib

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.inputs import (
    AuthoringInputError,
    refuse_unserved_procedure_nodes,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureOwnedContract,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.contracts import ProcedureContractItemBudgetExceeded
from cruxible_client.contracts.procedures.models import RepeatBodyNode
from cruxible_client.contracts.procedures.source_compiler import verify_source_graph
from cruxible_client.contracts.procedures.source_program import (
    SourceContract,
    SourceProviderBinding,
)
from cruxible_client.contracts.provider_contracts import (
    ProviderOperationContract,
    validate_provider_value,
)
from tests.test_procedures.test_source_compiler import accepted, compile


def operation():
    return ProviderOperationContract(
        input=ContractSchema(fields={"value": PropertySchema(type="string")}),
        output=ContractSchema(fields={"value": PropertySchema(type="string")}),
    )


def compiled_call():
    binding = SourceProviderBinding(
        provider="demo",
        provider_version="sha256:" + "1" * 64,
        interface="demo.echo/v1",
        interface_version="sha256:" + "2" * 64,
        interface_digest="sha256:" + "3" * 64,
        implementation_digest="sha256:" + "4" * 64,
        effect_class="pure",
        operation=operation(),
    )
    return compile(
        """
        def example(request, bindings):
            result = call(bindings.echo, input=bindings.echo.input(value="hello"))
            return result
    """,
        bindings={"echo": binding},
    )


def test_source_call_carries_only_root_contracts_and_no_call_schema_pins():
    compiled = compiled_call()
    call = compiled.definition.nodes[0]
    assert call.kind == "call"
    assert call.contract_in is None and call.contract_out is None
    assert "contract_in" not in call.model_dump(mode="json")
    assert {contract.name for contract in compiled.contracts} == {"input", "output"}
    verify_source_graph(accepted(compiled).procedure)


def test_historical_source_call_copies_still_verify_without_changing_bytes():
    compiled = compiled_call()
    schema = operation().input
    name = (
        "source." + hashlib.sha256(canonical_bytes(schema.model_dump(mode="json"))).hexdigest()[:24]
    )
    owned = ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name=name), schema=schema
    )
    pins = {
        "contract_" + side: ArtifactPin(
            role="contract-" + side,
            target=owned.identity,
            artifact_digest=procedure_owned_contract_digest(owned).tagged,
        )
        for side in ("in", "out")
    }
    call = compiled.definition.nodes[0].model_copy(update=pins)
    legacy = dataclasses.replace(
        compiled,
        definition=compiled.definition.model_copy(
            update={"nodes": (call, *compiled.definition.nodes[1:])}
        ),
        contracts=tuple(
            sorted(
                (*compiled.contracts, SourceContract(name=name, schema=schema)),
                key=lambda c: c.name,
            )
        ),
    )
    procedure = accepted(legacy).procedure
    before = procedure.model_dump(mode="json")
    verify_source_graph(procedure)
    assert procedure.model_dump(mode="json") == before

    from cruxible_client.contracts.procedures.names import name_procedure

    named = name_procedure(procedure)
    verify_source_graph(named)
    assert {contract.identity.name for contract in named.owned_contracts} == {"input", "output"}
    assert procedure.model_dump(mode="json") == before
    tampered = procedure.definition.model_copy(
        update={
            "nodes": (
                call.model_copy(update={"input": {"value": "changed"}}),
                *compiled.definition.nodes[1:],
            )
        }
    )
    with pytest.raises(ProjectionFormatError, match="disagree"):
        verify_source_graph(procedure.model_copy(update={"definition": tampered}))


@pytest.mark.parametrize("repeat", [False, True])
def test_new_authoring_refuses_copied_call_contracts(repeat):
    node = {"kind": "call", "contract_in": {"kind": "carried_contract", "name": "copied"}}
    if repeat:
        node = {"kind": "repeat", "body": [{"operation": "call", "contract_out": {}}]}
    with pytest.raises(AuthoringInputError, match="ProviderInterface"):
        refuse_unserved_procedure_nodes({"nodes": [node]})


def test_repeat_call_needs_no_contract_copies():
    def pin(role, kind):
        return ArtifactPin(
            role=role,
            target=ArtifactIdentity(kind=kind, name="demo"),
            artifact_digest="sha256:" + "1" * 64,
        )

    body = RepeatBodyNode(
        node_id="repeat_call",
        operation="call",
        provider=pin("provider", "Provider"),
        interface=pin("provider-interface", "ProviderInterface"),
        interface_digest="sha256:" + "2" * 64,
        implementation_digest="sha256:" + "3" * 64,
        spec={"value": "hello"},
        **{"as": "result"},
    )
    assert body.contract_in is None and body.contract_out is None
    assert "contract_out" not in body.model_dump(mode="json")


def test_interface_schema_preserves_list_budget_observations_and_refusals():
    schema = ContractSchema(
        fields={"rows": PropertySchema(type="list", item_fields={"n": PropertySchema(type="int")})}
    )
    contract = ProviderOperationContract(input=schema, output=schema)
    payload = {"rows": [{"n": 1}, {"n": 2}]}
    observations = []
    assert (
        validate_provider_value(
            contract,
            payload,
            direction="input",
            normalize_call=True,
            max_items=2,
            observe_items=lambda n, path: observations.append((n, path)),
        )
        == payload
    )
    assert observations == [(2, "rows")]
    with pytest.raises(ProcedureContractItemBudgetExceeded) as refusal:
        validate_provider_value(
            contract, payload, direction="output", normalize_call=True, max_items=1
        )
    assert (refusal.value.limit, refusal.value.observed, refusal.value.field_path) == (1, 2, "rows")


def test_interface_schema_checks_json_constraints():
    schema = ContractSchema(
        fields={"n": PropertySchema(type="json", json_schema={"type": "integer", "minimum": 1})}
    )
    with pytest.raises(ValueError):
        validate_provider_value(
            ProviderOperationContract(input=schema, output=schema),
            {"n": 0},
            direction="input",
            normalize_call=True,
        )
