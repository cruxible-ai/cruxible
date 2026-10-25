"""Coordinate-bound vocabulary for authored Procedures; no reference pinning here."""

from __future__ import annotations

import json
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
from cruxible_core.errors import RequestRefusedError
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

        def maybe_source(identity: str) -> Any:
            if identity not in cache:
                cache[identity] = selected.source(identity)
            return cache[identity]

        def source(identity: str, path: str) -> Any:
            value = maybe_source(identity)
            if (
                value is None
                or getattr(getattr(value, "lifecycle", None), "state", "live") != "live"
            ):
                from cruxible_client.contracts.errors import ReadRefusalError
                from cruxible_core.service.discovery.get import resolve_get_ref

                # Keep the indexed exact-name clean path. On refusal, reuse
                # discovery's existing nearest-name diagnostics for this kind.
                candidates: tuple[str, ...]
                try:
                    found = resolve_get_ref(instance, base, identity)
                    candidates = (found.display,) if found.identity != identity else ()
                except ReadRefusalError as exc:
                    candidates = exc.candidates
                raise ProcedureSpecError(
                    "artifact_reference_unresolved",
                    path,
                    f"{identity} is not live at the candidate tree. "
                    f"Nearest candidates: {list(candidates)}",
                )
            return value

        def binding_name(kind: str, name: str, path: str) -> str:
            bare = name.removeprefix(kind + ":")
            if kind == "Provider":
                if "/" in bare:
                    return "Provider:" + bare
                # Candidate registrations are change-local. Existing build
                # metadata/lifecycle edits do not re-register a name head.
                added = []
                for candidate_path in candidate_paths:
                    if (
                        not candidate_path.startswith("providers/" + bare + "/")
                        and candidate_path != "providers/" + bare + ".json"
                    ):
                        continue
                    provider = selected.source_bytes(candidate_path)
                    from cruxible_client.contracts.providers import parse_provider

                    artifact = parse_provider(provider, path=candidate_path)
                    if (
                        artifact.lifecycle.state == "live"
                        and rows.source(artifact.identity.qualified) is None
                    ):
                        added.append(artifact.identity.qualified)
                if len(added) > 1:
                    raise ProcedureSpecError(
                        "artifact_reference_unresolved",
                        path,
                        f"Ambiguous candidate builds for {bare}: {added}",
                    )
                if added:
                    return added[0]
            try:
                return builds.resolve(kind, name)
            except RequestRefusedError as exc:
                raise ProcedureSpecError("artifact_reference_unresolved", path, str(exc)) from exc

        def resolve(kind: str, name: Any) -> Any:
            if kind == "procedure_output":
                child = source(
                    "Procedure:" + name.invoke.removeprefix("Procedure:"),
                    step_paths[id(name)] + ".invoke",
                )
                pin = child.definition.contract_out
                return next(
                    c.contract_schema.fields
                    for c in child.owned_contracts
                    if c.identity == pin.target
                )
            if kind == "claim":
                from cruxible_client.contracts.procedures.source_views import claim_view_schema

                claim_type = source(
                    "ClaimType:" + name.claim_type.removeprefix("ClaimType:"),
                    step_paths[id(name)] + ".claim_type",
                )
                shape = claim_type.structure
                subject_kind = name.subject_kind or (
                    shape.allowed_subject_kinds[0]
                    if len(shape.allowed_subject_kinds) == 1
                    else None
                )
                if subject_kind not in shape.allowed_subject_kinds:
                    raise ProcedureSpecError(
                        "procedure_operand_type_mismatch",
                        step_paths.get(id(name), "$.steps"),
                        "Declare one of the ClaimType subject kinds: "
                        + str(shape.allowed_subject_kinds),
                    )
                return {"output": claim_view_schema(shape).fields, "subject_kind": subject_kind}
            if kind == "slot":
                interface_identity = binding_name(
                    "ProviderInterface", _interface_name(name), "$.slots"
                )
                return source(interface_identity, "$.slots").interface_digest
            step = name
            authored_path = step_paths.get(id(step), "$.steps")
            capture = (
                source(
                    "CaptureContract:" + step.contract.removeprefix("CaptureContract:"),
                    authored_path + ".contract",
                )
                if isinstance(step, SourceStep)
                else None
            )
            interface_name = _interface_name(step.interface)
            interface_identity = binding_name(
                "ProviderInterface", interface_name, authored_path + ".interface"
            )
            if maybe_source(interface_identity) is None and interface_name.startswith(
                "workspace.file/v"
            ):
                # Genesis retains its frozen single-path v1 registration over
                # the workspace.file@2 interface bytes. Resolve that exact
                # declaration without rewriting it or selecting another version.
                legacy = maybe_source("ProviderInterface:workspace.file")
                requested_version = int(interface_name.rpartition("/v")[2])
                if (
                    legacy is not None
                    and json.loads(bytes.fromhex(legacy.interface_bytes_hex)).get("version")
                    == requested_version
                ):
                    interface_identity = "ProviderInterface:workspace.file"
            interface = source(interface_identity, authored_path + ".interface")
            provider_name = step.provider
            if provider_name in spec.slots:
                declared = binding_name(
                    "ProviderInterface",
                    _interface_name(spec.slots[provider_name]),
                    "$.slots." + provider_name,
                )
                if declared != interface_identity:
                    raise ProcedureSpecError(
                        "procedure_operand_type_mismatch",
                        authored_path,
                        "Provider slot and step interfaces differ",
                    )
                operation = provider_operation_contract(interface)
                if operation is None:
                    raise ProcedureSpecError(
                        "procedure_operand_type_mismatch",
                        authored_path,
                        "Slot interface requires an operation schema",
                    )
                output = operation.material if isinstance(step, SourceStep) else operation.output
                return {
                    "bindings": {
                        "provider": {"kind": "slot", "slot_name": provider_name},
                        "interface": _ref(
                            "ProviderInterface", interface_identity, "provider-interface"
                        ),
                        "interface_digest": interface.interface_digest,
                    },
                    "input": operation.input.fields,
                    "input_allow_extra": operation.input.allow_extra,
                    "output": output.fields if isinstance(output, ContractSchema) else None,
                }
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
                provider_identity = binding_name(
                    "Provider", provider_name, authored_path + ".provider"
                )
            provider = source(provider_identity, authored_path + ".provider")
            implementations = [
                item
                for item in getattr(provider, "implementations", ())
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
            request_defaults: dict[str, Any] = {}
            if isinstance(step, SourceStep) and interface_identity.startswith(
                "ProviderInterface:workspace.file"
            ):
                assert capture is not None
                coordinates = capture.coordinate_schema_pins
                selectors = capture.selector_schema_pins
                if len(coordinates) == len(selectors) == 1:
                    coordinate_type, selector_type = (
                        coordinates[0].target.name,
                        selectors[0].target.name,
                    )
                    request_defaults = {
                        "tag": "playbill-workspace-file-source-request-v2",
                        "coordinate_type": coordinate_type,
                        "selector_type": selector_type,
                        "replayability": "exact"
                        if getattr(capture, "replay", "exact") == "exact"
                        else "attested_only",
                    }
                    if coordinate_type in {"http-response-v1", "workspace-file-v1"}:
                        request_defaults["coordinate"] = {"revision": "working"}
                    if selector_type in {"whole-response-v1", "whole-file-v1"}:
                        request_defaults["selector"] = {"whole": True}
            return {
                "request_defaults": request_defaults,
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
                "input_allow_extra": operation.input.allow_extra,
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
