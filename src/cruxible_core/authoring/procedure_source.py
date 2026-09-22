"""One backend resolver for source preview and governed Procedure preparation."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from typing import Any, NoReturn, TypeVar

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.captures import CaptureContractV1, capture_contract_digest
from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactV2,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.graph import analyze_procedure_v4
from cruxible_client.contracts.procedures.models import (
    TERMINAL_REQUIRED_RUNGS,
    CaptureEgressNodeV6,
    InvokeNodeV6,
    ProcedureDefinitionV5,
    ProcedurePinSlotRefV1,
)
from cruxible_client.contracts.procedures.source_compiler import (
    CompiledSource,
    SourceCompileError,
    compile_source,
)
from cruxible_client.contracts.procedures.source_program import (
    ProcedureSourceV1,
    SourceBinding,
    SourceClaimType,
    SourceDiagnostic,
    SourceProcedureBinding,
    SourceProviderBinding,
    SourceQueryBinding,
    SourceSpan,
)
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourceRequestV1,
    SourceProcedureSelection,
    SourceProviderSelection,
    SourceQuerySelection,
)
from cruxible_client.contracts.provider_contracts import read_provider_operation_contract
from cruxible_client.contracts.provider_interfaces import (
    ProviderInterfaceRegistrationV1,
    provider_interface_digest,
)
from cruxible_client.contracts.providers import ProviderV2, provider_digest
from cruxible_client.contracts.query.definitions import QueryDefinitionV1, query_definition_digest

SourceLookup = Callable[[str], Any]
T = TypeVar("T")


def resolve_source(
    request: ProcedureSourceRequestV1, *, lookup: SourceLookup, claim_types: Iterable[object]
) -> CompiledSource:
    def fail(message: str) -> NoReturn:
        raise SourceCompileError(
            SourceDiagnostic(
                code="playbill.source.binding_required",
                message=message,
                span=SourceSpan(
                    filename=request.filename,
                    line=request.first_line,
                    column=0,
                    end_line=request.first_line,
                    end_column=0,
                ),
            )
        )

    def require(kind: str, name: str, type_: type[T]) -> T:
        identity = name if name.startswith(kind + ":") else kind + ":" + name
        artifact = lookup(identity)
        if not isinstance(artifact, type_):
            fail(f"{identity} is not uniquely accepted at the selected authoring base")
        if hasattr(artifact, "lifecycle") and artifact.lifecycle.state != "live":
            fail(f"{identity} is retired")
        return artifact

    child_shapes: dict[str, tuple[bool, int]] = {}

    def child_shape(child: ProcedureArtifactV2, active: tuple[str, ...]) -> tuple[bool, int]:
        identity = child.identity.name
        if identity in active:
            fail(
                "Recursive Procedure invocation is unsupported: " + " -> ".join((*active, identity))
            )
        if identity in child_shapes:
            return child_shapes[identity]
        if not child.directly_runnable or not isinstance(child.definition, ProcedureDefinitionV5):
            fail(f"{identity} must have exact bindings and explicit operation contracts")
        graph = analyze_procedure_v4(child.definition)
        leaves = [
            node
            for node in child.definition.nodes
            if not graph.successors[node.node_id] and node.kind != "halt"
        ]
        capture = bool(leaves) and all(
            isinstance(node, CaptureEgressNodeV6) and isinstance(node.input, str) for node in leaves
        )
        required = max(
            (TERMINAL_REQUIRED_RUNGS.get(node.kind, 0) for node in child.definition.nodes),
            default=0,
        )
        for node in child.definition.nodes:
            if isinstance(node, InvokeNodeV6):
                nested = require("Procedure", node.procedure.target.name, ProcedureArtifactV2)
                if procedure_artifact_digest(nested).tagged != node.procedure.artifact_digest:
                    fail(f"{identity} has a child binding that is not current at this coordinate")
                required = max(required, child_shape(nested, (*active, identity))[1])
        child_shapes[identity] = (capture, required)
        return capture, required

    bindings: dict[str, SourceBinding] = {}
    for name, selected in request.bindings.items():
        if isinstance(selected, SourceProviderSelection):
            provider = require("Provider", selected.provider, ProviderV2)
            interface = require(
                "ProviderInterface", selected.interface, ProviderInterfaceRegistrationV1
            )
            implementations = [
                item
                for item in provider.implementations
                if item.interface_digest == interface.interface_digest
            ]
            if len(implementations) != 1:
                fail(
                    f"{selected.provider} does not expose exactly one matching "
                    f"{selected.interface} implementation"
                )
            bindings[name] = SourceProviderBinding(
                provider=provider.identity.name,
                provider_version=provider_digest(provider).tagged,
                interface=interface.identity.name,
                interface_version=provider_interface_digest(interface).tagged,
                interface_digest=interface.interface_digest,
                implementation_digest=implementations[0].implementation_digest,
                effect_class=interface.effect_class,
                operation=read_provider_operation_contract(interface.interface_bytes_hex),
            )
        elif isinstance(selected, SourceQuerySelection):
            query = require("QueryDefinition", selected.name, QueryDefinitionV1)
            bindings[name] = SourceQueryBinding(
                name=query.identity.name,
                version=query_definition_digest(query).tagged,
                definition=query,
            )
        else:
            child = require("Procedure", selected.name, ProcedureArtifactV2)

            def contract(pin: ArtifactPin | ProcedurePinSlotRefV1) -> ContractSchema:
                if isinstance(pin, ArtifactPin):
                    for contract in child.owned_contracts:
                        if (
                            contract.identity == pin.target
                            and procedure_owned_contract_digest(contract).tagged
                            == pin.artifact_digest
                        ):
                            return contract.contract_schema
                fail(f"{selected.name} has no exact owned input/output Contract")

            if not child.directly_runnable:
                fail(f"{selected.name} has unresolved slots")
            capture_terminal, required_rung = child_shape(
                child, (request.name.removeprefix("Procedure:"),)
            )
            bindings[name] = SourceProcedureBinding(
                capture_terminal=capture_terminal,
                required_terminal_rung=required_rung,
                name=child.identity.name,
                version=procedure_artifact_digest(child).tagged,
                input=contract(child.definition.contract_in),
                output=contract(child.definition.contract_out),
            )
    captures = {}
    try:
        syntax = ast.parse(request.text)
    except SyntaxError:
        syntax = ast.Module(body=[], type_ignores=[])
    for node in ast.walk(syntax):
        if (
            isinstance(node, ast.keyword)
            and node.arg == "capture_contract"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            name = node.value.value.removeprefix("CaptureContract:")
            artifact = require("CaptureContract", name, CaptureContractV1)
            captures[name] = capture_contract_digest(artifact).tagged
    types: dict[str, SourceClaimType] = {}
    kinds: set[str] = set()
    for claim_type in claim_types:
        if not isinstance(claim_type, ClaimType):
            fail("The accepted ClaimType directory returned an invalid definition")
        if claim_type.lifecycle.state != "live":
            continue
        structure = ClaimTypeStructure.model_validate(
            {name: getattr(claim_type, name) for name in ClaimTypeStructure.model_fields}
        )
        types[claim_type.predicate] = SourceClaimType(
            version=claim_type_digest(claim_type).tagged, structure=structure
        )
        kinds.update(claim_type.allowed_subject_kinds)
        kinds.update(claim_type.allowed_object_subject_kinds)
    program = ProcedureSourceV1(
        text=request.text,
        filename=request.filename,
        first_line=request.first_line,
        function=request.function,
        contracts=request.contracts,
        bindings=bindings,
        claim_types=types,
        subject_kinds=tuple(sorted(kinds)),
        capture_contracts=captures,
    )
    return compile_source(
        program,
        name=request.name,
        input=request.input,
        output=request.output,
        budget=request.budget,
        hard_caps=request.hard_caps,
        terminal_capability=request.terminal_capability,
        description=request.description,
    )


def verify_source_bindings(
    procedure: ProcedureArtifactV2, *, lookup: SourceLookup, claim_types: Iterable[object]
) -> None:
    """Acceptance resolves retained declarations against the candidate state.

    Pure source/graph verification is also performed when rebuilding a projection.
    This additional acceptance check prevents forged schema metadata from claiming
    to describe an accepted provider, query, ClaimType, or child Procedure.
    """
    from cruxible_client.contracts.procedures.models import ProcedureDefinitionV6
    from cruxible_client.contracts.procedures.source_program import SourceContract
    from cruxible_client.contracts.procedures.source_requests import SourceSelection

    definition = procedure.definition
    if not isinstance(definition, ProcedureDefinitionV6) or definition.source is None:
        return
    program = definition.source
    selections: dict[str, SourceSelection] = {}
    for name, binding in program.bindings.items():
        if isinstance(binding, SourceProviderBinding):
            selections[name] = SourceProviderSelection(
                provider=binding.provider, interface=binding.interface
            )
        elif isinstance(binding, SourceQueryBinding):
            selections[name] = SourceQuerySelection(name=binding.name)
        else:
            selections[name] = SourceProcedureSelection(name=binding.name)
    owned = {c.identity.qualified: c for c in procedure.owned_contracts}

    def root(pin: ArtifactPin | ProcedurePinSlotRefV1) -> SourceContract:
        if not isinstance(pin, ArtifactPin) or pin.target.qualified not in owned:
            raise ValueError("Source Procedure root requires an owned Contract")
        contract = owned[pin.target.qualified]
        return SourceContract(name=contract.identity.name, schema=contract.contract_schema)

    request = ProcedureSourceRequestV1(
        name=definition.name,
        text=program.text,
        filename=program.filename,
        first_line=program.first_line,
        function=program.function,
        input=root(definition.contract_in),
        output=root(definition.contract_out),
        contracts=program.contracts,
        bindings=selections,
        budget=definition.budget,
        hard_caps=definition.hard_caps,
        terminal_capability=definition.terminal_capability,
        description=definition.description,
    )
    compiled = resolve_source(request, lookup=lookup, claim_types=claim_types)
    if compiled.definition != definition:
        raise ValueError("Procedure source bindings differ from their accepted definitions")
