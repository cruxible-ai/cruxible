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
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.models import ProcedureDefinition, iter_pin_bindings
from cruxible_client.contracts.provider_interfaces import interface_definition_digest
from cruxible_client.contracts.providers import ProviderV2, provider_interface_pin

ReferenceLookup = Callable[[ArtifactIdentity], tuple[Any, str] | None]
SlotLookup = Callable[[ArtifactIdentity], Iterable[tuple[Any, str]]]
_FIELDS = {
    "contract_in": "contract-in",
    "contract_out": "contract-out",
    "parameter_contract": "parameter-contract",
    "query": "query",
    "reducer_or_query": "query",
    "provider": "provider",
    "interface": "provider-interface",
    "capture_contract": "capture-contract",
    "claim_type": "claim-type",
    "claim_types": "claim-type",
    "procedure": "procedure",
    "effect_policy": "effect-policy",
}
_NODE_FIELDS = {
    "state_tap": ("query",),
    "source": ("capture_contract", "provider", "interface"),
    "call": ("provider", "interface", "effect_policy", "contract_in", "contract_out"),
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
    acquisition_policy: str | None
    lifecycle: ArtifactLifecycle


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


def bind_procedure_references(
    accepted: AcceptedProcedure,
    *,
    lookup: ReferenceLookup,
    slots: SlotLookup | None = None,
    bindings: Mapping[str, ArtifactIdentity] | None = None,
    frozen_providers: Mapping[str, ArtifactIdentity] | None = None,
    frozen_pins: tuple[ArtifactPin, ...] | None = None,
    frozen_implementations: dict[str, str] | None = None,
) -> BoundAcceptedProcedure:
    """Resolve one graph, with one lookup per distinct dependency identity.

    ``lookup`` and ``slots`` must be bound to the admission's accepted
    coordinate. Recovery supplies that recorded coordinate, never the head.
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

    selected_slots: dict[str, tuple[Any, str]] = {}
    slot_declarations = {s.slot_name: s for s in authored.definition.pin_slots}
    if set(bindings or {}) - set(slot_declarations):
        from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
        from cruxible_core.errors import RequestRefusedError

        raise RequestRefusedError(
            "cruxible.procedure.slot_binding_missing",
            "Bind only declared Provider slots.",
            repair=HandEditRepair(
                hand_edit=HandEditInstruction.model_validate(
                    {
                        "target": "bindings",
                        "required_change": "Remove bindings for undeclared slots.",
                    }
                )
            ),
        )
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

    def occurrence(item: dict[str, Any], occurrence_id: str) -> None:
        interface_ref = item.get("interface")
        if interface_ref is None:
            return
        interface_identity = ArtifactIdentity.model_validate(interface_ref["target"])
        interface, _ = resolve(interface_identity)
        provider_ref = item["provider"]
        slot = provider_ref.get("slot_name")
        if slot is not None:
            if slot not in selected_slots:
                if slot not in slot_declarations or slots is None:
                    raise ExecutionError(f"Provider slot {slot!r} has no admission resolver")
                explicit = (bindings or {}).get(slot) or (frozen_providers or {}).get(occurrence_id)
                candidates = (
                    (resolve(explicit),)
                    if explicit is not None
                    else tuple(slots(interface_identity))
                )
                if len(candidates) != 1:
                    from cruxible_client.contracts.repairs import (
                        HandEditInstruction,
                        HandEditRepair,
                    )
                    from cruxible_core.errors import RequestRefusedError

                    code = "slot_binding_missing" if not candidates else "slot_binding_ambiguous"
                    names = [provider.identity.qualified for provider, _ in candidates]
                    raise RequestRefusedError(
                        "cruxible.procedure." + code,
                        f"Provider slot {slot!r} needs one fitting build; candidates: {names}",
                        repair=HandEditRepair(
                            hand_edit=HandEditInstruction(
                                target="bindings",
                                required_change=(
                                    f"Install a fitting build or bind {slot}=provider explicitly."
                                ),
                            )
                        ),
                    )
                selected_slots[slot] = candidates[0]
            provider, digest = selected_slots[slot]
            resolved[provider.identity.qualified] = provider, digest
            item["provider"] = {"target": provider.identity.model_dump(mode="json")}
        else:
            provider, _ = resolve(ArtifactIdentity.model_validate(provider_ref["target"]))
        if not isinstance(provider, ProviderV2):
            raise ExecutionError("Provider occurrence requires an installed runtime build")
        if slot is not None:
            from cruxible_client.contracts.procedures.provider_slots import fitting_implementation
            from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
            from cruxible_core.errors import RequestRefusedError

            if fitting_implementation(provider, interface) is None:
                raise RequestRefusedError(
                    "cruxible.procedure.slot_binding_missing",
                    f"Provider {provider.identity.qualified} does not fit slot {slot!r}.",
                    repair=HandEditRepair(
                        hand_edit=HandEditInstruction(
                            target="bindings",
                            required_change=(
                                "Bind a live build implementing the slot interface exactly once."
                            ),
                        )
                    ),
                )
        declared_interface = provider_interface_pin(provider, interface.interface_id)
        if declared_interface is None or declared_interface.target != interface_identity:
            raise ExecutionError("Provider does not declare the named interface")
        matches = tuple(
            row
            for row in provider.implementations
            if row.interface_id == interface.interface_id
            and row.interface_digest == interface_definition_digest(interface)
        )
        if len(matches) > 1 and frozen_pins is not None:
            selected = (frozen_implementations or {}).get(
                occurrence_id, item.get("implementation_digest")
            )
            matches = tuple(row for row in matches if row.implementation_digest == selected)
        if len(matches) != 1:
            raise ExecutionError("Provider occurrence requires one fitting implementation")
        item["interface_digest"] = matches[0].interface_digest
        item["implementation_digest"] = matches[0].implementation_digest

    for field in ("contract_in", "contract_out", "parameter_contract"):
        if field in data:
            data[field] = reference(data[field], field)
    for node in data["nodes"]:
        if node["kind"] in {"call", "source"}:
            occurrence(node, node["node_id"])
        for field in _NODE_FIELDS.get(node["kind"], ()):
            if field in node:
                node[field] = reference(node[field], field)
        for body in node.get("body", ()):
            if body["operation"] == "call":
                occurrence(body, node["node_id"] + "." + body["node_id"])
            for field in ("provider", "interface", "contract_in", "contract_out", "effect_policy"):
                if field in body:
                    body[field] = reference(body[field], field)
    data.update(graph_format=6, pin_slots=())
    definition = ExecutionDefinition.model_validate(data)
    definition._authored = authored.definition
    pins = {p for p in iter_pin_bindings(definition) if isinstance(p, ArtifactPin)}
    for pin in dependency_pins(authored) if frozen_pins is None else frozen_pins:
        if pin.target.kind in EXACT_REGISTRY_TARGET_KINDS:
            if not isinstance(pin, ArtifactPin):
                raise ExecutionError(
                    f"Registry dependency {pin.target.qualified!r} requires an exact pin"
                )
            pins.add(pin)
            continue
        if pin.target.kind != "Contract":
            _, digest = resolve(pin.target)
            pins.add(ArtifactPin(role=pin.role, target=pin.target, artifact_digest=digest))
    if frozen_pins is not None and pins != set(frozen_pins):
        raise ExecutionError("Recorded admission pins do not reproduce the execution plan")
    plan = ProcedureExecutionPlan(
        identity=authored.identity,
        definition=definition,
        definition_digest=authored.definition_digest,
        pins=tuple(sorted(pins, key=lambda p: (p.role, p.target.qualified, p.artifact_digest))),
        owned_contracts=authored.owned_contracts,
        activation_policy=authored.activation_policy,
        acquisition_policy=authored.acquisition_policy,
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
    implementations: dict[str, str] | None = None,
    providers: Mapping[str, ArtifactIdentity] | None = None,
) -> BoundAcceptedProcedure:
    """Rebuild from recorded digests. There is no accepted-head/name resolver."""
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

    def slots(interface: ArtifactIdentity) -> tuple[tuple[Any, str], ...]:
        candidates = []
        for identity in versions:
            if identity.kind != "Provider":
                continue
            selected = exact(identity)
            assert selected is not None
            provider = selected[0]
            reference = provider_interface_pin(provider, interface.name.partition("/")[0])
            if reference is not None and reference.target == interface:
                candidates.append(selected)
        return tuple(candidates)

    return bind_procedure_references(
        accepted,
        lookup=exact,
        slots=slots,
        frozen_pins=pins,
        frozen_providers=providers,
        frozen_implementations=implementations,
    )
