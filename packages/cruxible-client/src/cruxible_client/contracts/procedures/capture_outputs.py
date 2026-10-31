"""Capture outputs: which call fields commit Captures, and the laws that pin them.

A call acquires when its slot's out Contract carries ``capture<K>`` fields (or
when its operation returns its whole output as Capture wire). Each such field
is one capture output, described by ``{node_path, field, capture_kind,
acquisition_key}``, and keyed ``<alias>.<field>``: its acquisition rule, its
plan decision, its journal records and its Capture budget are all its own.
Admission, trigger binding and execution all read these descriptors. A trigger
substitutes one of them, named ``<alias>.<field>``, and only from a call with
exactly one capture field: substituting one field of a call would leave the
call's other fields unproduced.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.captures import CaptureContractAny
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ExtractNode,
    ProcedureDefinition,
)
from cruxible_client.contracts.provider_operations import capture_fields
from cruxible_client.contracts.shapes import Contract, ContractRef
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_CAPTURE_FIELD,
    is_workspace_file_signature,
)

#: The capture field of an operation whose whole output is Capture wire.
WHOLE_WIRE_CAPTURE_FIELD = "capture"

ContractLookup = Callable[[ContractRef], Contract | None]
CaptureLookup = Callable[[str], CaptureContractAny | None]


class CaptureOutputDescriptor(BaseModel):
    """One capture output of an acquiring call.

    ``node_path`` is the call's node id, ``field`` its capture field,
    ``capture_kind`` the CaptureContract the field's bytes are committed as,
    and ``acquisition_key`` (``<alias>.<field>``) the key its acquisition
    rule, plan decision and journal records are named by.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_path: str
    alias: str
    field: str
    capture_kind: str
    acquisition_key: str

    @property
    def reference(self) -> str:
        """``<alias>.<field>``: how a trigger names this output."""

        return f"{self.alias}.{self.field}"


def capture_output_descriptors(
    definition: ProcedureDefinition,
) -> tuple[CaptureOutputDescriptor, ...]:
    """Every capture output of the graph's acquiring calls, by node then field."""

    found: list[CaptureOutputDescriptor] = []
    for node in definition.nodes:
        if not isinstance(node, CallNode) or not node.acquires:
            continue
        for field, capture in node.captures.items():
            found.append(
                CaptureOutputDescriptor(
                    node_path=node.node_id,
                    alias=node.as_,
                    field=field,
                    capture_kind=capture.target.name,
                    acquisition_key=f"{node.as_}.{field}",
                )
            )
    return tuple(found)


class TriggerTargetRefused(ValueError):
    """A trigger input that names no capture output, or more than one."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def trigger_capture_output(
    definition: ProcedureDefinition, trigger_input: str
) -> CaptureOutputDescriptor:
    """The one capture output a trigger substitutes, named ``<alias>.<field>``.

    Only a call with exactly one capture field can be substituted: a trigger
    aimed at a call with several, by any name, is ``trigger_target_ambiguous``
    (binding one field would leave the others, and the call's typed fields,
    unproduced). Such a call runs normally when nothing substitutes it.
    """

    alias, _dot, field = trigger_input.partition(".")
    outputs = [item for item in capture_output_descriptors(definition) if item.alias == alias]
    if not outputs:
        raise TriggerTargetRefused(
            "trigger_target_unknown",
            f"trigger_input {trigger_input!r} names no acquiring call's capture output",
        )
    if len(outputs) != 1:
        raise TriggerTargetRefused(
            "trigger_target_ambiguous",
            f"acquiring call {alias!r} commits {len(outputs)} capture fields "
            f"({', '.join(item.field for item in outputs)}); a trigger substitutes only a "
            "call with one capture field",
        )
    (output,) = outputs
    if field != output.field:
        raise TriggerTargetRefused(
            "trigger_target_unknown",
            f"trigger_input {trigger_input!r} names no capture output; name it "
            f"{output.reference!r}",
        )
    return output


def procedure_capture_problem(
    definition: ProcedureDefinition,
    *,
    contract_lookup: ContractLookup | None,
    capture_lookup: CaptureLookup,
) -> tuple[str, str] | None:
    """``(code, message)`` for the first capture law the graph breaks, or ``None``."""

    from cruxible_client.contracts.provider_operations import operation_signature_digest

    calls = {node.as_: node for node in definition.nodes if isinstance(node, CallNode)}
    for node in calls.values():
        slot = definition.slots.get(node.slot)
        if (
            slot is not None
            and node.acquires
            and is_workspace_file_signature(operation_signature_digest(slot))
        ):
            # Core reads the workspace bytes itself: its one capture field is core's.
            if set(node.captures) != {WORKSPACE_FILE_CAPTURE_FIELD}:
                return (
                    "cruxible.procedure.captures_mismatch",
                    f"call {node.node_id!r}: a workspace.file read commits the bytes core read "
                    f"as its one capture field, {WORKSPACE_FILE_CAPTURE_FIELD!r}",
                )
            declared: dict[str, str] | None = None
        else:
            out = None if slot is None or contract_lookup is None else contract_lookup(slot.out)
            declared = None if out is None else dict(capture_fields(out))
        if declared is None:
            pass
        elif not node.acquires:
            if declared:
                return (
                    "cruxible.procedure.captures_undeclared",
                    f"call {node.node_id!r}: its out Contract carries capture fields "
                    f"{sorted(declared)}; name the CaptureContract of each in 'captures'",
                )
            continue
        elif not declared:
            if set(node.captures) != {WHOLE_WIRE_CAPTURE_FIELD}:
                return (
                    "cruxible.procedure.captures_mismatch",
                    f"call {node.node_id!r}: its operation returns one Capture as its whole "
                    f"output; its captures name exactly {WHOLE_WIRE_CAPTURE_FIELD!r}",
                )
        else:
            if set(node.captures) != set(declared):
                return (
                    "cruxible.procedure.captures_mismatch",
                    f"call {node.node_id!r}: captures {sorted(node.captures)} differ from the "
                    f"out Contract's capture fields {sorted(declared)}",
                )
            for field, kind in declared.items():
                named = node.captures[field].target.name
                if named != kind:
                    return (
                        "cruxible.procedure.captures_mismatch",
                        f"call {node.node_id!r}: field {field!r} is capture<{kind}>, "
                        f"not CaptureContract {named!r}",
                    )
        for field, capture in node.captures.items():
            if capture_lookup(capture.target.name) is None:
                return (
                    "cruxible.procedure.capture_contract_unresolved",
                    f"call {node.node_id!r}: field {field!r} names CaptureContract "
                    f"{capture.target.name!r}, which is not accepted",
                )
    for extract in definition.nodes:
        if not isinstance(extract, ExtractNode):
            continue
        producer = calls.get(extract.capture_alias)
        reference = None if producer is None else producer.captures.get(extract.capture_field)
        contract = None if reference is None else capture_lookup(reference.target.name)
        decoding = getattr(contract, "decoding", None)
        if decoding is None:
            return (
                "cruxible.procedure.extract_decoding_undeclared",
                f"extract {extract.node_id!r} reads {extract.capture!r}, whose CaptureContract "
                "declares no decoding; declare its decoding (a v4 CaptureContract)",
            )
        if decoding.format not in {"json", "csv"}:
            return (
                "cruxible.procedure.extract_format_unsupported",
                f"extract {extract.node_id!r}: core extracts json and csv, not {decoding.format}",
            )
        if decoding.format == "csv" and (extract.header or extract.complete_when is not None):
            return (
                "cruxible.procedure.extract_format_unsupported",
                f"extract {extract.node_id!r}: a CSV is complete when its header is present "
                "and every row reads; it declares no header fields or complete_when",
            )
        row = None if contract_lookup is None else contract_lookup(extract.row)
        if row is not None:
            unknown = sorted(set(extract.fields) - set(row.fields))
            if unknown:
                return (
                    "cruxible.procedure.extract_field_unknown",
                    f"extract {extract.node_id!r} reads fields its row Contract "
                    f"{extract.row.spelling} does not declare: {unknown}",
                )
    return None


__all__ = [
    "WHOLE_WIRE_CAPTURE_FIELD",
    "CaptureOutputDescriptor",
    "TriggerTargetRefused",
    "capture_output_descriptors",
    "procedure_capture_problem",
    "trigger_capture_output",
]
