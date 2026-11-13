"""The external inputs a Line's Procedure declares, read from definitions only.

An external input is what a run takes from outside accepted state: a file a
person or agent puts in the workspace (``workspace_file``), the files a glob
matches (``workspace_glob``), a fetch the run makes (``fetch``), or the
Capture a Trigger hands the run (``trigger_capture``). Each is one acquiring
call node of the Procedure. Where it reads -- the path, glob or URL -- is the
call input's own value, resolved against the Line's parameters when it is a
``$params.NAME`` reference; anything the run computes (``$input``, a step
output) has no location before the run.

Pure: definitions in, a tuple out. No file is opened, nothing is fetched.
``runbook preview`` composes these per step, the run's workspace refusal
names its parameter from them, and ``next`` indexes the workspace files.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Any, Literal

from cruxible_client.contracts.capabilities import CapabilityLookup
from cruxible_client.contracts.procedures.line_specs import LineSpec
from cruxible_client.contracts.procedures.nodes import CallNode
from cruxible_client.contracts.provider_operations import OperationSignature
from cruxible_client.contracts.shapes import Contract
from cruxible_client.contracts.workspace_file import (
    WorkspaceFileGlobRequestV1,
    is_workspace_glob,
)
from cruxible_core.capabilities.artifact import BUILTIN_CAPABILITY_NAMES

_GLOB_DEFAULT_MAX_FILES = WorkspaceFileGlobRequestV1.model_fields["max_files"].default

DeclaredInputKind = Literal["workspace_file", "workspace_glob", "fetch", "trigger_capture"]


@dataclass(frozen=True)
class DeclaredInput:
    """One acquiring call node's external input, as the definitions state it."""

    node_id: str
    kind: DeclaredInputKind
    #: The path, glob or URL; None when the run computes it.
    location: str | None
    #: The Line parameter the location comes from, when it is ``$params.NAME``.
    parameter: str | None
    #: The authored workspace name of a workspace read, when it names one.
    workspace: str | None
    #: The CaptureContract the input lands under (the call's one capture field).
    capture_contract: str | None
    #: A glob read's file cap.
    max_files: int | None = None
    #: A fetch's permitted origins from the Line's acquisition rule (None: any).
    origins: tuple[str, ...] | None = None


def parameter_reference(value: object) -> str | None:
    """``NAME`` when ``value`` is exactly the reference ``$params.NAME``."""

    if isinstance(value, str) and value.startswith("$params."):
        name = value.removeprefix("$params.")
        if name and "." not in name:
            return name
    return None


def effective_parameters(definition: Any, line: LineSpec | None) -> dict[str, object]:
    """The Line's parameters over the Procedure's defaults, as a run sees them."""

    defaults = getattr(definition, "parameter_defaults", None) or {}
    stated = {} if line is None or not isinstance(line.parameters, dict) else line.parameters
    return {**dict(defaults), **dict(stated)}


def resolve_input_value(
    value: object, parameters: Mapping[str, object]
) -> tuple[str | None, str | None]:
    """``(location, parameter)`` for one authored input value.

    A literal string is its own location. ``$params.NAME`` resolves against
    the effective parameters. Any other reference has no location yet.
    """

    name = parameter_reference(value)
    if name is not None:
        resolved = parameters.get(name)
        return (resolved if isinstance(resolved, str) else None), name
    if isinstance(value, str) and not value.startswith("$"):
        return value, None
    return None, None


def parameter_description(contract: Contract | None, name: str | None) -> str | None:
    """The params Contract field's description for ``name``; None when none is written."""

    if contract is None or name is None:
        return None
    field = contract.fields.get(name)
    return None if field is None else field.description


def reads_workspace(slot: Any) -> bool:
    """Whether ``slot`` is a workspace read core's builtin serves.

    Provisional recognition: by the slot's capability name, one of the names in
    ``BUILTIN_CAPABILITY_NAMES``. Glob reads keep today's request handling
    until the file-read slice moves them to their own capability.
    """

    return slot is not None and slot.capability.name in BUILTIN_CAPABILITY_NAMES


@cache
def builtin_slot_signatures() -> Mapping[str, OperationSignature]:
    """Each capability name core's builtin serves -> the builtin operation's signature.

    The provisional slot key (``line_specs.provisional_slot_signatures``) binds
    such a slot by name to the builtin, whichever Contracts its capability pins.
    """

    from cruxible_client.contracts.shapes import Contract, contract_digest
    from cruxible_client.contracts.workspace_file import workspace_file_contracts

    _source, _content, request, result = workspace_file_contracts()
    assert isinstance(request, Contract) and isinstance(result, Contract)
    signature = OperationSignature.model_validate(
        {
            "in": {
                "name": request.name,
                "version": request.version,
                "digest": contract_digest(request).tagged,
            },
            "out": {
                "name": result.name,
                "version": result.version,
                "digest": contract_digest(result).tagged,
            },
            "effect": "pure",
        }
    )
    return MappingProxyType({name: signature for name in sorted(BUILTIN_CAPABILITY_NAMES)})


def _workspace_call(definition: Any, node: CallNode) -> bool:
    return reads_workspace(definition.slots.get(node.slot))


def _external_read(definition: Any, node: CallNode, capabilities: CapabilityLookup | None) -> bool:
    slot = definition.slots.get(node.slot)
    capability = None if slot is None or capabilities is None else capabilities(slot.capability)
    return capability is not None and capability.effect == "external_read"


def workspace_location_key(node_input: object) -> str:
    """Which field of a workspace call's input names what it reads."""

    return "glob" if is_workspace_glob(node_input) else "relative_path"


def declared_inputs(
    definition: Any,
    line: LineSpec | None,
    *,
    capabilities: CapabilityLookup | None = None,
) -> tuple[DeclaredInput, ...]:
    """Every external input the Procedure's top-level acquiring calls declare, in node order.

    Workspace reads are recognised from the slot alone. A fetch is a call whose
    slot's capability is ``external_read`` (``capabilities`` resolves it), or
    whose input names a ``url``; a caller that reads only workspace inputs may
    leave ``capabilities`` out.
    """

    parameters = effective_parameters(definition, line)
    found: list[DeclaredInput] = []
    for node in definition.nodes:
        if not isinstance(node, CallNode) or not node.acquires:
            continue
        fields = tuple(node.captures)
        capture = node.captures[fields[0]].target.name if len(fields) == 1 else None
        node_input = node.input if isinstance(node.input, dict) else {}
        trigger_bound = line is not None and line.trigger_input in {
            f"{node.as_}.{field}" for field in fields
        }
        if trigger_bound:
            assert line is not None and line.trigger_event is not None
            found.append(
                DeclaredInput(
                    node_id=node.node_id,
                    kind="trigger_capture",
                    location=None,
                    parameter=None,
                    workspace=None,
                    capture_contract=line.trigger_event.capture_contract_identity.name,
                )
            )
            continue
        if _workspace_call(definition, node):
            key = workspace_location_key(node_input)
            location, parameter = resolve_input_value(node_input.get(key), parameters)
            workspace, _ = resolve_input_value(node_input.get("workspace"), parameters)
            max_files: int | None = None
            if key == "glob":
                raw = node_input.get("max_files", _GLOB_DEFAULT_MAX_FILES)
                if isinstance(raw, str):
                    raw = parameters.get(parameter_reference(raw) or "")
                max_files = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
            found.append(
                DeclaredInput(
                    node_id=node.node_id,
                    kind="workspace_glob" if key == "glob" else "workspace_file",
                    location=location,
                    parameter=parameter,
                    workspace=workspace,
                    capture_contract=capture,
                    max_files=max_files,
                )
            )
            continue
        if _external_read(definition, node, capabilities) or "url" in node_input:
            location, parameter = resolve_input_value(node_input.get("url"), parameters)
            origins: set[str] | None = None
            if line is not None:
                for field in fields:
                    rule = line.acquisition.get(f"{node.as_}.{field}")
                    if rule is not None and rule.origins is not None:
                        origins = (origins or set()) | set(rule.origins)
            found.append(
                DeclaredInput(
                    node_id=node.node_id,
                    kind="fetch",
                    location=location,
                    parameter=parameter,
                    workspace=None,
                    capture_contract=capture,
                    origins=None if origins is None else tuple(sorted(origins)),
                )
            )
    return tuple(found)


__all__ = [
    "DeclaredInput",
    "DeclaredInputKind",
    "builtin_slot_signatures",
    "declared_inputs",
    "reads_workspace",
    "effective_parameters",
    "parameter_description",
    "parameter_reference",
    "resolve_input_value",
    "workspace_location_key",
]
