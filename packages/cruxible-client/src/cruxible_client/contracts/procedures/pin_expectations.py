"""Central field-to-pin expectations for graph-format-v3 Procedures.

An ``ArtifactPin`` proves exact bytes and a slot declaration proves an
interface binding point.  Neither fact says that the artifact is suitable for
the Procedure field that names it.  This module is the one nominal typing seam
between graph fields and exact/slot-backed pins.
"""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.procedures.models import (
    CaptureEgressNodeV3,
    ExhaustTapNodeV3,
    MandateSettlementNodeV3,
    ProcedureDefinitionAny,
    ProcedurePinBindingV1,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProjectNodeV3,
    ProviderNodeV3,
    ProviderNodeV4,
    RepeatBodyNodeV3,
    RepeatBodyNodeV4,
    RepeatNodeV3,
    RepeatNodeV4,
    SourceNodeV3,
    SourceNodeV4,
    StateTapNodeV3,
    TransformNodeV3,
)


@dataclass(frozen=True)
class PinExpectation:
    """The closed role/kind pairs one semantic field admits."""

    allowed: tuple[tuple[str, str], ...]

    @property
    def description(self) -> str:
        return " or ".join(f"role={role!r} kind={kind!r}" for role, kind in self.allowed)


CONTRACT_IN = PinExpectation((("contract-in", "Contract"),))
CONTRACT_OUT = PinExpectation((("contract-out", "Contract"),))
PARAMETER_CONTRACT = PinExpectation((("parameter-contract", "Contract"),))
QUERY = PinExpectation((("query", "QueryDefinition"),))
PROVIDER = PinExpectation((("provider", "Provider"),))
PROVIDER_INTERFACE = PinExpectation((("provider-interface", "ProviderInterface"),))
CAPTURE_CONTRACT = PinExpectation((("capture-contract", "CaptureContract"),))
ENVIRONMENT = PinExpectation((("environment", "EnvironmentManifest"),))
EFFECT_POLICY = PinExpectation((("effect-policy", "EffectPolicy"),))
REDUCER_OR_QUERY = PinExpectation((("query", "QueryDefinition"), ("reducer", "Reducer")))
MANDATE = PinExpectation((("mandate", "StandingMandate"),))
TARGET_LAW = PinExpectation((("target-law", "Policy"),))

TRIGGER_CADENCE_POLICY = PinExpectation((("trigger-cadence-policy", "Policy"),))
TRIGGER_CAPTURE_CONTRACT = PinExpectation((("trigger-capture-contract", "CaptureContract"),))
TRIGGER_LANDING_FILTER = PinExpectation((("trigger-landing-filter", "LandingFilter"),))
TRIGGER_WINDOW_POLICY = PinExpectation((("trigger-window-policy", "Policy"),))


def validate_exact_pin_expectation(
    pin: ArtifactPin,
    expectation: PinExpectation,
    *,
    location: str,
) -> None:
    """Refuse an exact pin whose nominal role/kind does not fit its field."""

    actual = (pin.role, pin.target.kind)
    if actual not in expectation.allowed:
        raise ValueError(
            f"{location} requires {expectation.description}; got "
            f"role={pin.role!r} kind={pin.target.kind!r}"
        )


def _validate_binding_expectation(
    binding: ProcedurePinBindingV1,
    expectation: PinExpectation,
    *,
    location: str,
    slots: dict[str, ProcedurePinSlotV1],
) -> None:
    if isinstance(binding, ArtifactPin):
        validate_exact_pin_expectation(binding, expectation, location=location)
        return
    if not isinstance(binding, ProcedurePinSlotRefV1):  # pragma: no cover - closed union
        raise TypeError(f"unsupported Procedure pin binding at {location}")
    declaration = slots.get(binding.slot_name)
    if declaration is None:
        raise ValueError(f"{location} references undeclared pin slot {binding.slot_name!r}")
    actual = (declaration.pin_role, declaration.artifact_kind)
    if actual not in expectation.allowed:
        raise ValueError(
            f"{location} slot {binding.slot_name!r} requires "
            f"{expectation.description}; declaration has "
            f"role={declaration.pin_role!r} kind={declaration.artifact_kind!r}"
        )


def validate_procedure_pin_expectations(definition: ProcedureDefinitionAny) -> None:
    """Validate every exact or slot-backed semantic pin field in one graph."""

    slots = {slot.slot_name: slot for slot in definition.pin_slots}

    def check(
        binding: ProcedurePinBindingV1 | None,
        expectation: PinExpectation,
        location: str,
    ) -> None:
        if binding is not None:
            _validate_binding_expectation(
                binding,
                expectation,
                location=location,
                slots=slots,
            )

    check(definition.contract_in, CONTRACT_IN, "Procedure contract_in")
    check(definition.contract_out, CONTRACT_OUT, "Procedure contract_out")
    check(
        definition.parameter_contract,
        PARAMETER_CONTRACT,
        "Procedure parameter_contract",
    )

    for node in definition.nodes:
        prefix = f"Procedure node {node.node_id!r}"
        if isinstance(node, StateTapNodeV3):
            check(node.query, QUERY, f"{prefix} query")
        elif isinstance(node, SourceNodeV3 | SourceNodeV4):
            check(node.capture_contract, CAPTURE_CONTRACT, f"{prefix} capture_contract")
            check(node.provider, PROVIDER, f"{prefix} provider")
            if isinstance(node, SourceNodeV4):
                validate_exact_pin_expectation(
                    node.interface,
                    PROVIDER_INTERFACE,
                    location=f"{prefix} interface",
                )
                if isinstance(node.provider, ProcedurePinSlotRefV1):
                    slot = slots[node.provider.slot_name]
                    if slot.interface_digest != node.interface_digest:
                        raise ValueError(
                            f"{prefix} interface_digest disagrees with Provider slot "
                            f"{node.provider.slot_name!r}"
                        )
        elif isinstance(node, ExhaustTapNodeV3):
            check(
                node.reducer_or_query,
                REDUCER_OR_QUERY,
                f"{prefix} reducer_or_query",
            )
        elif isinstance(node, ProviderNodeV3 | ProviderNodeV4):
            check(node.provider, PROVIDER, f"{prefix} provider")
            check(node.contract_in, CONTRACT_IN, f"{prefix} contract_in")
            check(node.contract_out, CONTRACT_OUT, f"{prefix} contract_out")
            if isinstance(node, ProviderNodeV3):
                check(node.environment, ENVIRONMENT, f"{prefix} environment")
            else:
                validate_exact_pin_expectation(
                    node.interface,
                    PROVIDER_INTERFACE,
                    location=f"{prefix} interface",
                )
                if isinstance(node.provider, ProcedurePinSlotRefV1):
                    slot = slots[node.provider.slot_name]
                    if slot.interface_digest != node.interface_digest:
                        raise ValueError(
                            f"{prefix} interface_digest disagrees with Provider slot "
                            f"{node.provider.slot_name!r}"
                        )
            check(node.effect_policy, EFFECT_POLICY, f"{prefix} effect_policy")
        elif isinstance(node, TransformNodeV3):
            check(node.contract_in, CONTRACT_IN, f"{prefix} contract_in")
            check(node.contract_out, CONTRACT_OUT, f"{prefix} contract_out")
        elif isinstance(node, ProjectNodeV3):
            check(node.contract_out, CONTRACT_OUT, f"{prefix} contract_out")
        elif isinstance(node, RepeatNodeV3 | RepeatNodeV4):
            for body in node.body:
                body_prefix = f"{prefix} repeat body {body.node_id!r}"
                check(body.provider, PROVIDER, f"{body_prefix} provider")
                check(body.contract_in, CONTRACT_IN, f"{body_prefix} contract_in")
                check(body.contract_out, CONTRACT_OUT, f"{body_prefix} contract_out")
                if isinstance(body, RepeatBodyNodeV3):
                    check(body.environment, ENVIRONMENT, f"{body_prefix} environment")
                elif isinstance(body, RepeatBodyNodeV4) and body.operation == "provider":
                    if body.interface is None or body.interface_digest is None:
                        raise ValueError(f"{body_prefix} lacks its Provider interface")
                    validate_exact_pin_expectation(
                        body.interface,
                        PROVIDER_INTERFACE,
                        location=f"{body_prefix} interface",
                    )
                    if isinstance(body.provider, ProcedurePinSlotRefV1):
                        slot = slots[body.provider.slot_name]
                        if slot.interface_digest != body.interface_digest:
                            raise ValueError(
                                f"{body_prefix} interface_digest disagrees with Provider slot "
                                f"{body.provider.slot_name!r}"
                            )
                    check(body.effect_policy, EFFECT_POLICY, f"{body_prefix} effect_policy")
        elif isinstance(node, CaptureEgressNodeV3):
            check(node.capture_contract, CAPTURE_CONTRACT, f"{prefix} capture_contract")
        elif isinstance(node, MandateSettlementNodeV3):
            check(node.mandate, MANDATE, f"{prefix} mandate")
            check(node.target_law, TARGET_LAW, f"{prefix} target_law")


__all__ = [
    "PinExpectation",
    "TRIGGER_CADENCE_POLICY",
    "TRIGGER_CAPTURE_CONTRACT",
    "TRIGGER_LANDING_FILTER",
    "TRIGGER_WINDOW_POLICY",
    "validate_exact_pin_expectation",
    "validate_procedure_pin_expectations",
]
