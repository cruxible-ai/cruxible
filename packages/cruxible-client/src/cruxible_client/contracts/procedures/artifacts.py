"""Governed Procedure envelopes and acceptance laws."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Literal

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
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    canonical_bytes,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.closure import ProcedureSlotBinding
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureDefinition,
    ProcedurePinSlotRef,
    RepeatNode,
    SourceNode,
    iter_pin_bindings,
)
from cruxible_client.contracts.provider_contracts import ProviderOperationContract
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
)
from cruxible_client.contracts.providers import AcceptedProvider, ProviderV2
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
    """What a Procedure and a Blueprint share: one graph and the Contracts it carries."""

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

    @field_validator("pins")
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
        declared_slots = {slot.slot_name for slot in self.definition.pin_slots}
        referenced_slots = set(self.definition.open_slots)
        if not referenced_slots.issubset(declared_slots):
            raise ValueError(f"{kind} definition references undeclared slots")

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


class BlueprintOrigin(_StrictProcedureArtifactModel):
    """The Blueprint a Procedure was instantiated from, and the Provider bound per slot."""

    tag: Literal["cruxible-blueprint-origin-v1"] = "cruxible-blueprint-origin-v1"
    blueprint: ArtifactPin
    bindings: tuple[ProcedureSlotBinding, ...]

    @model_validator(mode="after")
    def _shape(self) -> "BlueprintOrigin":
        if self.blueprint.role != "blueprint" or self.blueprint.target.kind != "Blueprint":
            raise ValueError("a Blueprint origin pins its Blueprint with role blueprint")
        names = tuple(item.slot_name for item in self.bindings)
        if not names or names != tuple(sorted(set(names), key=lambda item: item.encode())):
            raise ValueError("Blueprint bindings must be nonempty, sorted and unique by slot")
        for item in self.bindings:
            if item.artifact_pin.target.kind != "Provider" or item.artifact_pin.role != "provider":
                raise ValueError("a Blueprint slot binds one exact Provider")
        return self


class ProcedureArtifact(_GraphEnvelope):
    """Procedure envelope whose Contract closure rides with its owner.

    ``blueprint`` records the Blueprint it was instantiated from and the exact
    Provider bound to each slot; it is provenance, not a dependency.
    """

    artifact_format: Literal["playbill-procedure-v2"] = "playbill-procedure-v2"
    blueprint: BlueprintOrigin | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _correspondence(self) -> "ProcedureArtifact":
        self._check_envelope("Procedure")
        return self


class BlueprintArtifact(_GraphEnvelope):
    """A Procedure skeleton: the same definition with interface-typed Provider slots open.

    Not runnable. Instantiating it binds one compatible accepted Provider per
    slot and yields an ordinary Procedure that records this Blueprint and its
    bindings.
    """

    artifact_format: Literal["cruxible-blueprint-v1"] = "cruxible-blueprint-v1"

    @model_validator(mode="after")
    def _correspondence(self) -> "BlueprintArtifact":
        self._check_envelope("Blueprint")
        slots = {slot.slot_name: slot for slot in self.definition.pin_slots}
        if not slots:
            raise ValueError("a Blueprint declares at least one open Provider slot")
        if set(self.definition.open_slots) != set(slots):
            raise ValueError("every declared Blueprint slot must be used by a node")
        for slot in slots.values():
            if slot.artifact_kind != "Provider" or slot.pin_role != "provider":
                raise ValueError("a Blueprint slot is an interface-typed Provider slot")
        provider_slots = {
            getattr(occurrence, "provider").slot_name
            for _occurrence_id, occurrence in provider_occurrences(self.definition)
            if isinstance(getattr(occurrence, "provider"), ProcedurePinSlotRef)
        }
        if provider_slots != set(slots):
            raise ValueError("Blueprint slots stand only in Provider positions")
        return self


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
    if definition.open_slots or definition.pin_slots:
        rows.append(ProcedureNodeSupport(node_id="procedure", kind="open_slot", runs_on="nowhere"))
    if any(row.runs_on == "nowhere" for row in rows):
        return "unsupported", tuple(rows)
    return ("line" if rows else "direct"), tuple(rows)


_PROCEDURE_ADAPTER: TypeAdapter[ProcedureArtifact] = TypeAdapter(ProcedureArtifact)


def procedure_path(name: str) -> str:
    if not _PROCEDURE_NAME_RE.fullmatch(name):
        raise ProcedureFormatError("Procedure identity is not path-addressable")
    return f"procedures/{name}.json"


def render_procedure(procedure: ProcedureArtifact) -> bytes:
    return pretty_canonical_bytes(procedure.model_dump(mode="json", by_alias=True))


def parse_procedure(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProcedureArtifact:
    try:
        procedure = _PROCEDURE_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureFormatError("Procedure failed strict versioned validation") from exc
    if not artifact_path_matches(procedure_path(procedure.identity.name), path, codec=codec):
        raise ProcedureFormatError("Procedure identity/path disagreement")
    if artifact_bytes_for_path(render_procedure(procedure), path, codec=codec) != content:
        raise ProcedureFormatError("Procedure is not in canonical wire form")
    return procedure


def procedure_artifact_digest(procedure: ProcedureArtifact) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        procedure.model_dump(mode="json", by_alias=True),
    )


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
    providers: Mapping[str, AcceptedProvider] | None = None,
    provider_interfaces: Mapping[
        str,
        AcceptedProviderInterfaceRegistration,
    ]
    | None = None,
) -> ProcedureLawResult:
    """Evaluate stable identity, predecessor, and exact closure."""

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
            and procedure.lifecycle.state == predecessor.procedure.lifecycle.state
        ):
            return _refusal(
                "cruxible.proposal.non_singleton_scope",
                "The proposal changes no registered semantic member.",
                path=path,
            )
    slots = sorted(
        {*procedure.definition.open_slots, *(s.slot_name for s in procedure.definition.pin_slots)}
    )
    if slots:
        return _refusal(
            "cruxible.procedure.open_slots",
            "A Procedure pins every Provider exactly; open slots belong only to a "
            f"Blueprint. Open: {', '.join(slots)}.",
            path=path,
        )
    provider_refusal = _evaluate_provider_pins(
        procedure.definition,
        procedure=procedure,
        providers={} if providers is None else providers,
        provider_interfaces=({} if provider_interfaces is None else provider_interfaces),
    )
    if provider_refusal is not None:
        code, message = provider_refusal
        return _refusal(code, message, path=path)
    return ProcedureLawResult(
        verdict="accepted",
        artifact_digest=procedure_artifact_digest(procedure).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


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


def _evaluate_provider_pins(
    definition: ProcedureDefinition,
    *,
    procedure: ProcedureArtifact,
    providers: Mapping[str, AcceptedProvider],
    provider_interfaces: Mapping[str, AcceptedProviderInterfaceRegistration],
) -> tuple[str, str] | None:
    for occurrence_id, occurrence in provider_occurrences(definition):
        interface_pin = getattr(occurrence, "interface")
        interface_digest = getattr(occurrence, "interface_digest")
        accepted_interface = provider_interfaces.get(interface_pin.artifact_digest)
        if accepted_interface is None or (
            accepted_interface.registration.identity != interface_pin.target
            or accepted_interface.registration.interface_digest != interface_digest
        ):
            return (
                "cruxible.procedure.provider_interface_pin_mismatch",
                f"Provider occurrence {occurrence_id!r} does not bind its exact interface.",
            )
        try:
            check_provider_node_contract(occurrence, accepted_interface, procedure)
        except (ValueError, KeyError) as exc:
            return ("cruxible.procedure.provider_interface_pin_mismatch", str(exc))
        provider_binding = getattr(occurrence, "provider")
        if isinstance(provider_binding, ProcedurePinSlotRef):
            continue
        accepted_provider = providers.get(provider_binding.artifact_digest)
        if accepted_provider is None or (
            accepted_provider.provider.identity != provider_binding.target
            or not isinstance(accepted_provider.provider, ProviderV2)
        ):
            return (
                "cruxible.procedure.provider_runtime_manifest_required",
                f"Provider occurrence {occurrence_id!r} requires an accepted Provider v2.",
            )
        implementation_digest = getattr(occurrence, "implementation_digest")
        matches = tuple(
            row
            for row in accepted_provider.provider.implementations
            if row.implementation_digest == implementation_digest
            and row.interface_id == accepted_interface.registration.interface_id
            and row.interface_digest == interface_digest
        )
        if not matches:
            return (
                "cruxible.procedure.provider_implementation_unavailable",
                f"Provider occurrence {occurrence_id!r} implementation is unavailable.",
            )
        if len(matches) != 1:
            return (
                "cruxible.procedure.provider_implementation_ambiguous",
                f"Provider occurrence {occurrence_id!r} implementation is ambiguous.",
            )
    return None


def check_provider_node_contract(
    node: object,
    interface: AcceptedProviderInterfaceRegistration,
    procedure: ProcedureArtifact,
) -> ProviderOperationContract | None:
    """Check specialization and exact operation schemas before materialization.

    The first ``workspace.file`` interface revision declares no operation
    contract: the daemon owns that read and its receipt, so a Source over it is
    checked by the host protocol, not here, and has no operation contract.
    """
    from cruxible_client.contracts.provider_contracts import (
        ACQUISITION_RESULT,
        operation_schema_shape,
        read_provider_operation_contract,
    )
    from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_DIGEST

    if (
        isinstance(node, SourceNode)
        and interface.registration.interface_digest == WORKSPACE_FILE_INTERFACE_DIGEST
    ):
        return None
    contract = read_provider_operation_contract(interface.registration.interface_bytes_hex)
    declared_effect = json.loads(bytes.fromhex(interface.registration.interface_bytes_hex)).get(
        "effect_class"
    )
    # Package registrations retain the provider contract bytes. Package metadata
    # spells a no-effect operation "pure"; the governed effect class is "none".
    from cruxible_client.contracts.provider_interfaces import ProviderInterfaceRegistration

    if (
        isinstance(interface.registration, ProviderInterfaceRegistration)
        and declared_effect == "pure"
    ):
        declared_effect = "none"
    if declared_effect != interface.registration.effect_class:
        raise ValueError("ProviderInterface effect class differs from its operation declaration")
    if isinstance(node, SourceNode):
        from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_V2_DIGEST

        # The daemon owns workspace reads and wraps the structured byte result
        # with its independently retained source-read receipt. Generic Sources
        # must themselves return the external acquisition envelope.
        workspace_read = (
            interface.registration.interface_digest == WORKSPACE_FILE_INTERFACE_V2_DIGEST
        )
        if contract.output != ACQUISITION_RESULT and not workspace_read:
            raise ValueError("Source requires the shared external acquisition result contract")
        if interface.registration.effect_class == "external_mutation":
            raise ValueError("Source cannot invoke an external mutation")
        return contract
    owned = {
        procedure_owned_contract_digest(item).tagged: item for item in procedure.owned_contracts
    }
    for field, expected in (("contract_in", contract.input), ("contract_out", contract.output)):
        binding = getattr(node, field)
        if isinstance(binding, ProcedurePinSlotRef):
            continue  # A Blueprint slot: checked when the Blueprint is instantiated.
        carried = owned.get(binding.artifact_digest)
        if (
            carried is None
            or carried.identity != binding.target
            or isinstance(expected, str)
            or operation_schema_shape(carried.contract_schema) != operation_schema_shape(expected)
        ):
            raise ValueError(
                f"Call {field} does not match the ProviderInterface operation contract"
            )
    return contract


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
    "render_procedure",
]
