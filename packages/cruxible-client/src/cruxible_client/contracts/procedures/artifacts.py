"""Governed Procedure envelopes and acceptance laws."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.captures import CaptureContractAny
from cruxible_client.contracts.content_memo import ParseMemo, sealed_memo
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.acquisition_rules import (
    LineAcquisitionRule,
)
from cruxible_client.contracts.procedures.graph import (
    compute_procedure_definition_digest,
    procedure_guard_halt_targets,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ExtractNode,
    InboxEgressNode,
    ProcedureDefinition,
    RepeatNode,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.shapes import (
    Contract,
    ContractRef,
    EnumResolver,
    FieldInvalid,
    check_value,
    contract_ref_for_identity,
)

_PROCEDURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class ProcedureFormatError(FormatError):
    """A Procedure artifact or canonical path is invalid."""


class _StrictProcedureArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class ProcedureArtifact(_StrictProcedureArtifactModel):
    """The one Procedure envelope: one named graph.

    Every dependency the graph names -- its input, output and parameter
    Contracts included, which are governed Contracts -- resolves at the
    accepted coordinate; the envelope itself pins nothing and carries no
    Contracts. Acquisition rules are not the Procedure's:
    its Line declares them per capture output (``<alias>.<field>``).
    """

    artifact_format: Literal["cruxible-procedure-v4"] = "cruxible-procedure-v4"
    identity: ArtifactIdentity
    definition: ProcedureDefinition
    definition_digest: str
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()
    #: The envelope carries no exact pins; its dependencies are named.
    pins: ClassVar[tuple[ArtifactPin, ...]] = ()

    @model_validator(mode="before")
    @classmethod
    def _stored(cls, value: object) -> object:
        if isinstance(value, dict):
            if "owned_contracts" in value:
                raise ValueError(
                    "procedure_owned_contracts_retired: a Procedure carries no Contracts; its "
                    "input, output and params are governed Contracts (generated "
                    "<procedure>.input|output|params@N, or a named name@N)"
                )
            if "acquisition_defaults" in value:
                raise ValueError(
                    "procedure_acquisition_defaults_retired: acquisition rules live on the "
                    "Line only; declare them as the Line's acquisition per capture output "
                    "(<alias>.<field>)"
                )
            definition = value.get("definition")
            if isinstance(definition, dict) and any(
                isinstance(node, dict) and "id" in node for node in definition.get("nodes", ())
            ):
                from cruxible_client.contracts.procedures.wire_v3 import expand_procedure

                return expand_procedure(value)
        return value

    @field_validator("definition_digest")
    @classmethod
    def _definition_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _correspondence(self) -> "ProcedureArtifact":
        if self.identity.kind != "Procedure" or not _PROCEDURE_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("Procedure identity must be path-addressable")
        if self.definition.name != self.identity.name:
            raise ValueError("Procedure definition name must match its identity")
        if self.definition_digest != compute_procedure_definition_digest(self.definition).tagged:
            raise ValueError("Procedure definition digest does not reproduce")
        guard_halts = procedure_guard_halt_targets(self.definition)
        nodes = []
        for index, node in enumerate(self.definition.nodes):
            following = (
                self.definition.nodes[index + 1].node_id
                if index + 1 < len(self.definition.nodes)
                else None
            )
            updates: dict[str, None] = {}
            for edge in ("next", "on_true"):
                if following is not None and getattr(node, edge, None) == following:
                    if edge != "next" or following not in guard_halts:
                        updates[edge] = None
            nodes.append(node.model_copy(update=updates) if updates else node)
        object.__setattr__(
            self, "definition", self.definition.model_copy(update={"nodes": tuple(nodes)})
        )
        return self


#: How an accepted Procedure can run: on its own (``procedure run``), only as a
#: Line (its terminals act outward under a Line's authority), or not at all
#: (a node no run lane serves).
ProcedureRunnable = Literal["direct", "line", "unsupported"]

#: Node kinds the direct run lane executes.
DIRECT_NODE_KINDS = frozenset(
    {
        "read",
        "extract",
        "transform",
        "project",
        "guard",
        "repeat",
        "halt",
        "call",
        "select",
        "invoke",
    }
)
#: Node kinds only a Line runs: they egress under the Line's authority.
LINE_NODE_KINDS = frozenset({"claims", "emit_capture", "post_inbox"})


class ProcedureNodeSupport(_StrictProcedureArtifactModel):
    """One node the direct run lane does not execute, and where it can run."""

    node_id: str
    kind: str
    runs_on: Literal["line", "nowhere"]


def procedure_runnability(
    definition: ProcedureDefinition,
) -> tuple[ProcedureRunnable, tuple[ProcedureNodeSupport, ...]]:
    """The one runnability answer every surface reports, with the nodes behind it."""

    rows: list[ProcedureNodeSupport] = []
    for node in definition.nodes:
        if node.kind in LINE_NODE_KINDS:
            rows.append(ProcedureNodeSupport(node_id=node.node_id, kind=node.kind, runs_on="line"))
        elif node.kind not in DIRECT_NODE_KINDS:
            rows.append(
                ProcedureNodeSupport(node_id=node.node_id, kind=node.kind, runs_on="nowhere")
            )
        if isinstance(node, RepeatNode):
            rows.extend(
                ProcedureNodeSupport(
                    node_id=f"{node.node_id}.{body.node_id}", kind=body.operation, runs_on="line"
                )
                for body in node.body
                if body.operation != "transform"
            )
    if any(row.runs_on == "nowhere" for row in rows):
        return "unsupported", tuple(rows)
    return ("line" if rows else "direct"), tuple(rows)


def procedure_path(name: str) -> str:
    if not _PROCEDURE_NAME_RE.fullmatch(name):
        raise ProcedureFormatError("Procedure identity is not path-addressable")
    return f"procedures/{name}.json"


def render_procedure(procedure: ProcedureArtifact) -> bytes:
    from cruxible_client.contracts.procedures.wire_v3 import compact_procedure

    return pretty_canonical_bytes(compact_procedure(procedure))


_PROCEDURE_PARSES: ParseMemo[ProcedureArtifact] = ParseMemo(
    capacity=256, max_bytes=16 * 1024 * 1024
)


def parse_procedure(
    content: bytes,
    *,
    path: str,
) -> ProcedureArtifact:
    """Parse once per exact (path, bytes); see ``content_memo``."""
    return _PROCEDURE_PARSES.parse(
        content, path, None, lambda: _parse_procedure(content, path=path)
    )


def _parse_procedure(
    content: bytes,
    *,
    path: str,
) -> ProcedureArtifact:
    try:
        procedure = ProcedureArtifact.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureFormatError("Procedure failed strict versioned validation") from exc
    if procedure_path(procedure.identity.name) != path:
        raise ProcedureFormatError("Procedure identity/path disagreement")
    if render_procedure(procedure) != content:
        raise ProcedureFormatError("Procedure is not in canonical wire form")
    return procedure


@sealed_memo
def procedure_artifact_digest(procedure: ProcedureArtifact) -> ArtifactDigest:
    from cruxible_client.contracts.procedures.wire_v3 import compact_procedure

    return typed_digest(ArtifactDigest, "cruxible-procedure-v4", compact_procedure(procedure))


class AcceptedProcedure(_StrictProcedureArtifactModel):
    path: str
    procedure: ProcedureArtifact
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedProcedure":
        if self.path != procedure_path(self.procedure.identity.name):
            raise ValueError("accepted Procedure path does not reproduce")
        if self.artifact_digest != procedure_artifact_digest(self.procedure).tagged:
            raise ValueError("accepted Procedure digest does not reproduce")
        return self


class ProcedureLawResult(_StrictProcedureArtifactModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> ProcedureLawResult:
    return ProcedureLawResult(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


def evaluate_procedure_law(
    procedure: ProcedureArtifact,
    *,
    path: str,
    predecessor: AcceptedProcedure | None,
    contracts: Mapping[tuple[str, int], str] | None = None,
    contract_lookup: Callable[[ContractRef], Contract | None] | None = None,
    capture_lookup: Callable[[str], CaptureContractAny | None] | None = None,
    enum_members: EnumResolver | None = None,
) -> ProcedureLawResult:
    """Evaluate stable identity, predecessor, and slot signatures.

    ``contracts`` maps each accepted Contract ``(name, version)`` the slots
    name to its digest; every slot must pin both Contracts exactly, and every
    extract its row Contract. With ``contract_lookup`` and ``capture_lookup``
    (candidate Contracts and CaptureContracts by name), each acquiring call's
    captures must match its slot's out Contract and each extract must read a
    CaptureContract that declares its decoding. A Procedure never names a
    provider: a Line binds its slots.
    """

    if path != procedure_path(procedure.identity.name):
        return _refusal(
            "cruxible.procedure.path_mismatch",
            "Procedure identity/path disagreement.",
            path=path,
        )
    if predecessor is None:
        if procedure.lifecycle.predecessor_digest is not None:
            return _refusal(
                "cruxible.procedure.predecessor_missing",
                "A new Procedure cannot name a predecessor.",
                path=path,
            )
    else:
        if procedure.identity != predecessor.procedure.identity:
            return _refusal(
                "cruxible.procedure.stable_identity_changed",
                "A Procedure successor must retain stable identity.",
                path=path,
            )
        if procedure.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.procedure.predecessor_mismatch",
                "Procedure successor does not pin its exact predecessor.",
                path=path,
            )
        if (
            procedure.definition_digest == predecessor.procedure.definition_digest
            and procedure.activation_policy == predecessor.procedure.activation_policy
            and procedure.lifecycle.state == predecessor.procedure.lifecycle.state
        ):
            return _refusal(
                "cruxible.proposal.non_singleton_scope",
                "The proposal changes no registered semantic member.",
                path=path,
            )
    problem = slot_contract_problem(procedure.definition.slots, contracts or {})
    if problem is not None:
        return _refusal("cruxible.procedure.slot_contract_unresolved", problem, path=path)
    problem = extract_row_problem(procedure.definition, contracts or {})
    if problem is not None:
        return _refusal("cruxible.procedure.extract_row_unresolved", problem, path=path)
    if contract_lookup is not None:
        problem = parameter_default_problem(
            procedure.definition, contract_lookup, enum_members=enum_members
        )
        if problem is not None:
            return _refusal("cruxible.procedure.parameter_default_invalid", problem, path=path)
    if capture_lookup is not None:
        from cruxible_client.contracts.procedures.capture_outputs import (
            procedure_capture_problem,
        )

        found = procedure_capture_problem(
            procedure.definition,
            contract_lookup=contract_lookup,
            capture_lookup=capture_lookup,
        )
        if found is not None:
            return _refusal(found[0], found[1], path=path)
    return ProcedureLawResult(
        verdict="accepted",
        artifact_digest=procedure_artifact_digest(procedure).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


def parameter_default_problem(
    definition: ProcedureDefinition,
    contract_lookup: Callable[[ContractRef], Contract | None],
    *,
    enum_members: EnumResolver | None = None,
) -> str | None:
    """Why a parameter default does not fit its params Contract field, or ``None``.

    Contracts stay shape-only: a default lives on the Procedure, and each one
    must be a value of the field it defaults (nested Contracts resolve through
    ``contract_lookup``).
    """

    if not definition.parameter_defaults:
        return None
    reference = definition.parameter_contract
    if reference is None:  # pragma: no cover - the definition model refuses this
        return "parameter_defaults name parameters, and the Procedure declares no params"
    declared = contract_lookup(contract_ref_for_identity(reference.target))
    if declared is None:
        return f"params Contract {reference.target.name} is not accepted"
    from cruxible_client.contracts.provider_operations import (
        OperationValueRefused,
        validate_typed_number_encoding,
    )

    for name, value in sorted(definition.parameter_defaults.items()):
        field = declared.fields.get(name)
        if field is None:
            return (
                f"parameter {name!r} has a default and {declared.name}@{declared.version} "
                "declares no such field"
            )
        checked = check_value(
            value,
            field.type,
            at=f"/{name}",
            field=name,
            resolve=contract_lookup,
            enum_members=enum_members,
        )
        if isinstance(checked, FieldInvalid):
            return (
                f"the default of parameter {name!r} does not fit {field.spelling} "
                f"({declared.name}@{declared.version}): {checked.problem}"
            )
        try:
            validate_typed_number_encoding(value, field.type, resolve=contract_lookup, path=name)
        except OperationValueRefused as exc:
            return f"the default of parameter {name!r} is invalid: {exc}"
    return None


def _node_contracts(definition: ProcedureDefinition) -> tuple[tuple[str, str, ContractRef], ...]:
    """``(label, node id, Contract)`` for every node that pins a Contract version."""

    found: list[tuple[str, str, ContractRef]] = []
    for node in definition.nodes:
        if isinstance(node, ExtractNode):
            found.append(("extract", node.node_id, node.row))
        elif isinstance(node, InboxEgressNode):
            found.append(("post_inbox", node.node_id, node.contract))
    return tuple(found)


def procedure_contract_refs(definition: ProcedureDefinition) -> tuple[ContractRef, ...]:
    """Every Contract version the graph pins: slot signatures, extract rows, inbox items."""

    refs: list[ContractRef] = [
        ref for slot in definition.slots.values() for ref in (slot.in_, slot.out)
    ]
    refs.extend(ref for _label, _node_id, ref in _node_contracts(definition))
    return tuple(refs)


def extract_row_problem(
    definition: ProcedureDefinition, contracts: Mapping[tuple[str, int], str]
) -> str | None:
    """Why a node's Contract (an extract row, an inbox item) is not pinned to its
    accepted version, or ``None``."""

    for label, node_id, ref in _node_contracts(definition):
        accepted = contracts.get((ref.name, ref.version))
        if accepted is None:
            return f"{label} {node_id!r} Contract {ref.spelling} is not accepted"
        if ref.digest != accepted:
            return (
                f"{label} {node_id!r} Contract {ref.spelling} must pin {accepted}, not {ref.digest}"
            )
    return None


def slot_contract_problem(
    slots: Mapping[str, ProcedureSlot], contracts: Mapping[tuple[str, int], str]
) -> str | None:
    """Why a slot's Contracts are not pinned to accepted versions, or ``None``."""

    for name, slot in slots.items():
        for side, ref in (("in", slot.in_), ("out", slot.out)):
            accepted = contracts.get((ref.name, ref.version))
            if accepted is None:
                return f"slot {name!r} {side} Contract {ref.spelling} is not accepted"
            if ref.digest != accepted:
                return (
                    f"slot {name!r} {side} Contract {ref.spelling} must pin {accepted}, "
                    f"not {ref.digest}"
                )
    return None


def acquisition_keys(definition: ProcedureDefinition) -> set[str]:
    """Every capture output, ``<alias>.<field>``: what acquisition rules key."""

    return {
        f"{node.as_}.{field}"
        for node in definition.nodes
        if isinstance(node, CallNode)
        for field in node.captures
    }


__all__ = [
    "DIRECT_NODE_KINDS",
    "acquisition_keys",
    "LINE_NODE_KINDS",
    "AcceptedProcedure",
    "ProcedureNodeSupport",
    "ProcedureRunnable",
    "procedure_runnability",
    "ProcedureArtifact",
    "ProcedureFormatError",
    "ProcedureLawResult",
    "evaluate_procedure_law",
    "parse_procedure",
    "procedure_artifact_digest",
    "procedure_contract_refs",
    "procedure_path",
    "slot_contract_problem",
    "LineAcquisitionRule",
    "render_procedure",
]
