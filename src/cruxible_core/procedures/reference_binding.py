"""Request-owned execution plans, resolved at one immutable accepted coordinate.

These are execution data, never governed artifacts. Authored bytes and their
identity remain separate from the exact pins recorded by run admission.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SerializeAsAny, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.dependencies import EXACT_REGISTRY_TARGET_KINDS, dependency_pins
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedures.acquisition_rules import AcquisitionDefault
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureDefinition,
    iter_pin_bindings,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.provider_operations import (
    ContractLookup,
    LineBinding,
    ProviderOperation,
    contract_closure,
    operation_signature_digest,
)
from cruxible_client.contracts.providers import ProviderV2
from cruxible_client.contracts.shapes import Contract

ReferenceLookup = Callable[[ArtifactIdentity], tuple[Any, str] | None]
_FIELDS = {
    "contract_in": "contract-in",
    "contract_out": "contract-out",
    "parameter_contract": "parameter-contract",
    "query": "query",
    "reducer_or_query": "query",
    "capture_contract": "capture-contract",
    "claim_type": "claim-type",
    "claim_types": "claim-type",
    "procedure": "procedure",
    "effect_policy": "effect-policy",
}
_NODE_FIELDS = {
    "state_tap": ("query",),
    "source": ("capture_contract",),
    "call": ("effect_policy",),
    "exhaust_tap": ("reducer_or_query",),
    "transform": ("contract_in", "contract_out"),
    "project": ("contract_out",),
    "select": ("contract_out",),
    "return": ("contract_out",),
    "constant": ("contract_out",),
    "emit_capture": ("capture_contract",),
    "state_claim": ("claim_type",),
    "invoke": ("procedure",),
    "propose_change_set": ("claim_types",),
    "settle_change_set": ("claim_types",),
}


class ResolvedSlotBinding(BaseModel):
    """One slot of an execution plan, bound to its exact build and operation.

    ``contracts`` is the exact Contract closure the operation pins, resolved at
    admission; it is empty in a replayed plan (a replay reads the recorded
    acquisition plan, which carries its own closure).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    slot: str
    build: ArtifactPin
    operation: ProviderOperation
    operation_signature_digest: str
    contracts: tuple[Contract, ...] = ()

    @property
    def build_digest(self) -> str:
        return self.build.artifact_digest


class ExecutionDefinition(ProcedureDefinition):
    _authored: ProcedureDefinition = PrivateAttr()


class ProcedureExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    identity: ArtifactIdentity
    definition: ExecutionDefinition
    definition_digest: str
    pins: tuple[ArtifactPin, ...]
    owned_contracts: tuple[ProcedureOwnedContract, ...]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    acquisition_defaults: dict[str, AcquisitionDefault]
    #: Each bound slot, by slot name: the run uses these and never refits.
    slot_bindings: tuple[ResolvedSlotBinding, ...] = ()
    lifecycle: ArtifactLifecycle

    def binding_for(self, slot: str) -> ResolvedSlotBinding | None:
        return next((item for item in self.slot_bindings if item.slot == slot), None)


class BoundAcceptedProcedure(AcceptedProcedure):
    procedure: ProcedureExecutionPlan  # type: ignore[assignment]
    authored: SerializeAsAny[ProcedureArtifact] = Field(exclude=True)

    @model_validator(mode="after")
    def _binding(self) -> BoundAcceptedProcedure:
        if self.path != "procedures/" + self.authored.identity.name + ".json":
            raise ValueError("bound Procedure path does not reproduce")
        if self.artifact_digest != procedure_artifact_digest(self.authored).tagged:
            raise ValueError("bound Procedure identity does not reproduce authored bytes")
        if (self.procedure.identity, self.procedure.definition_digest) != (
            self.authored.identity,
            self.authored.definition_digest,
        ):
            raise ValueError("execution plan must retain authored identity metadata")
        return self


def authored_procedure(accepted: AcceptedProcedure) -> ProcedureArtifact:
    return accepted.authored if isinstance(accepted, BoundAcceptedProcedure) else accepted.procedure


def validate_slot_names(bindings: Iterable[str], declared: Iterable[str]) -> None:
    """Reject unknown keys before resolving any binding."""
    declared = set(declared)
    unknown = set(bindings) - declared
    if unknown:
        from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
        from cruxible_core.errors import RequestRefusedError

        raise RequestRefusedError(
            "cruxible.procedure.slot_undeclared",
            f"Unknown slots: {sorted(unknown)}; declared slots: {sorted(declared)}.",
            repair=HandEditRepair(
                hand_edit=HandEditInstruction.model_validate(
                    {
                        "target": "bindings",
                        "required_change": f"Bind only declared slots: {sorted(declared)}.",
                    }
                )
            ),
        )


def bind_procedure_references(
    accepted: AcceptedProcedure,
    *,
    lookup: ReferenceLookup,
    bindings: Mapping[str, LineBinding] | None = None,
    contracts: ContractLookup | None = None,
    frozen_pins: tuple[ArtifactPin, ...] | None = None,
    partial: bool = False,
) -> BoundAcceptedProcedure:
    """Resolve one graph, with one lookup per distinct dependency identity.

    ``lookup`` must be bound to the admission's accepted coordinate. Recovery
    supplies that recorded coordinate, never the head. ``bindings`` holds each
    slot's exact ``{build, operation}`` (the Line's, or a direct run's
    ephemeral spec); nothing is fitted here. ``contracts`` resolves the bound
    operations' Contract closures; a replay passes none. With ``partial`` a
    slot may be left unbound (a replay of a slot only supplied Sources used).
    Payloads, schemas and retained source snapshots are not traversed.
    """
    authored = authored_procedure(accepted)
    if frozen_pins is not None and {
        pin for pin in frozen_pins if pin.target.kind in EXACT_REGISTRY_TARGET_KINDS
    } != {pin for pin in authored.pins if pin.target.kind in EXACT_REGISTRY_TARGET_KINDS}:
        raise ExecutionError("Recorded registry pins differ from the authored exact pins")
    resolved = {
        c.identity.qualified: (c, procedure_owned_contract_digest(c).tagged)
        for c in authored.owned_contracts
    }

    def resolve(identity: ArtifactIdentity) -> tuple[Any, str]:
        if identity.qualified not in resolved:
            selected = lookup(identity)
            if (
                selected is None
                or getattr(getattr(selected[0], "lifecycle", None), "state", "live") != "live"
            ):
                raise ExecutionError(f"Procedure dependency {identity.qualified!r} is unavailable")
            resolved[identity.qualified] = selected
        return resolved[identity.qualified]

    slot_declarations: dict[str, ProcedureSlot] = dict(authored.definition.slots)
    validate_slot_names(bindings or {}, slot_declarations)
    data = authored.definition.model_dump(mode="json", by_alias=True)
    data.pop("source", None)

    def reference(value: Any, field: str) -> Any:
        if value is None:
            return None
        if isinstance(value, list):
            return [reference(item, field) for item in value]
        identity = ArtifactIdentity.model_validate(value["target"])
        if identity.kind in EXACT_REGISTRY_TARGET_KINDS:
            if "artifact_digest" not in value:
                raise ExecutionError(
                    f"Registry dependency {identity.qualified!r} requires an exact pin"
                )
            # No ledger owner exists for this kind. Its owning registry law
            # checks the digest; admission and replay retain the same bytes.
            return ArtifactPin.model_validate(value).model_dump(mode="json")
        _, digest = resolve(identity)
        return ArtifactPin(role=_FIELDS[field], target=identity, artifact_digest=digest).model_dump(
            mode="json"
        )

    for field in ("contract_in", "contract_out", "parameter_contract"):
        if field in data:
            data[field] = reference(data[field], field)
    for node in data["nodes"]:
        for field in _NODE_FIELDS.get(node["kind"], ()):
            if field in node:
                node[field] = reference(node[field], field)
        for body in node.get("body", ()):
            for field in ("contract_in", "contract_out", "effect_policy"):
                if field in body:
                    body[field] = reference(body[field], field)
    data.update(graph_format=6)
    definition = ExecutionDefinition.model_validate(data)
    definition._authored = authored.definition
    slot_bindings: list[ResolvedSlotBinding] = []
    for name, slot in slot_declarations.items():
        binding = (bindings or {}).get(name)
        if binding is None:
            if partial:
                continue
            from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
            from cruxible_core.errors import RequestRefusedError

            raise RequestRefusedError(
                "cruxible.line.slot_unbound",
                f"Slot {name!r} ({slot.spelling()}) has no binding.",
                repair=HandEditRepair(
                    hand_edit=HandEditInstruction(
                        target="bindings",
                        required_change=f"Bind {name} to a build#operation serving it.",
                    )
                ),
            )
        provider, digest = resolve(binding.build.target)
        if digest != binding.build.artifact_digest or not isinstance(provider, ProviderV2):
            raise ExecutionError(
                f"Slot {name!r} binds {binding.spelling} at {binding.build.artifact_digest}, "
                "which is not the live accepted build at this coordinate"
            )
        operation = next(
            (item for item in provider.operations if item.name == binding.operation), None
        )
        if operation is None:
            raise ExecutionError(f"Slot {name!r}: {binding.spelling} declares no such operation")
        try:
            fits = operation.signature().same_as(slot)
        except ValueError:
            fits = False
        if not fits:
            raise ExecutionError(
                f"Slot {name!r} needs {slot.spelling()}; {binding.spelling} serves "
                f"{operation.signature().spelling()}"
            )
        closure: tuple[Contract, ...] = ()
        if contracts is not None:
            found = contract_closure(operation.contract_refs(), contracts)
            if found is None:
                raise ExecutionError(
                    f"Slot {name!r}: a Contract {binding.spelling} pins is not accepted"
                )
            closure = found
        slot_bindings.append(
            ResolvedSlotBinding(
                slot=name,
                build=binding.build,
                operation=operation,
                operation_signature_digest=operation_signature_digest(operation.signature()),
                contracts=closure,
            )
        )
    pins = {p for p in iter_pin_bindings(definition) if isinstance(p, ArtifactPin)}
    for pin in dependency_pins(authored) if frozen_pins is None else frozen_pins:
        if pin.target.kind in EXACT_REGISTRY_TARGET_KINDS:
            if not isinstance(pin, ArtifactPin):
                raise ExecutionError(
                    f"Registry dependency {pin.target.qualified!r} requires an exact pin"
                )
            pins.add(pin)
            continue
        if frozen_pins is not None and pin.target.kind == "Provider" and pin.role == "provider":
            # A replay keeps every build the admission bound, including one only
            # a supplied Source would have used.
            assert isinstance(pin, ArtifactPin)
            pins.add(pin)
            continue
        if pin.target.kind != "Contract":
            _, digest = resolve(pin.target)
            pins.add(ArtifactPin(role=pin.role, target=pin.target, artifact_digest=digest))
    pins.update(item.build for item in slot_bindings)
    if frozen_pins is not None and pins != set(frozen_pins):
        raise ExecutionError("Recorded admission pins do not reproduce the execution plan")
    plan = ProcedureExecutionPlan(
        identity=authored.identity,
        definition=definition,
        definition_digest=authored.definition_digest,
        pins=tuple(sorted(pins, key=lambda p: (p.role, p.target.qualified, p.artifact_digest))),
        owned_contracts=authored.owned_contracts,
        activation_policy=authored.activation_policy,
        acquisition_defaults=dict(authored.acquisition_defaults),
        slot_bindings=tuple(slot_bindings),
        lifecycle=authored.lifecycle,
    )
    return BoundAcceptedProcedure(
        path=accepted.path,
        procedure=plan,
        authored=authored,
        artifact_digest=accepted.artifact_digest,
    )


def replay_procedure_references(
    accepted: AcceptedProcedure,
    *,
    pins: tuple[ArtifactPin, ...],
    lookup_exact: Callable[[ArtifactIdentity, str], tuple[Any, str] | None],
    bindings: Mapping[str, LineBinding],
) -> BoundAcceptedProcedure:
    """Rebuild from recorded digests and the recorded per-slot bindings.

    There is no accepted-head/name resolver and no selection by fit.
    """
    versions: dict[ArtifactIdentity, str] = {}
    for pin in pins:
        if pin.target in versions and versions[pin.target] != pin.artifact_digest:
            raise ExecutionError("Recorded admission binds two versions of one execution identity")
        versions[pin.target] = pin.artifact_digest
    cache: dict[ArtifactIdentity, tuple[Any, str]] = {}

    def exact(identity: ArtifactIdentity) -> tuple[Any, str] | None:
        if identity not in versions:
            raise ExecutionError(f"Recorded admission lacks {identity.qualified}")
        if identity not in cache:
            selected = lookup_exact(identity, versions[identity])
            if (
                selected is None
                or selected[1] != versions[identity]
                or getattr(selected[0], "identity", identity) != identity
            ):
                raise ExecutionError(
                    f"Recorded admission material is unavailable: {identity.qualified}"
                )
            cache[identity] = selected
        return cache[identity]

    return bind_procedure_references(
        accepted,
        lookup=exact,
        bindings=bindings,
        frozen_pins=pins,
        partial=True,
    )


def recorded_slot_bindings(
    resolved_provider_bindings: Iterable[Any], pins: Iterable[ArtifactPin]
) -> dict[str, LineBinding]:
    """Each recorded occurrence binding's slot as the exact ``{build, operation}`` it bound."""

    builds = {pin.artifact_digest: pin for pin in pins if pin.target.kind == "Provider"}
    found: dict[str, LineBinding] = {}
    for item in resolved_provider_bindings:
        slot = getattr(item, "slot", None)
        operation = getattr(item, "operation", None)
        if slot is None or operation is None:
            raise ExecutionError("Recorded Provider binding predates slots; re-import the run")
        pin = builds.get(item.provider_artifact_digest)
        if pin is None:
            raise ExecutionError("Recorded Provider binding has no matching admission pin")
        binding = LineBinding(build=pin, operation=operation)
        if found.get(slot, binding) != binding:
            raise ExecutionError(f"Recorded admission binds slot {slot!r} twice")
        found[slot] = binding
    return found
