"""Request-owned execution plans, resolved at one immutable accepted coordinate.

These are execution data, never governed artifacts. A plan keeps the authored
graph unchanged and records, beside it, the exact pin every named dependency
resolves to at the admission's coordinate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
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
    CallNode,
    ProcedureDefinition,
    RepeatNode,
    SourceNode,
    iter_role_references,
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

#: The pin role a resolved dependency carries where no graph field names it.
_KIND_ROLES = {
    "CaptureContract": "capture-contract",
    "ClaimType": "claim-type",
    "Contract": "contract",
    "EffectPolicy": "effect-policy",
    "Procedure": "procedure",
    "QueryDefinition": "query",
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


class ProcedureExecutionPlan(BaseModel):
    """The authored graph with every named dependency resolved to an exact pin."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    identity: ArtifactIdentity
    definition: ProcedureDefinition
    definition_digest: str
    pins: tuple[ArtifactPin, ...]
    #: Each named dependency's exact digest at the coordinate, by qualified identity.
    resolved: dict[str, str]
    owned_contracts: tuple[ProcedureOwnedContract, ...]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    acquisition_defaults: dict[str, AcquisitionDefault]
    #: Each bound slot, by slot name: the run uses these and never refits.
    slot_bindings: tuple[ResolvedSlotBinding, ...] = ()
    lifecycle: ArtifactLifecycle

    def binding_for(self, slot: str) -> ResolvedSlotBinding | None:
        return next((item for item in self.slot_bindings if item.slot == slot), None)

    def pin(self, reference: ArtifactNameRef, *, role: str | None = None) -> ArtifactPin:
        """The exact pin ``reference`` resolved to at the admission coordinate."""

        digest = self.resolved.get(reference.target.qualified)
        if digest is None:
            raise ExecutionError(f"{reference.target.qualified} was not resolved at admission")
        return ArtifactPin(
            role=role or _KIND_ROLES.get(reference.target.kind, "dependency"),
            target=reference.target,
            artifact_digest=digest,
        )


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


def execution_plan(accepted: AcceptedProcedure) -> ProcedureExecutionPlan:
    """The bound plan an admission runs; an unbound Procedure has no exact pins."""

    if not isinstance(accepted, BoundAcceptedProcedure):
        raise ExecutionError(
            f"Procedure {accepted.procedure.identity.qualified} was not bound at admission"
        )
    return accepted.procedure


def resolved_pin(
    accepted: AcceptedProcedure, reference: ArtifactNameRef, *, role: str | None = None
) -> ArtifactPin:
    """The exact pin one graph reference resolved to in ``accepted``'s plan."""

    return execution_plan(accepted).pin(reference, role=role)


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


def _source_only_slot_names(definition: ProcedureDefinition) -> frozenset[str]:
    """Slot names every node that uses them is a Source: a Call never is one.

    ``partial`` admits a missing binding only for these: a Source's input may
    be supplied directly (a landed Capture, exhaust input or a parent run's
    material) rather than invoked, so its slot was never bound at admission.
    A Call is always invoked, so its slot binding is never optional.
    """

    source_slots: set[str] = set()
    other_slots: set[str] = set()
    for node in definition.nodes:
        if isinstance(node, SourceNode):
            source_slots.add(node.slot)
        elif isinstance(node, CallNode):
            other_slots.add(node.slot)
        elif isinstance(node, RepeatNode):
            for body in node.body:
                if body.operation == "call" and body.slot is not None:
                    other_slots.add(body.slot)
    return frozenset(source_slots - other_slots)


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
    Source-only slot may be left unbound (a replay of a slot only supplied
    Sources used); a slot any Call node also uses still fails closed, since a
    Call is always invoked and never has supplied material standing in for
    its binding. The authored graph is kept as is; payloads, schemas and
    retained source snapshots are not traversed.
    """
    authored = authored_procedure(accepted)
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
    source_only_slots = _source_only_slot_names(authored.definition)
    references: dict[str, str] = {}
    pins: set[ArtifactPin] = set()
    for role, reference in iter_role_references(authored.definition):
        identity = reference.target
        _, digest = resolve(identity)
        references[identity.qualified] = digest
        pins.add(ArtifactPin(role=role, target=identity, artifact_digest=digest))
    slot_bindings: list[ResolvedSlotBinding] = []
    for name, slot in slot_declarations.items():
        binding = (bindings or {}).get(name)
        if binding is None:
            if partial and name in source_only_slots:
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
    if frozen_pins is not None:
        # A replay keeps every build the admission bound, including one only a
        # supplied Source would have used.
        pins.update(
            pin for pin in frozen_pins if pin.target.kind == "Provider" and pin.role == "provider"
        )
    pins.update(item.build for item in slot_bindings)
    if frozen_pins is not None and pins != set(frozen_pins):
        raise ExecutionError("Recorded admission pins do not reproduce the execution plan")
    plan = ProcedureExecutionPlan(
        identity=authored.identity,
        definition=authored.definition,
        definition_digest=authored.definition_digest,
        pins=tuple(sorted(pins, key=lambda p: (p.role, p.target.qualified, p.artifact_digest))),
        resolved=references,
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
