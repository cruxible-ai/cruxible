"""One backend resolver for source preview and governed Procedure preparation."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypeVar

from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.captures import CaptureContract as LightCaptureContract
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    NamedCaptureContract,
)
from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.procedures.artifacts import ProcedureArtifact
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.graph import analyze_procedure
from cruxible_client.contracts.procedures.models import (
    TERMINAL_REQUIRED_RUNGS,
    CaptureEgressNode,
    InvokeNode,
)
from cruxible_client.contracts.procedures.source_compiler import (
    CompiledSource,
    SourceCompileError,
    compile_source,
)
from cruxible_client.contracts.procedures.source_program import (
    ProcedureSourceProgram,
    SourceBinding,
    SourceClaimType,
    SourceDiagnostic,
    SourceProcedureBinding,
    SourceQueryBinding,
    SourceSlotBinding,
    SourceSpan,
)
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourceRequest,
    SourceProcedureSelection,
    SourceQuerySelection,
    SourceSlotSelection,
)
from cruxible_client.contracts.query.definitions import QueryDefinition

if TYPE_CHECKING:
    from cruxible_core.indexes.evaluated_state import EvaluationRows

SourceLookup = Callable[[str], Any]
T = TypeVar("T")


def source_ontology_names(text: str) -> frozenset[str]:
    """Conservative lexical candidates; the compiler still resolves exact meanings.

    Attribute suffixes and literal names cover field access, world namespaces,
    explicit claim_type/kind calls and subscripts, including assigned aliases.
    """
    try:
        syntax = ast.parse(text)
    except SyntaxError:
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(syntax):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return frozenset(names)


def resolve_indexed_source(
    request: ProcedureSourceRequest,
    rows: EvaluationRows,
) -> CompiledSource:
    """Resolve the source's named artifacts at the indexed rows."""

    identities, kinds = rows.claim_type_names(source_ontology_names(request.text))
    sources: dict[str, Any] = {}

    def lookup(identity: str) -> Any:
        if identity not in sources:
            sources[identity] = rows.source(identity)
        return sources[identity]

    return resolve_source(
        request,
        lookup=lookup,
        claim_types=(lookup(identity) for identity in identities),
        subject_kinds=kinds,
    )


def resolve_source(
    request: ProcedureSourceRequest,
    *,
    lookup: SourceLookup,
    claim_types: Iterable[object],
    subject_kinds: Iterable[str] = (),
    rules: Literal[
        "cruxible.procedure-source.v1", "cruxible.procedure-source.v2"
    ] = "cruxible.procedure-source.v2",
    retained_terminal_capability: Literal[1, 2, 3] | None = None,
) -> CompiledSource:
    """Resolve and compile source; capability is derived unless reproducing a retained one.

    A slot selection is a signature ``{in, out, effect}``: its Contracts are
    pinned to the accepted versions here, and the compiler types the slot by
    their views. A Procedure names no provider; a Line binds each slot.
    """

    def fail(message: str) -> NoReturn:
        raise SourceCompileError(
            SourceDiagnostic(
                code="cruxible.source.binding_required",
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

    def accepted(kind: str, name: str, types: tuple[type, ...]) -> Any:
        identity = name if name.startswith(kind + ":") else kind + ":" + name
        artifact = lookup(identity)
        if not isinstance(artifact, types):
            fail(f"{identity} is not uniquely accepted at the selected authoring base")
        if hasattr(artifact, "lifecycle") and artifact.lifecycle.state != "live":
            fail(f"{identity} is retired")
        return artifact

    def require(kind: str, name: str, type_: type[T]) -> T:
        artifact: T = accepted(kind, name, (type_,))
        return artifact

    def contract_of(spelling: str) -> Any:
        from cruxible_client.contracts.shapes import (
            Contract,
            contract_identity,
            parse_contract_ref,
        )

        ref = parse_contract_ref(spelling)
        artifact = accepted("Contract", contract_identity(ref.name, ref.version).name, (Contract,))
        return ref, artifact

    child_shapes: dict[str, tuple[bool, int]] = {}

    def child_shape(child: ProcedureArtifact, active: tuple[str, ...]) -> tuple[bool, int]:
        identity = child.identity.name
        if identity in active:
            fail(
                "Recursive Procedure invocation is unsupported: " + " -> ".join((*active, identity))
            )
        if identity in child_shapes:
            return child_shapes[identity]
        graph = analyze_procedure(child.definition)
        leaves = [
            node
            for node in child.definition.nodes
            if not graph.successors[node.node_id] and node.kind != "halt"
        ]
        capture = bool(leaves) and all(
            isinstance(node, CaptureEgressNode) and isinstance(node.input, str) for node in leaves
        )
        required = max(
            (TERMINAL_REQUIRED_RUNGS.get(node.kind, 0) for node in child.definition.nodes),
            default=0,
        )
        for node in child.definition.nodes:
            if isinstance(node, InvokeNode):
                nested = require("Procedure", node.procedure.target.name, ProcedureArtifact)
                required = max(required, child_shape(nested, (*active, identity))[1])
        child_shapes[identity] = (capture, required)
        return capture, required

    bindings: dict[str, SourceBinding] = {}
    for name, selected in request.bindings.items():
        if isinstance(selected, SourceSlotSelection):
            from cruxible_client.contracts.provider_operations import contract_schema_view
            from cruxible_client.contracts.shapes import contract_digest

            def resolve_ref(ref: Any) -> Any:
                from cruxible_client.contracts.shapes import contract_identity

                return lookup(contract_identity(ref.name, ref.version).qualified)

            in_ref, in_contract = contract_of(selected.in_)
            out_ref, out_contract = contract_of(selected.out)
            bindings[name] = SourceSlotBinding.model_validate(
                {
                    "in": in_ref.model_copy(update={"digest": contract_digest(in_contract).tagged}),
                    "out": out_ref.model_copy(
                        update={"digest": contract_digest(out_contract).tagged}
                    ),
                    "effect": selected.effect,
                    "input": contract_schema_view(in_contract, resolve_ref),
                    "output": contract_schema_view(out_contract, resolve_ref),
                }
            )
        elif isinstance(selected, SourceQuerySelection):
            query = require("QueryDefinition", selected.name, QueryDefinition)
            bindings[name] = SourceQueryBinding(name=query.identity.name, definition=query)
        else:
            child = require("Procedure", selected.name, ProcedureArtifact)

            def contract(reference: ArtifactNameRef) -> ContractSchema:
                for contract in child.owned_contracts:
                    if contract.identity == reference.target:
                        return contract.contract_schema
                fail(f"{selected.name} has no owned input/output Contract")

            capture_terminal, required_rung = child_shape(
                child, (request.name.removeprefix("Procedure:"),)
            )
            bindings[name] = SourceProcedureBinding(
                capture_terminal=capture_terminal,
                required_terminal_rung=required_rung,
                name=child.identity.name,
                input=contract(child.definition.contract_in),
                output=contract(child.definition.contract_out),
            )
    captures: set[str] = set()
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
            accepted(
                "CaptureContract",
                name,
                (CaptureContractV1, LightCaptureContract, NamedCaptureContract),
            )
            captures.add(name)
    types: dict[str, SourceClaimType] = {}
    kinds: set[str] = set(subject_kinds)
    for claim_type in claim_types:
        if not isinstance(claim_type, ClaimType):
            fail("The accepted ClaimType directory returned an invalid definition")
        if claim_type.lifecycle.state != "live":
            continue
        structure = ClaimTypeStructure.model_validate(
            {name: getattr(claim_type, name) for name in ClaimTypeStructure.model_fields}
        )
        types[claim_type.predicate] = SourceClaimType(structure=structure)
        kinds.update(claim_type.allowed_subject_kinds)
        kinds.update(claim_type.allowed_object_subject_kinds)
    program = ProcedureSourceProgram(
        syntax_rules=rules,
        text=request.text,
        # Retain a portable source coordinate. The caller's filesystem location
        # belongs to diagnostics, not the accepted Procedure's content identity.
        filename=f"{request.name.removeprefix('Procedure:')}.py",
        first_line=1,
        function=request.function,
        contracts=request.contracts,
        bindings=bindings,
        claim_types=types,
        subject_kinds=tuple(sorted(kinds)),
        capture_contracts=tuple(sorted(captures)),
    )

    def local_span(span: SourceSpan) -> SourceSpan:
        return span.model_copy(
            update={
                "filename": request.filename,
                "line": span.line + request.first_line - 1,
                "end_line": span.end_line + request.first_line - 1,
            }
        )

    try:
        compiled = compile_source(
            program,
            name=request.name,
            input=request.input,
            output=request.output,
            budget=request.budget,
            hard_caps=request.hard_caps,
            terminal_capability=retained_terminal_capability,
            description=request.description,
        )
    except SourceCompileError as exc:
        raise SourceCompileError(
            exc.diagnostic.model_copy(
                update={
                    "span": local_span(exc.diagnostic.span),
                    "related_spans": tuple(local_span(s) for s in exc.diagnostic.related_spans),
                }
            )
        ) from exc
    return replace(
        compiled,
        source_map=tuple(
            entry.model_copy(update={"span": local_span(entry.span)})
            for entry in compiled.source_map
        ),
    )


def verify_source_bindings(
    procedure: ProcedureArtifact,
    *,
    lookup: SourceLookup,
    claim_types: Iterable[object],
    subject_kinds: Iterable[str] = (),
) -> None:
    """Acceptance resolves retained declarations against the candidate state.

    Pure source/graph verification is also performed when rebuilding a projection.
    This additional acceptance check prevents forged schema metadata from claiming
    to describe an accepted provider, query, ClaimType, or child Procedure.
    """
    from cruxible_client.contracts.procedures.models import ProcedureDefinition
    from cruxible_client.contracts.procedures.source_program import SourceContract
    from cruxible_client.contracts.procedures.source_requests import SourceSelection

    definition = procedure.definition
    if not isinstance(definition, ProcedureDefinition) or definition.source is None:
        return
    program = definition.source
    selections: dict[str, SourceSelection] = {}
    for name, binding in program.bindings.items():
        if isinstance(binding, SourceSlotBinding):
            selections[name] = SourceSlotSelection.model_validate(
                {"in": binding.in_.spelling, "out": binding.out.spelling, "effect": binding.effect}
            )
        elif isinstance(binding, SourceQueryBinding):
            selections[name] = SourceQuerySelection(name=binding.name)
        else:
            selections[name] = SourceProcedureSelection(name=binding.name)
    owned = {c.identity.qualified: c for c in procedure.owned_contracts}

    def root(reference: ArtifactNameRef) -> SourceContract:
        if reference.target.qualified not in owned:
            raise ValueError("Source Procedure root requires an owned Contract")
        contract = owned[reference.target.qualified]
        return SourceContract(name=contract.identity.name, schema=contract.contract_schema)

    request = ProcedureSourceRequest(
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
        description=definition.description,
    )
    compiled = resolve_source(
        request,
        lookup=lookup,
        claim_types=claim_types,
        subject_kinds=subject_kinds,
        rules=program.syntax_rules,
        retained_terminal_capability=definition.terminal_capability,
    )
    # Older retained source may carry an author's absolute source location.
    # Verify its dependencies under the same portable coordinate without ever
    # rewriting its stored bytes or recomputing its historical digest.
    assert compiled.definition.source is not None
    expected = definition.model_copy(
        update={
            "source": program.model_copy(
                update={
                    "filename": compiled.definition.source.filename,
                    "first_line": compiled.definition.source.first_line,
                }
            )
        }
    )
    from cruxible_client.contracts.procedures.wire_v3 import compact_procedure

    def comparable(value: ProcedureDefinition) -> object:
        body = compact_procedure(value)
        body.pop("source", None)
        return body

    if comparable(compiled.definition) != comparable(expected):
        raise ValueError("Procedure source bindings differ from their accepted definitions")
