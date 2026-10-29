"""Trigger-input rules for a Line: which Procedure input a trigger feeds.

The Line law calls :func:`validate_trigger_input`; Line admission resolves the
input with :func:`trigger_capture_source`. Moved out of ``line_specs``, which
re-exports ``trigger_capture_source``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.nodes import SourceNode

if TYPE_CHECKING:
    from cruxible_client.contracts.procedures.line_specs import LineSpec


def trigger_capture_source(
    line: LineSpec, procedure: AcceptedProcedure, *, by_name: bool = True
) -> SourceNode:
    """Resolve the single input and verify its closed CaptureContract pin."""
    if line.trigger_input is None:
        raise ValueError("this Line binds no trigger input")
    nodes = [
        n
        for n in procedure.procedure.definition.nodes
        if getattr(n, "as_", None) == line.trigger_input
    ]
    if len(nodes) != 1 or not isinstance(nodes[0], SourceNode):
        raise ValueError("trigger_input must name exactly one Source input")
    node = nodes[0]
    pin = node.capture_contract
    selector = line.trigger_event
    if (
        not isinstance(pin, ArtifactPin | ArtifactNameRef)
        or selector is None
        or (pin.target != selector.capture_contract_identity)
        or (
            not by_name
            and isinstance(pin, ArtifactPin)
            and pin.artifact_digest != selector.capture_contract_digest
        )
    ):
        raise ValueError("trigger input CaptureContract differs from the event selector")
    return node


def validate_trigger_input(
    line: LineSpec, procedure: AcceptedProcedure, *, by_name: bool
) -> str | None:
    """The Line law's trigger check: ``None`` when it holds, else why it fails."""
    if line.trigger_input is None:
        return None
    try:
        trigger_capture_source(line, procedure, by_name=by_name)
    except ValueError as exc:
        return str(exc)
    return None


__all__ = ["trigger_capture_source", "validate_trigger_input"]
