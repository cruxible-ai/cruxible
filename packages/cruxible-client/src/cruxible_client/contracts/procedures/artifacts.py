"""Governed Procedure envelopes and acceptance laws."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    canonical_bytes,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.content_memo import ParseMemo, sealed_memo
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionDefault,
    validate_acquisition_keys,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import (
    compute_procedure_definition_digest,
    procedure_guard_halt_targets,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureDefinition,
    RepeatNode,
    SourceNode,
    iter_pin_bindings,
)
from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.semantic import SemanticAddress

_PROCEDURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class ProcedureFormatError(FormatError):
    """A Procedure artifact or canonical path is invalid."""


class _StrictProcedureArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


class ProcedureOwnedContract(_StrictProcedureArtifactModel):
    """A closed Contract artifact carried by exactly one Procedure envelope."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    tag: Literal["playbill-procedure-owned-contract-v1"] = "playbill-procedure-owned-contract-v1"
    identity: ArtifactIdentity
    contract_schema: ContractSchema = Field(alias="schema")

    @field_validator("contract_schema", mode="before")
    @classmethod
    def _closed_schema(cls, value: object) -> object:
        if isinstance(value, dict) and set(value) - {
            "description",
            "fields",
            "allow_extra",
        }:
            raise ValueError("owned Contract schema contains unknown fields")
        if isinstance(value, dict) and isinstance(value.get("fields"), dict):
            for name, field in value["fields"].items():
                if isinstance(field, dict) and set(field) - set(PropertySchema.model_fields):
                    raise ValueError(f"owned Contract field {name!r} contains unknown fields")
        return value

    @model_validator(mode="after")
    def _contract_identity(self) -> "ProcedureOwnedContract":
        if self.identity.kind != "Contract":
            raise ValueError("owned Contract identity must use kind Contract")
        return self


def procedure_owned_contract_digest(contract: ProcedureOwnedContract) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-owned-contract-v1",
        {
            "identity": contract.identity.model_dump(mode="json"),
            "schema": contract.contract_schema.model_dump(mode="json"),
        },
    )


def _owned_contract_key(contract: ProcedureOwnedContract) -> bytes:
    return canonical_bytes(contract.model_dump(mode="json", by_alias=True))


class _GraphEnvelope(_StrictProcedureArtifactModel):
    """A Procedure's envelope: one graph and the Contracts it carries."""

    identity: ArtifactIdentity
    definition: ProcedureDefinition
    definition_digest: str
    pins: tuple[ArtifactPin, ...]
    owned_contracts: tuple[ProcedureOwnedContract, ...]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("definition_digest")
    @classmethod
    def _definition_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("pins", check_fields=False)
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("Procedure pins must be canonically sorted")
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(set(keys)) != len(keys):
            raise ValueError("Procedure pins must be unique by role and target")
        return value

    @field_validator("owned_contracts")
    @classmethod
    def _owned_contracts(
        cls,
        value: tuple[ProcedureOwnedContract, ...],
    ) -> tuple[ProcedureOwnedContract, ...]:
        if value != tuple(sorted(value, key=_owned_contract_key)):
            raise ValueError("owned Contracts must be canonically byte-sorted")
        identities = tuple(contract.identity.qualified for contract in value)
        digests = tuple(procedure_owned_contract_digest(contract).tagged for contract in value)
        if len(set(identities)) != len(identities):
            raise ValueError("owned Contracts must be unique by identity")
        if len(set(digests)) != len(digests):
            raise ValueError("owned Contracts must be unique by digest")
        return value

    def _check_envelope(self, kind: str) -> None:
        if getattr(self, "artifact_format", None) != "cruxible-procedure-v3":
            # The frozen v2/v1 validators refused literal-null operands. The
            # newly usable arm belongs to v3; old receipts keep that boundary.
            for node in self.definition.nodes:
                if node.kind == "guard" and (
                    getattr(node, "refusal_code", None) is None
                    or getattr(node, "message", None) is None
                ):
                    raise ValueError(
                        "omitted guard diagnostics require Procedure v3; upgrade the compiler"
                    )
                if node.kind == "halt" and getattr(node, "message", None) is not None:
                    raise ValueError("halt messages require Procedure v3; upgrade the compiler")
                pending = [
                    predicate
                    for predicate in (
                        getattr(node, "predicate", None),
                        getattr(node, "until", None),
                    )
                    if predicate is not None
                ]
                while pending:
                    predicate = pending.pop()
                    if any(
                        operand is not None and operand.kind == "literal" and operand.value is None
                        for operand in (predicate.left, predicate.right)
                    ):
                        raise ValueError(
                            "literal null operands require Procedure v3; upgrade the compiler"
                        )
                    pending.extend(
                        predicate.all_of
                        or predicate.any_of
                        or ((predicate.not_of,) if predicate.not_of else ())
                    )

        if self.identity.kind != kind or not _PROCEDURE_NAME_RE.fullmatch(self.identity.name):
            raise ValueError(f"{kind} identity must be path-addressable")
        if self.definition.name != self.identity.name:
            raise ValueError(f"{kind} definition name must match stable artifact identity")
        expected = compute_procedure_definition_digest(self.definition).tagged
        if self.definition_digest != expected:
            raise ValueError(f"{kind} definition_digest does not reproduce its graph format")
        declared_exact = {
            (pin.role, pin.target.qualified, pin.artifact_digest) for pin in self.pins
        }
        referenced_bindings = tuple(
            binding
            for binding in iter_pin_bindings(self.definition)
            if isinstance(binding, ArtifactPin)
        )
        referenced_exact = {
            (binding.role, binding.target.qualified, binding.artifact_digest)
            for binding in referenced_bindings
        }
        if not referenced_exact.issubset(declared_exact):
            raise ValueError(f"{kind} definition contains exact pins absent from its envelope")

        contracts = {
            contract.identity.qualified: procedure_owned_contract_digest(contract).tagged
            for contract in self.owned_contracts
        }
        referenced_contracts = tuple(
            binding for binding in referenced_bindings if binding.target.kind == "Contract"
        )
        for binding in referenced_contracts:
            if contracts.get(binding.target.qualified) != binding.artifact_digest:
                raise ValueError("exact Contract binding does not resolve to its owned Contract")
        referenced_contract_keys = {
            (binding.target.qualified, binding.artifact_digest) for binding in referenced_contracts
        }
        if referenced_contract_keys != set(contracts.items()):
            raise ValueError(f"every owned Contract must be referenced exactly by the {kind}")
        declared_contract_keys = {
            (pin.target.qualified, pin.artifact_digest)
            for pin in self.pins
            if pin.target.kind == "Contract"
        }
        if declared_contract_keys != referenced_contract_keys:
            raise ValueError(f"{kind} envelope contains an unreferenced Contract pin")


class ProcedureArtifact(_GraphEnvelope):
    """Procedure envelope whose Contract closure rides with its owner.

    ``acquisition_defaults`` holds the Procedure's acquisition rule per Source
    alias (``max_age``, ``on_unavailable``); a Line's ``acquisition`` overrides
    them field by field.
    """

    artifact_format: Literal["playbill-procedure-v2"] = "playbill-procedure-v2"
    acquisition_defaults: dict[str, AcquisitionDefault] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )

    _defaults = field_validator("acquisition_defaults")(validate_acquisition_keys)

    @model_validator(mode="after")
    def _correspondence(self) -> "ProcedureArtifact":
        self._check_envelope("Procedure")
        aliases = {node.as_ for node in self.definition.nodes if isinstance(node, SourceNode)}
        unknown = sorted(set(self.acquisition_defaults) - aliases)
        if unknown:
            raise ValueError(f"acquisition_defaults name no Source alias: {unknown}")
        if getattr(self, "artifact_format") == "cruxible-procedure-v3":
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


def _check_named_envelope(envelope: _GraphEnvelope, kind: str) -> None:
    if envelope.identity.kind != kind or not _PROCEDURE_NAME_RE.fullmatch(envelope.identity.name):
        raise ValueError(f"{kind} identity must be path-addressable")
    if envelope.definition.name != envelope.identity.name:
        raise ValueError(f"{kind} definition name must match its identity")
    if (
        envelope.definition_digest
        != compute_procedure_definition_digest(envelope.definition).tagged
    ):
        raise ValueError(f"{kind} definition digest does not reproduce")
    contracts = {contract.identity for contract in envelope.owned_contracts}
    referenced = {
        binding.target
        for binding in iter_pin_bindings(envelope.definition)
        if isinstance(binding, ArtifactNameRef) and binding.target.kind == "Contract"
    }
    if referenced != contracts:
        raise ValueError("every owned Contract must be referenced by name exactly within its owner")


class NamedProcedureArtifact(ProcedureArtifact):
    """Revision-36 writer: dependencies resolve at the accepted coordinate."""

    artifact_format: Literal["cruxible-procedure-v3"] = "cruxible-procedure-v3"  # type: ignore[assignment]
    definition: NamedProcedureDefinition
    pins: ClassVar[tuple[ArtifactPin, ...]] = ()  # type: ignore[misc]

    @model_validator(mode="before")
    @classmethod
    def _stored_v3(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("artifact_format") == "cruxible-procedure-v3":
            definition = value.get("definition")
            if isinstance(definition, dict) and any(
                isinstance(node, dict) and "id" in node for node in definition.get("nodes", ())
            ):
                from cruxible_client.contracts.procedures.wire_v3 import expand_procedure

                return expand_procedure(value)
        return value

    def _check_envelope(self, kind: str) -> None:
        _check_named_envelope(self, kind)


#: How an accepted Procedure can run: on its own (``procedure run``), only as a
#: Line (its terminals act outward under a Line's authority), or not at all
#: (an exhaust tap: no v1 path admits one).
ProcedureRunnable = Literal["direct", "line", "unsupported"]

#: Node kinds the direct run lane executes.
DIRECT_NODE_KINDS = frozenset(
    {
        "state_tap",
        "state_claim",
        "transform",
        "project",
        "guard",
        "repeat",
        "halt",
        "source",
        "call",
        "select",
        "constant",
        "return",
        "invoke",
    }
)
#: Node kinds only a Line runs: they egress under the Line's authority.
LINE_NODE_KINDS = frozenset(
    {"emit_capture", "post_inbox", "propose_change_set", "settle_change_set"}
)


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


ProcedureArtifactAny = Annotated[
    ProcedureArtifact | NamedProcedureArtifact, Field(discriminator="artifact_format")
]
_PROCEDURE_ADAPTER: TypeAdapter[ProcedureArtifactAny] = TypeAdapter(ProcedureArtifactAny)


def procedure_path(name: str) -> str:
    if not _PROCEDURE_NAME_RE.fullmatch(name):
        raise ProcedureFormatError("Procedure identity is not path-addressable")
    return f"procedures/{name}.json"


def render_procedure(procedure: ProcedureArtifact) -> bytes:
    if getattr(procedure, "artifact_format") == "cruxible-procedure-v3":
        from cruxible_client.contracts.procedures.wire_v3 import compact_procedure

        return pretty_canonical_bytes(compact_procedure(procedure))
    return pretty_canonical_bytes(procedure.model_dump(mode="json", by_alias=True))


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
        document = json.loads(content)
        if document.get("artifact_format") == "cruxible-procedure-v3":
            from cruxible_client.contracts.procedures.wire_v3 import expand_procedure

            document = expand_procedure(document)
        procedure = _PROCEDURE_ADAPTER.validate_python(document)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureFormatError("Procedure failed strict versioned validation") from exc
    if procedure_path(procedure.identity.name) != path:
        raise ProcedureFormatError("Procedure identity/path disagreement")
    if render_procedure(procedure) != content:
        raise ProcedureFormatError("Procedure is not in canonical wire form")
    return procedure


@sealed_memo
def procedure_artifact_digest(procedure: ProcedureArtifact) -> ArtifactDigest:
    if getattr(procedure, "artifact_format") == "cruxible-procedure-v3":
        from cruxible_client.contracts.procedures.wire_v3 import compact_procedure

        return typed_digest(ArtifactDigest, "cruxible-procedure-v3", compact_procedure(procedure))
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        procedure.model_dump(mode="json", by_alias=True),
    )


class AcceptedProcedure(_StrictProcedureArtifactModel):
    path: str
    procedure: ProcedureArtifactAny
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
    by_name: bool = False,
) -> ProcedureLawResult:
    """Evaluate stable identity, predecessor, and slot signatures.

    ``contracts`` maps each accepted Contract ``(name, version)`` the slots
    name to its digest; every slot must pin both Contracts exactly. A Procedure
    never names a provider: a Line binds its slots.
    """

    del by_name
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
            and procedure.pins == predecessor.procedure.pins
            and procedure.owned_contracts == predecessor.procedure.owned_contracts
            and procedure.acquisition_defaults == predecessor.procedure.acquisition_defaults
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
    return ProcedureLawResult(
        verdict="accepted",
        artifact_digest=procedure_artifact_digest(procedure).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


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


def provider_occurrences(definition: ProcedureDefinition) -> tuple[tuple[str, object], ...]:
    """Every Source/Call occurrence, repeat bodies included, keyed by occurrence id."""

    occurrences: list[tuple[str, object]] = []
    for node in definition.nodes:
        if isinstance(node, SourceNode | CallNode):
            occurrences.append((node.node_id, node))
        elif isinstance(node, RepeatNode):
            occurrences.extend(
                (f"{node.node_id}.{body.node_id}", body)
                for body in node.body
                if body.operation == "call"
            )
    return tuple(sorted(occurrences, key=lambda item: item[0].encode("utf-8")))


__all__ = [
    "DIRECT_NODE_KINDS",
    "LINE_NODE_KINDS",
    "AcceptedProcedure",
    "ProcedureNodeSupport",
    "ProcedureRunnable",
    "procedure_runnability",
    "ProcedureArtifact",
    "ProcedureFormatError",
    "ProcedureLawResult",
    "ProcedureOwnedContract",
    "evaluate_procedure_law",
    "parse_procedure",
    "procedure_artifact_digest",
    "procedure_owned_contract_digest",
    "procedure_path",
    "provider_occurrences",
    "slot_contract_problem",
    "AcquisitionDefault",
    "render_procedure",
]
