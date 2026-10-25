"""Request-owned execution plans, resolved at one immutable accepted coordinate.

These are execution data, never governed artifacts. Authored bytes and their
identity remain separate from the exact pins recorded by run admission.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SerializeAsAny, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
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
from cruxible_client.contracts.providers import ProviderV2

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
) -> BoundAcceptedProcedure:
    """Resolve one graph, with one lookup per distinct dependency identity.

    ``lookup`` and ``slots`` must be bound to the admission's accepted
    coordinate. Recovery supplies that recorded coordinate, never the head.
    Payloads, schemas and retained source snapshots are not traversed.
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

    selected_slots: dict[str, tuple[Any, str]] = {}
    slot_declarations = {s.slot_name: s for s in authored.definition.pin_slots}
    data = authored.definition.model_dump(mode="json", by_alias=True)
    data.pop("source", None)

    def reference(value: Any, field: str) -> Any:
        if value is None:
            return None
        if isinstance(value, list):
            return [reference(item, field) for item in value]
        identity = ArtifactIdentity.model_validate(value["target"])
        _, digest = resolve(identity)
        return ArtifactPin(role=_FIELDS[field], target=identity, artifact_digest=digest).model_dump(
            mode="json"
        )

    def occurrence(item: dict[str, Any]) -> None:
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
                candidates = tuple(slots(interface_identity))
                if len(candidates) != 1:
                    raise ExecutionError(
                        f"Provider slot {slot!r} needs one fitting build; found {len(candidates)}"
                    )
                selected_slots[slot] = candidates[0]
            provider, digest = selected_slots[slot]
            resolved[provider.identity.qualified] = provider, digest
            item["provider"] = {"target": provider.identity.model_dump(mode="json")}
        else:
            provider, _ = resolve(ArtifactIdentity.model_validate(provider_ref["target"]))
        if not isinstance(provider, ProviderV2):
            raise ExecutionError("Provider occurrence requires an installed runtime build")
        matches = tuple(
            row
            for row in provider.implementations
            if row.interface_id == interface.interface_id
            and row.interface_digest == interface_definition_digest(interface)
        )
        if len(matches) != 1:
            raise ExecutionError("Provider occurrence requires one fitting implementation")
        item["interface_digest"] = matches[0].interface_digest
        item["implementation_digest"] = matches[0].implementation_digest

    for field in ("contract_in", "contract_out", "parameter_contract"):
        if field in data:
            data[field] = reference(data[field], field)
    for node in data["nodes"]:
        if node["kind"] in {"call", "source"}:
            occurrence(node)
        for field in _NODE_FIELDS.get(node["kind"], ()):
            if field in node:
                node[field] = reference(node[field], field)
        for body in node.get("body", ()):
            if body["operation"] == "call":
                occurrence(body)
            for field in ("provider", "interface", "contract_in", "contract_out", "effect_policy"):
                if field in body:
                    body[field] = reference(body[field], field)
    data.update(graph_format=6, pin_slots=())
    definition = ExecutionDefinition.model_validate(data)
    definition._authored = authored.definition
    pins = {p for p in iter_pin_bindings(definition) if isinstance(p, ArtifactPin)}
    for pin in authored.pins:
        if pin.target.kind != "Contract":
            _, digest = resolve(pin.target)
            pins.add(pin.model_copy(update={"artifact_digest": digest}))
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
