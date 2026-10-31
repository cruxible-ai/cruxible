"""Coordinate-bound vocabulary for authored Procedures; no reference pinning here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.captures import capture_components
from cruxible_client.contracts.procedures.authored import (
    CallStep,
    ProcedureSpec,
    ProcedureSpecError,
    lower_procedure_spec,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.provider_operations import (
    contract_schema_view,
    operation_signature_digest,
)
from cruxible_client.contracts.shapes import Contract, ContractRef, contract_digest
from cruxible_client.contracts.workspace_file import is_workspace_file_signature
from cruxible_core.indexes.evaluated_state import EvaluationRows


def resolve_procedure_spec(
    instance: Any,
    base: Any,
    tree: Mapping[str, bytes],
    spec: ProcedureSpec,
    candidate_paths: tuple[str, ...],
) -> Any:
    """Resolve only named artifacts and the slot Contracts a Procedure pins.

    A Procedure names no provider: a slot is its signature, pinned here to the
    exact accepted Contract versions; a Line binds it.
    """
    with instance.bind_accepted_projection(base) as projection:
        rows = EvaluationRows(projection)
        edits = {path: tree[path] for path in candidate_paths if path in tree}
        selected = rows.overlay(edits) if edits else rows
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

        def contract(ref: ContractRef, path: str) -> Contract:
            from cruxible_client.contracts.shapes import contract_identity

            value = source(contract_identity(ref.name, ref.version).qualified, path)
            if not isinstance(value, Contract):
                raise ProcedureSpecError(
                    "artifact_reference_unresolved", path, f"{ref.spelling} is not a Contract"
                )
            return value

        def pinned(slot: ProcedureSlot, path: str) -> ProcedureSlot:
            pins = {}
            for side, ref in (("in", slot.in_), ("out", slot.out)):
                digest = contract_digest(contract(ref, f"{path}.{side}")).tagged
                if ref.digest is not None and ref.digest != digest:
                    raise ProcedureSpecError(
                        "artifact_reference_unresolved",
                        f"{path}.{side}",
                        f"{ref.spelling} pins {ref.digest}; the accepted version is {digest}",
                    )
                pins[side] = ref.model_copy(update={"digest": digest})
            return ProcedureSlot.model_validate(
                {"in": pins["in"], "out": pins["out"], "effect": slot.effect}
            )

        def resolve_ref(ref: ContractRef) -> Contract | None:
            from cruxible_client.contracts.shapes import contract_identity

            value = maybe_source(contract_identity(ref.name, ref.version).qualified)
            return value if isinstance(value, Contract) else None

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
                assert isinstance(name, ProcedureSlot)
                return pinned(name, "$.slots")
            if kind == "extract_row":
                from cruxible_client.contracts.shapes import parse_contract_ref

                row_path = step_paths.get(id(name), "$.steps") + ".row"
                ref = parse_contract_ref(name.row)
                row = contract(ref, row_path)
                return {
                    "row": ref.model_copy(
                        update={"digest": contract_digest(row).tagged}
                    ).model_dump(mode="json"),
                    "row_fields": contract_schema_view(row, resolve_ref).fields,
                }
            step = name
            authored_path = step_paths.get(id(step), "$.steps")
            acquiring = isinstance(step, CallStep) and bool(step.captures)
            captures = (
                [
                    source(
                        "CaptureContract:" + name.removeprefix("CaptureContract:"),
                        f"{authored_path}.captures.{field}",
                    )
                    for field, name in sorted(step.captures.items())
                ]
                if acquiring
                else []
            )
            capture = captures[0] if len(captures) == 1 else None
            slot = pinned(spec.slots[step.slot], "$.slots." + step.slot)
            input_view = contract_schema_view(contract(slot.in_, authored_path), resolve_ref)
            output_view = contract_schema_view(contract(slot.out, authored_path), resolve_ref)
            request_defaults: dict[str, Any] = {}
            output_fields: Any = output_view.fields
            workspace_source = acquiring and is_workspace_file_signature(
                operation_signature_digest(slot)
            )
            if acquiring and not workspace_source:
                from cruxible_client.contracts.procedures.capture_outputs import (
                    WHOLE_WIRE_CAPTURE_FIELD,
                )
                from cruxible_client.contracts.procedures.contract_schema import PropertySchema
                from cruxible_client.contracts.provider_operations import capture_fields

                if not capture_fields(contract(slot.out, authored_path)):
                    # The operation returns one Capture as its whole output: the
                    # call's value names only that Capture.
                    output_fields = {WHOLE_WIRE_CAPTURE_FIELD: PropertySchema(type="string")}
            if workspace_source:
                from cruxible_client.contracts.procedures.contract_schema import PropertySchema
                from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_CAPTURE_FIELD

                # The read's exact bytes, committed by core beside its result.
                output_fields = {
                    **output_fields,
                    WORKSPACE_FILE_CAPTURE_FIELD: PropertySchema(type="string"),
                }
            if workspace_source and capture is not None:
                components = capture_components(capture)
                coordinates = components.coordinate_schema_pins
                selectors = components.selector_schema_pins
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
                # The daemon reads the workspace; the request is not the slot input.
                input_view = None
            return {
                "request_defaults": request_defaults,
                "input": None if input_view is None else input_view.fields,
                "input_allow_extra": False,
                "output": output_fields,
            }

        return lower_authoring_input(lower_procedure_spec(spec, resolve=resolve))
