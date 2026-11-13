"""Coordinate-bound vocabulary for authored Procedures; no reference pinning here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_client.contracts.capabilities import (
    Capability,
    capability_pin,
    tree_capability_resolver,
)
from cruxible_client.contracts.captures import capture_components
from cruxible_client.contracts.procedures.authored import (
    CallStep,
    ProcedureSpec,
    ProcedureSpecError,
    lower_procedure_spec,
    refuse_closed_steps,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.shapes import Contract, ContractRef, contract_digest
from cruxible_core.capabilities.artifact import BUILTIN_CAPABILITY_NAMES
from cruxible_core.indexes.evaluated_state import EvaluationRows


def resolve_procedure_spec(
    instance: Any,
    base: Any,
    tree: Mapping[str, bytes],
    spec: ProcedureSpec,
    candidate_paths: tuple[str, ...],
) -> Any:
    """Resolve only named artifacts and the slot capabilities a Procedure pins.

    A Procedure names no provider: a slot names one capability version, pinned
    here to its exact digest in ``tree`` (the base plus this change set), and a
    call step is typed against that capability's request and result Contracts;
    a Line binds the slot. A step outside the Procedure template is refused
    first, for every Procedure (D-R37 h1).
    """
    refuse_closed_steps(spec)
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

        capability_at = tree_capability_resolver(tree)

        def slot_capability(name: str, slot: ProcedureSlot) -> Capability:
            """The capability ``slot`` names, at the base or in this change set."""

            path = f"$.slots.{name}.capability"
            ref = slot.capability
            found = capability_at(ref.model_copy(update={"digest": None}))
            if found is None:
                raise ProcedureSpecError(
                    "slot_capability_unresolved",
                    path,
                    f"Slot {name!r} names capability {ref.spelling}, which is not accepted "
                    "at the base or authored in this change set.",
                    repair=f"Author capability {ref.spelling} first (or in the same change "
                    "set), or name an accepted capability version.",
                )
            digest = capability_pin(found).digest
            if ref.digest is not None and ref.digest != digest:
                raise ProcedureSpecError(
                    "slot_capability_digest_mismatch",
                    path,
                    f"Slot {name!r} pins capability {ref.spelling} at {ref.digest}; the "
                    f"accepted version is {digest}.",
                    repair="Name the capability as name@N without a digest; authoring pins it.",
                )
            return found

        def pinned(name: str, slot: ProcedureSlot) -> ProcedureSlot:
            return slot.model_copy(
                update={"capability": capability_pin(slot_capability(name, slot))}
            )

        def slot_name_of(slot: ProcedureSlot) -> str:
            return next(key for key, value in spec.slots.items() if value is slot)

        def resolve_ref(ref: ContractRef) -> Contract | None:
            from cruxible_client.contracts.shapes import contract_identity

            value = maybe_source(contract_identity(ref.name, ref.version).qualified)
            return value if isinstance(value, Contract) else None

        def extract_layout(step: Any) -> dict[str, Any]:
            """An extract step through its call's CaptureKind layout: the row typing its
            records, or the layout refusal at the step's path."""

            from cruxible_client.contracts.procedures.layout_extract import (
                LayoutExtractNode,
                LayoutExtractRefused,
                resolve_layout_extract,
            )
            from cruxible_client.contracts.provider_operations import capture_list_fields

            path = step_paths.get(id(step), "$.steps")
            alias, field = step.capture.step, step.capture.path[0]
            call = next(
                (item for item in spec.steps if isinstance(item, CallStep) and item.call == alias),
                None,
            )
            kind_name = None if call is None else call.captures.get(field)
            if call is None or kind_name is None:
                # The graph law names a capture no acquiring call commits.
                return {}
            kind = source(
                "CaptureContract:" + kind_name.removeprefix("CaptureContract:"), path + ".capture"
            )
            reads_list = False
            slot = spec.slots.get(call.slot)
            if slot is not None and slot.capability.name not in BUILTIN_CAPABILITY_NAMES:
                out = contract(slot_capability(call.slot, slot).out, path)
                reads_list = field in {name for name, _kind, _cap in capture_list_fields(out)}
            node = LayoutExtractNode.model_validate(
                {
                    "node_id": step.id or step.extract,
                    "capture": f"$steps.{alias}.{field}",
                    "records": step.records,
                    "header": step.header,
                    "key": {part: rule.model_dump() for part, rule in step.key.items()},
                    "duplicates": step.duplicates,
                    "only_keys": None if step.only_keys is None else [],
                    "as": step.extract,
                }
            )
            try:
                resolved = resolve_layout_extract(
                    node, getattr(kind, "layout", None), reads_list=reads_list
                )
            except LayoutExtractRefused as refused:
                raise ProcedureSpecError(
                    refused.code.rsplit(".", 1)[-1],
                    path,
                    refused.message,
                    repair=f"Name a record set and header fields CaptureKind {kind_name} "
                    "declares in its layout, or add them to the kind's layout.",
                ) from refused
            row = resolved.row
            if row.digest is None:
                row = row.model_copy(update={"digest": contract_digest(contract(row, path)).tagged})
            return {"row": row.model_dump(mode="json")}

        def resolve(kind: str, name: Any) -> Any:
            if kind == "query":
                return source(
                    "QueryDefinition:" + name.query.removeprefix("QueryDefinition:"),
                    step_paths[id(name)] + ".query",
                )
            if kind == "contract":
                return resolve_ref(name)
            if kind == "procedure_output":
                from cruxible_client.contracts.shapes import contract_ref_for_identity

                child = source(
                    "Procedure:" + name.invoke.removeprefix("Procedure:"),
                    step_paths[id(name)] + ".invoke",
                )
                output = resolve_ref(
                    contract_ref_for_identity(child.definition.contract_out.target)
                )
                return output
            if kind == "claim":
                from cruxible_client.contracts.procedures.views import claim_view_contract

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
                return {"output": claim_view_contract(shape).fields, "subject_kind": subject_kind}
            if kind == "slot":
                assert isinstance(name, ProcedureSlot)
                return pinned(slot_name_of(name), name)
            if kind == "extract_layout":
                return extract_layout(name)
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
            slot = spec.slots[step.slot]
            capability = slot_capability(step.slot, slot)
            input_view: Contract | None = contract(capability.in_, authored_path)
            output_view = contract(capability.out, authored_path)
            request_defaults: dict[str, Any] = {}
            output_fields: Any = output_view.fields
            output_contract: Any = dict(output_view.fields)
            # Provisional builtin recognition: by capability name. Glob reads keep
            # today's request handling until the file-read slice moves them.
            workspace_source = acquiring and slot.capability.name in BUILTIN_CAPABILITY_NAMES
            from cruxible_client.contracts.procedures.capture_outputs import CAPTURE_FIELD
            from cruxible_client.contracts.shapes import (
                ANY_CAPTURE_KIND,
                CaptureType,
                ContractField,
            )
            from cruxible_client.contracts.source_references import HTTP_RESPONSE_COORDINATE_TYPE

            # The whole-wire Capture is typed by the kind it is taken under, so
            # it flows into a derive's capture<K> or capture<*> input.
            wire_capture = ContractField(
                type=CaptureType(
                    capture_kind=ANY_CAPTURE_KIND if capture is None else capture.identity.name
                )
            )
            if acquiring and not workspace_source:
                from cruxible_client.contracts.provider_operations import (
                    capture_fields,
                    capture_list_fields,
                )
                from cruxible_client.contracts.shapes import ListType

                out_view = output_view
                listed = capture_list_fields(out_view)
                if not capture_fields(out_view) and not listed:
                    # The operation returns one Capture as its whole output: the
                    # call's value names only that Capture.
                    output_fields = {CAPTURE_FIELD: wire_capture}
                    output_contract = None
                else:
                    # A capture<*> field, or a list<capture<*>>'s members, is typed
                    # by the kind the call's captures map binds (D-R37 a1).
                    bound = {
                        field: resolved.identity.name
                        for field, resolved in zip(sorted(step.captures), captures, strict=True)
                    }
                    retyped = dict(output_fields)
                    for field, declared in output_fields.items():
                        bound_kind = bound.get(field)
                        typed = declared.type
                        if bound_kind is None:
                            continue
                        if (
                            isinstance(typed, CaptureType)
                            and typed.capture_kind == ANY_CAPTURE_KIND
                        ):
                            retyped[field] = declared.model_copy(
                                update={"type": CaptureType(capture_kind=bound_kind)}
                            )
                        elif (
                            isinstance(typed, ListType)
                            and isinstance(typed.item, CaptureType)
                            and typed.item.capture_kind == ANY_CAPTURE_KIND
                        ):
                            retyped[field] = declared.model_copy(
                                update={
                                    "type": typed.model_copy(
                                        update={"item": CaptureType(capture_kind=bound_kind)}
                                    )
                                }
                            )
                    output_fields = retyped
            if workspace_source:
                # The read's exact bytes, committed by core beside its result.
                output_fields = {**output_fields, CAPTURE_FIELD: wire_capture}
                output_contract = None
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
                    if coordinate_type == HTTP_RESPONSE_COORDINATE_TYPE:
                        request_defaults["coordinate"] = {"revision": "working"}
                    if selector_type == "whole-response-v1":
                        request_defaults["selector"] = {"whole": True}
                # The daemon reads the workspace; the request is not the slot input.
                input_view = None
            return {
                "request_defaults": request_defaults,
                "input": None if input_view is None else input_view.fields,
                "input_allow_extra": False,
                "output": output_fields,
                "output_contract": output_contract,
            }

        def maybe_source_identity(identity: Any) -> Any:
            # Inline boundaries are co-proposed after this check. Type their
            # declared fields now, rather than reading the previous version.
            from cruxible_client.contracts.procedures.authored import generated_contract_name

            if identity.kind == "Contract":
                for role, fields in lowered.generated.items():
                    name = generated_contract_name(spec.procedure, role)
                    if identity.name == name + "/v1":
                        return Contract(name=name, version=1, fields=fields)
            return maybe_source(identity.qualified)

        lowered = lower_procedure_spec(spec, resolve=resolve)
        from cruxible_client.contracts.procedures.graph import ProcedureGraphFormatError
        from cruxible_client.contracts.procedures.models import ProcedureDefinition
        from cruxible_core.procedures.claims_typing import (
            ClaimsTypingRefused,
            validate_claims_templates,
        )

        # Definition/graph errors retain the ordinary authoring refusal; only
        # Claims vocabulary errors belong to claims_type_invalid.
        try:
            definition = ProcedureDefinition.model_validate(lowered.definition)
        except (ValueError, ProcedureGraphFormatError):
            # The ordinary definition lowerer validates again after boundary
            # Contracts are co-proposed and supplies the typed refusal/repair.
            return lowered
        try:
            validate_claims_templates(definition, maybe_source_identity)
        except (ClaimsTypingRefused, ValueError) as exc:
            raise ProcedureSpecError(
                getattr(exc, "code", "claims_type_invalid").rsplit(".", 1)[-1],
                _claims_refusal_path(spec, exc),
                f"Procedure {spec.procedure}: {exc}",
            ) from exc
        return lowered


def _claims_refusal_path(spec: Any, refusal: Exception) -> str:
    """The authored element a Claims typing refusal names: its step, then its template."""

    from cruxible_client.contracts.procedures.authored import authored_step_path

    step = authored_step_path(spec, getattr(refusal, "node_id", None))
    element = getattr(refusal, "element", None)
    return step if element is None or step == "$.steps" else f"{step}.{element}"
