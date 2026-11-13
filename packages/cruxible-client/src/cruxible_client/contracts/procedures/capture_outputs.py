"""Capture outputs: which call fields commit Captures, and the laws that pin them.

A call acquires when its slot's capability's out Contract carries ``capture<K>``
fields: a Capture is always a named field of the result Contract, never the
whole result (core's builtin workspace reads commit the bytes core read as
the one field their result declares: ``capture`` for ``workspace.file``,
``captures`` for ``workspace.files``). Each such field is one capture output,
described by ``{node_path, field, capture_kind, acquisition_key}``, and keyed
``<alias>.<field>``: its acquisition rule, its plan decision, its journal
records and its Capture budget are all its own.
Admission, trigger binding and execution all read these descriptors. A trigger
substitutes one of them, named ``<alias>.<field>``, and only from a call with
exactly one capture field: substituting one field of a call would leave the
call's other fields unproduced.

A ``list<capture<*>>`` field is one capture output whose members are each a
Capture; the call's ``captures`` map binds their kind (D-R37 a1). An extract
over it merges its members' records and declares no header or
``complete_when`` (a5); a trigger never substitutes it (a7).
"""

from __future__ import annotations

from collections.abc import Callable, Collection

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.capabilities import CapabilityLookup
from cruxible_client.contracts.captures import CAPTURE_FIELD, CaptureContractAny
from cruxible_client.contracts.procedures.layout_extract import (
    LayoutExtractNode,
    LayoutExtractRefused,
    resolve_layout_extract,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureDefinition,
)
from cruxible_client.contracts.provider_operations import capture_fields, capture_list_fields
from cruxible_client.contracts.shapes import ANY_CAPTURE_KIND, Contract, ContractRef
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILES_OPERATION,
    workspace_read_operation,
)

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
    capability_lookup: CapabilityLookup | None,
    builtin_capabilities: Collection[str],
) -> tuple[str, str] | None:
    """``(code, message)`` for the first capture law the graph breaks, or ``None``.

    A call's out Contract is its slot's capability's ``out``
    (``capability_lookup``). A slot naming one of ``builtin_capabilities`` (the
    capability names core's builtin serves) is a workspace read: core commits
    the bytes it read as the one field that out Contract declares, a
    ``capture<K>`` for one path or a ``list<capture<K>>`` for a glob, and
    returns nothing else.
    """

    calls = {node.as_: node for node in definition.nodes if isinstance(node, CallNode)}
    #: Per call alias, its ``list<capture<K>>`` fields: each member is a Capture (D-R37 a1).
    list_fields: dict[str, frozenset[str]] = {}
    for node in calls.values():
        slot = definition.slots.get(node.slot)
        capability = (
            None
            if slot is None or capability_lookup is None
            else capability_lookup(slot.capability)
        )
        out = (
            None
            if capability is None or contract_lookup is None
            else contract_lookup(capability.out)
        )
        listed = () if out is None else capture_list_fields(out)
        list_fields[node.as_] = frozenset(name for name, _kind, _cap in listed)
        declared = (
            None
            if out is None
            else {
                **dict(capture_fields(out)),
                **{name: kind for name, kind, _cap in listed},
            }
        )
        builtin = (
            None
            if slot is None or slot.capability.name not in builtin_capabilities
            else workspace_read_operation(slot.capability.name)
        )
        if builtin is not None and out is not None:
            # Core reads the workspace bytes itself and returns nothing else: the
            # out Contract declares exactly the one field core commits them as.
            wanted = (
                list_fields[node.as_]
                if builtin == WORKSPACE_FILES_OPERATION
                else (frozenset(dict(capture_fields(out))))
            )
            if len(out.fields) != 1 or set(out.fields) != set(wanted):
                form = (
                    "one list<capture<K>> field"
                    if builtin == WORKSPACE_FILES_OPERATION
                    else "one capture<K> field"
                )
                return (
                    "cruxible.procedure.captures_mismatch",
                    f"call {node.node_id!r}: {slot.capability.spelling if slot else builtin} "
                    f"is served by core's builtin, which returns {form} holding the bytes "
                    f"core read and nothing else; its out Contract {out.name}@{out.version} "
                    f"declares {sorted(out.fields)}",
                )
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
        else:
            # A Capture is always a named field of the result Contract: an out
            # Contract with no capture field commits none.
            if set(node.captures) != set(declared):
                return (
                    "cruxible.procedure.captures_mismatch",
                    f"call {node.node_id!r}: captures {sorted(node.captures)} differ from the "
                    f"out Contract's capture fields {sorted(declared)}; a Capture is a named "
                    "capture<K> field of the result Contract, never the whole result",
                )
            for field, kind in declared.items():
                named = node.captures[field].target.name
                # capture<*>: the call's captures map binds the kind (D-R37 a1).
                if kind != ANY_CAPTURE_KIND and named != kind:
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
        if not isinstance(extract, LayoutExtractNode):
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
        if extract.row is None:
            # Accepted Procedures pin each extract's row (ruling L1); authoring adds it.
            return (
                "cruxible.procedure.extract_row_unpinned",
                f"extract {extract.node_id!r} pins no row Contract; author the Procedure "
                "again (authoring pins the row of the record set it names)",
            )
        # The node names a record set and header fields of its kind's layout; the
        # layout law already holds the CSV shape and the row's field rules.
        try:
            resolve_layout_extract(
                extract,
                getattr(contract, "layout", None),
                reads_list=extract.capture_field
                in list_fields.get(extract.capture_alias, frozenset()),
                pinned_row=None if contract_lookup is None else contract_lookup(extract.row),
            )
        except LayoutExtractRefused as refused:
            return (refused.code, refused.message)
    return None


__all__ = [
    "CAPTURE_FIELD",
    "CaptureOutputDescriptor",
    "TriggerTargetRefused",
    "capture_output_descriptors",
    "procedure_capture_problem",
    "trigger_capture_output",
]
