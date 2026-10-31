"""Trigger-input rules for a Line: which capture output a trigger substitutes.

A Line's ``trigger_input`` names one capture output of its Procedure,
``<alias>.<capture field>``, of a call that commits exactly that one capture
field. The triggering Capture is bound to that field and the call does not
run. A trigger aimed at a call with several capture fields, by any name, is
refused ``trigger_target_ambiguous``; such a call runs normally. The Line law
calls :func:`validate_trigger_input`; Line admission resolves the output with
:func:`trigger_capture_target`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.capture_outputs import (
    CaptureOutputDescriptor,
    TriggerTargetRefused,
    trigger_capture_output,
)
from cruxible_client.contracts.procedures.nodes import CallNode

if TYPE_CHECKING:
    from cruxible_client.contracts.procedures.line_specs import LineSpec


def trigger_capture_target(
    line: LineSpec, procedure: AcceptedProcedure
) -> tuple[CallNode, CaptureOutputDescriptor]:
    """The acquiring call and the one capture output the Line's trigger substitutes."""

    if line.trigger_input is None:
        raise ValueError("this Line binds no trigger input")
    definition = procedure.procedure.definition
    descriptor = trigger_capture_output(definition, line.trigger_input)
    node = next(
        item
        for item in definition.nodes
        if isinstance(item, CallNode) and item.node_id == descriptor.node_path
    )
    selector = line.trigger_event
    if selector is None or node.captures[descriptor.field].target != (
        selector.capture_contract_identity
    ):
        raise ValueError("trigger input CaptureContract differs from the event selector")
    return node, descriptor


def trigger_capture_source(line: LineSpec, procedure: AcceptedProcedure) -> CallNode:
    """The acquiring call a trigger substitutes (see :func:`trigger_capture_target`)."""

    return trigger_capture_target(line, procedure)[0]


def validate_trigger_input(line: LineSpec, procedure: AcceptedProcedure) -> str | None:
    """The Line law's trigger check: ``None`` when it holds, else why it fails."""
    if line.trigger_input is None:
        return None
    try:
        trigger_capture_target(line, procedure)
    except ValueError as exc:
        return str(exc)
    return None


__all__ = [
    "TriggerTargetRefused",
    "trigger_capture_source",
    "trigger_capture_target",
    "validate_trigger_input",
]
