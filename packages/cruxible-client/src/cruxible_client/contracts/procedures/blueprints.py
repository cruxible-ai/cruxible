"""Procedure Blueprints: one skeleton graph whose Provider slots are left open.

A Blueprint is the same definition a Procedure carries, with interface-typed
Provider slots unbound; it never runs. Instantiating it binds one compatible
accepted Provider per slot -- checked by the shared slot closure -- and yields
an ordinary Procedure that records the Blueprint and its bindings. Binding
happens exactly once, at instantiation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.artifacts import (
    BlueprintArtifact,
    BlueprintOrigin,
    ProcedureArtifact,
    procedure_owned_contract_digest,
    provider_occurrences,
)
from cruxible_client.contracts.procedures.closure import (
    ProcedurePinClosureError,
    ProcedureSlotBinding,
    close_procedure_pin_slots,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import (
    ProcedureDefinition,
    ProcedurePinSlot,
    iter_pin_bindings,
)
from cruxible_client.contracts.provider_interfaces import AcceptedProviderInterfaceRegistration
from cruxible_client.contracts.providers import AcceptedProvider, ProviderV2
from cruxible_client.contracts.semantic import SemanticAddress

_BLUEPRINT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_SLOT_REF_TAG = "playbill-procedure-pin-slot-ref-v1"


class BlueprintFormatError(FormatError):
    """A Blueprint artifact or canonical path is invalid."""


class BlueprintInstantiationError(ValueError):
    """Bindings do not close a Blueprint's slots exactly."""

    def __init__(self, code: str, message: str, *, slot: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.slot = slot


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def blueprint_path(name: str) -> str:
    if not _BLUEPRINT_NAME_RE.fullmatch(name):
        raise BlueprintFormatError("Blueprint identity is not path-addressable")
    return f"blueprints/{name}.json"


def render_blueprint(blueprint: BlueprintArtifact) -> bytes:
    return pretty_canonical_bytes(blueprint.model_dump(mode="json", by_alias=True))


_BLUEPRINT_ADAPTER: TypeAdapter[BlueprintArtifact] = TypeAdapter(BlueprintArtifact)


def parse_blueprint(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> BlueprintArtifact:
    try:
        blueprint = _BLUEPRINT_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BlueprintFormatError("Blueprint failed strict versioned validation") from exc
    if not artifact_path_matches(blueprint_path(blueprint.identity.name), path, codec=codec):
        raise BlueprintFormatError("Blueprint identity/path disagreement")
    if artifact_bytes_for_path(render_blueprint(blueprint), path, codec=codec) != content:
        raise BlueprintFormatError("Blueprint is not in canonical wire form")
    return blueprint


def blueprint_digest(blueprint: BlueprintArtifact) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        blueprint.model_dump(mode="json", by_alias=True),
    )


class AcceptedBlueprint(_Strict):
    path: str
    blueprint: BlueprintArtifact
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> AcceptedBlueprint:
        if self.path != blueprint_path(self.blueprint.identity.name):
            raise ValueError("accepted Blueprint path does not reproduce")
        if self.artifact_digest != blueprint_digest(self.blueprint).tagged:
            raise ValueError("accepted Blueprint digest does not reproduce")
        return self


class BlueprintLawResult(_Strict):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> BlueprintLawResult:
    return BlueprintLawResult(
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


def evaluate_blueprint_law(
    blueprint: BlueprintArtifact,
    *,
    path: str,
    predecessor: AcceptedBlueprint | None,
    provider_interfaces: Mapping[str, AcceptedProviderInterfaceRegistration] | None = None,
) -> BlueprintLawResult:
    """Stable identity, predecessor, and every slot typed by an accepted interface."""

    if path != blueprint_path(blueprint.identity.name):
        return _refusal(
            "cruxible.blueprint.path_mismatch", "Blueprint identity/path disagreement.", path=path
        )
    if predecessor is None:
        if blueprint.lifecycle.predecessor_digest is not None:
            return _refusal(
                "cruxible.blueprint.predecessor_missing",
                "A new Blueprint cannot name a predecessor.",
                path=path,
            )
    else:
        if blueprint.identity != predecessor.blueprint.identity:
            return _refusal(
                "cruxible.blueprint.stable_identity_changed",
                "A Blueprint successor must retain stable identity.",
                path=path,
            )
        if blueprint.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.blueprint.predecessor_mismatch",
                "Blueprint successor does not pin its exact predecessor.",
                path=path,
            )
    interfaces = {} if provider_interfaces is None else provider_interfaces
    for slot in blueprint.definition.pin_slots:
        if not any(
            item.registration.interface_digest == slot.interface_digest
            for item in interfaces.values()
        ):
            return _refusal(
                "cruxible.blueprint.slot_interface_unknown",
                f"Slot {slot.slot_name!r} names no accepted ProviderInterface.",
                path=path,
            )
    return BlueprintLawResult(
        verdict="accepted",
        artifact_digest=blueprint_digest(blueprint).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


def blueprint_slot_interface(
    blueprint: BlueprintArtifact, slot: ProcedurePinSlot
) -> ArtifactPin | None:
    """The exact ProviderInterface pin the slot's Provider positions name."""

    for _occurrence_id, occurrence in provider_occurrences(blueprint.definition):
        if getattr(getattr(occurrence, "provider", None), "slot_name", None) == slot.slot_name:
            interface = getattr(occurrence, "interface", None)
            return interface if isinstance(interface, ArtifactPin) else None
    return None


def fitting_implementation(provider: AcceptedProvider, slot: ProcedurePinSlot) -> str | None:
    """The one implementation of this Provider that serves the slot's interface, if any."""

    if not isinstance(provider.provider, ProviderV2):
        return None
    matches = tuple(
        item.implementation_digest
        for item in provider.provider.implementations
        if item.interface_digest == slot.interface_digest
    )
    return matches[0] if len(matches) == 1 else None


def _replace_slots(
    value: Any, *, pins: Mapping[str, dict[str, Any]], impls: Mapping[str, str]
) -> Any:
    if isinstance(value, list):
        return [_replace_slots(item, pins=pins, impls=impls) for item in value]
    if isinstance(value, dict):
        provider = value.get("provider")
        if isinstance(provider, dict) and provider.get("tag") == _SLOT_REF_TAG:
            slot = str(provider["slot_name"])
            value = {**value, "provider": pins[slot], "implementation_digest": impls[slot]}
        return {key: _replace_slots(item, pins=pins, impls=impls) for key, item in value.items()}
    return value


def instantiate_blueprint(
    blueprint: BlueprintArtifact,
    *,
    blueprint_artifact_digest: str,
    name: str,
    providers: Mapping[str, AcceptedProvider],
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"] | None = None,
    extra_pins: tuple[ArtifactPin, ...] = (),
    lifecycle: ArtifactLifecycle | None = None,
) -> ProcedureArtifact:
    """Bind one accepted Provider per slot and return the ordinary Procedure it yields.

    ``providers`` maps each slot name to the accepted Provider bound to it. The
    slot closure checks role, kind and interface; the Provider must expose
    exactly one implementation of the slot's interface.
    """

    declarations = {slot.slot_name: slot for slot in blueprint.definition.pin_slots}
    if set(providers) != set(declarations):
        missing = sorted(set(declarations) - set(providers))
        extra = sorted(set(providers) - set(declarations))
        raise BlueprintInstantiationError(
            "cruxible.blueprint.binding_set_mismatch",
            f"Bind exactly the Blueprint's slots {sorted(declarations)}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unknown {extra}" if extra else ""),
        )
    bindings: list[ProcedureSlotBinding] = []
    interface_digests: dict[str, str] = {}
    implementations: dict[str, str] = {}
    for slot_name in sorted(declarations, key=lambda item: item.encode("utf-8")):
        slot = declarations[slot_name]
        provider = providers[slot_name]
        implementation = fitting_implementation(provider, slot)
        if implementation is None:
            raise BlueprintInstantiationError(
                "cruxible.blueprint.provider_interface_mismatch",
                f"Provider {provider.provider.identity.name!r} does not implement slot "
                f"{slot_name!r}'s interface exactly once.",
                slot=slot_name,
            )
        implementations[slot_name] = implementation
        interface_digests[provider.artifact_digest] = slot.interface_digest
        bindings.append(
            ProcedureSlotBinding(
                slot_name=slot_name,
                artifact_pin=ArtifactPin(
                    role="provider",
                    target=provider.provider.identity,
                    artifact_digest=provider.artifact_digest,
                ),
            )
        )
    try:
        closure = close_procedure_pin_slots(
            blueprint.definition,
            blueprint.pins,
            bindings=tuple(bindings),
            interface_digests=interface_digests,
        )
    except ProcedurePinClosureError as exc:
        raise BlueprintInstantiationError(
            "cruxible.blueprint.binding_closure_failed", str(exc)
        ) from exc
    definition = (
        _recompile_source(
            blueprint, name=name, providers=providers, implementations=implementations
        )
        if blueprint.definition.source is not None
        else _bind_graph(blueprint, name=name, bindings=bindings, implementations=implementations)
    )
    pins = {
        (pin.role, pin.target.qualified, pin.artifact_digest): pin
        for pin in (
            *(item for item in iter_pin_bindings(definition) if isinstance(item, ArtifactPin)),
            *extra_pins,
        )
    }
    if not set(pins).issubset(
        {
            (pin.role, pin.target.qualified, pin.artifact_digest)
            for pin in (*closure.exact_pins, *extra_pins)
        }
    ):
        raise BlueprintInstantiationError(
            "cruxible.blueprint.binding_closure_failed",
            "Instantiation produced pins outside the Blueprint's exact closure.",
        )
    return ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=tuple(
            pins[key]
            for key in sorted(pins, key=lambda item: tuple(part.encode("utf-8") for part in item))
        ),
        owned_contracts=blueprint.owned_contracts,
        activation_policy=activation_policy or blueprint.activation_policy,
        lifecycle=lifecycle or ArtifactLifecycle(),
        blueprint=BlueprintOrigin(
            blueprint=ArtifactPin(
                role="blueprint",
                target=blueprint.identity,
                artifact_digest=blueprint_artifact_digest,
            ),
            bindings=tuple(bindings),
        ),
    )


def _bind_graph(
    blueprint: BlueprintArtifact,
    *,
    name: str,
    bindings: list[ProcedureSlotBinding],
    implementations: Mapping[str, str],
) -> ProcedureDefinition:
    raw = blueprint.definition.model_dump(mode="json", by_alias=True)
    pins = {item.slot_name: item.artifact_pin.model_dump(mode="json") for item in bindings}
    bound = _replace_slots(raw, pins=pins, impls=implementations)
    bound["pin_slots"] = []
    bound["name"] = name
    return ProcedureDefinition.model_validate(bound)


def _recompile_source(
    blueprint: BlueprintArtifact,
    *,
    name: str,
    providers: Mapping[str, AcceptedProvider],
    implementations: Mapping[str, str],
) -> ProcedureDefinition:
    from cruxible_client.contracts.procedures.source_compiler import compile_source
    from cruxible_client.contracts.procedures.source_program import (
        SourceContract,
        SourceProviderBinding,
        SourceSlotBinding,
    )
    from cruxible_client.contracts.providers import provider_digest

    definition = blueprint.definition
    source = definition.source
    assert source is not None
    bound: dict[str, Any] = dict(source.bindings)
    for slot_name, value in source.bindings.items():
        if not isinstance(value, SourceSlotBinding):
            continue
        provider = providers[slot_name].provider
        bound[slot_name] = SourceProviderBinding(
            provider=provider.identity.name,
            provider_version=provider_digest(provider).tagged,
            interface=value.interface,
            interface_version=value.interface_version,
            interface_digest=value.interface_digest,
            implementation_digest=implementations[slot_name],
            effect_class=value.effect_class,
            operation=value.operation,
        )

    def root(pin: object) -> SourceContract:
        for contract in blueprint.owned_contracts:
            if (
                isinstance(pin, ArtifactPin)
                and contract.identity == pin.target
                and procedure_owned_contract_digest(contract).tagged == pin.artifact_digest
            ):
                return SourceContract(name=contract.identity.name, schema=contract.contract_schema)
        raise BlueprintInstantiationError(
            "cruxible.blueprint.source_contract_missing",
            "The Blueprint's root Contract is not an exact owned Contract.",
        )

    compiled = compile_source(
        source.model_copy(update={"bindings": bound}),
        name=name,
        input=root(definition.contract_in),
        output=root(definition.contract_out),
        budget=definition.budget,
        hard_caps=definition.hard_caps,
        terminal_capability=definition.terminal_capability,
        description=definition.description,
    )
    return compiled.definition


__all__ = [
    "AcceptedBlueprint",
    "BlueprintFormatError",
    "BlueprintInstantiationError",
    "BlueprintLawResult",
    "blueprint_digest",
    "blueprint_path",
    "blueprint_slot_interface",
    "evaluate_blueprint_law",
    "fitting_implementation",
    "instantiate_blueprint",
    "parse_blueprint",
    "render_blueprint",
]
