"""Coordinate-bound vocabulary for authored Procedures; no reference pinning here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.procedures.authored import (
    ProcedureSpec,
    ProcedureSpecError,
    SourceStep,
    _interface_name,
    _ref,
    lower_procedure_spec,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.provider_interfaces import provider_operation_contract
from cruxible_core.authoring.procedure_source import accepted_build_resolver
from cruxible_core.indexes.evaluated_state import EvaluationRows


def resolve_procedure_spec(
    instance: Any,
    base: Any,
    tree: Mapping[str, bytes],
    spec: ProcedureSpec,
    candidate_paths: tuple[str, ...],
) -> Any:
    """Resolve only named artifacts and the indexed providers fitting one interface."""
    with instance.bind_accepted_projection(base) as projection:
        rows = EvaluationRows(projection)
        edits = {path: tree[path] for path in candidate_paths if path in tree}
        selected = rows.overlay(edits) if edits else rows
        builds = accepted_build_resolver(instance, base, rows)
        cache: dict[str, Any] = {}
        step_paths = {id(step): f"$.steps[{index}]" for index, step in enumerate(spec.steps)}

        def source(identity: str) -> Any:
            if identity not in cache:
                cache[identity] = selected.source(identity)
            value = cache[identity]
            if (
                value is None
                or getattr(getattr(value, "lifecycle", None), "state", "live") != "live"
            ):
                raise ProcedureSpecError(
                    "artifact_reference_unresolved",
                    "$.steps",
                    f"{identity} is not live at the candidate tree",
                )
            return value

        def resolve(kind: str, name: Any) -> Any:
            if kind == "procedure_output":
                child = source("Procedure:" + name.removeprefix("Procedure:"))
                pin = child.definition.contract_out
                return next(
                    c.contract_schema.fields
                    for c in child.owned_contracts
                    if c.identity == pin.target
                )
            step = name
            authored_path = step_paths.get(id(step), "$.steps")
            interface_name = _interface_name(step.interface)
            interface_identity = builds.resolve("ProviderInterface", interface_name)
            interface = source(interface_identity)
            provider_name = step.provider
            if provider_name is None:
                candidates = tuple(
                    selected.connection.execute(
                        "SELECT DISTINCT p.source_identity FROM "
                        f"{selected.table('provider_interfaces')} i "
                        f"JOIN {selected.table('pins')} p ON p.target_identity=i.identity "
                        "AND p.target_digest=i.artifact_digest AND p.edge_kind='required_pin' "
                        f"JOIN {selected.table('providers')} r ON r.identity=p.source_identity "
                        "WHERE i.interface_digest=? AND r.lifecycle='live' "
                        "ORDER BY p.source_identity",
                        (interface.interface_digest,),
                    )
                )
                if len(candidates) != 1:
                    raise ProcedureSpecError(
                        "procedure_ref_unknown",
                        authored_path,
                        f"Interface {step.interface} requires one provider; "
                        f"candidates: {[row[0] for row in candidates]}",
                    )
                provider_identity = str(candidates[0][0])
            else:
                provider_identity = builds.resolve("Provider", provider_name)
            provider = source(provider_identity)
            implementations = [
                item
                for item in provider.implementations
                if item.interface_digest == interface.interface_digest
            ]
            if len(implementations) != 1:
                raise ProcedureSpecError(
                    "procedure_operand_type_mismatch",
                    authored_path,
                    f"{provider_identity} does not implement {step.interface}",
                )
            operation = provider_operation_contract(interface)
            if operation is None:
                raise ProcedureSpecError(
                    "procedure_operand_type_mismatch",
                    authored_path,
                    f"{step.interface} has no operation schema",
                )
            output = operation.output
            return {
                "bindings": {
                    "provider": _ref("Provider", provider_identity, "provider"),
                    "interface": _ref(
                        "ProviderInterface", interface_identity, "provider-interface"
                    ),
                    # N1b owns these mirrors and their eventual removal.
                    "interface_digest": interface.interface_digest,
                    "implementation_digest": implementations[0].implementation_digest,
                },
                "input": operation.input.fields,
                "output": (
                    operation.material.fields
                    if operation.material is not None
                    else output.fields
                    if isinstance(output, ContractSchema)
                    else None
                )
                if isinstance(step, SourceStep)
                else output.fields
                if isinstance(output, ContractSchema)
                else None,
            }

        return lower_authoring_input(lower_procedure_spec(spec, resolve=resolve))
