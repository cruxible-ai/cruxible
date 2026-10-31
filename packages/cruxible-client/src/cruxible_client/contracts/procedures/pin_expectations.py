"""Central field-to-pin expectations for Procedure graphs.

An ``ArtifactPin`` proves exact bytes; it does not say that the artifact is
suitable for the Procedure field that names it.  This module is the one
nominal typing seam between graph fields and their pins.
"""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.procedures.models import (
    CallNode,
    CaptureEgressNode,
    InvokeNode,
    ProcedureDefinition,
    ProjectNode,
    ProposeChangeSetNode,
    ReadNode,
    RepeatNode,
    SelectNode,
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
CAPTURE_CONTRACT = PinExpectation((("capture-contract", "CaptureContract"),))
EFFECT_POLICY = PinExpectation((("effect-policy", "EffectPolicy"),))
REDUCER_OR_QUERY = PinExpectation((("query", "QueryDefinition"), ("reducer", "Reducer")))

TRIGGER_CADENCE_POLICY = PinExpectation((("trigger-cadence-policy", "Policy"),))
TRIGGER_CAPTURE_CONTRACT = PinExpectation((("trigger-capture-contract", "CaptureContract"),))
TRIGGER_LANDING_FILTER = PinExpectation((("trigger-landing-filter", "LandingFilter"),))
TRIGGER_WINDOW_POLICY = PinExpectation((("trigger-window-policy", "Policy"),))


def validate_exact_pin_expectation(
    pin: ArtifactPin | ArtifactNameRef,
    expectation: PinExpectation,
    *,
    location: str,
) -> None:
    """Refuse an exact pin whose nominal role/kind does not fit its field."""

    if isinstance(pin, ArtifactNameRef):
        if pin.target.kind not in {kind for _role, kind in expectation.allowed}:
            raise ValueError(f"{location} requires {expectation.description}")
        return
    actual = (pin.role, pin.target.kind)
    if actual not in expectation.allowed:
        raise ValueError(
            f"{location} requires {expectation.description}; got "
            f"role={pin.role!r} kind={pin.target.kind!r}"
        )


def validate_procedure_pin_expectations(definition: ProcedureDefinition) -> None:
    """Validate every semantic pin field in one graph."""

    def check(
        binding: ArtifactNameRef | None,
        expectation: PinExpectation,
        location: str,
    ) -> None:
        if binding is not None:
            validate_exact_pin_expectation(binding, expectation, location=location)

    check(definition.contract_in, CONTRACT_IN, "Procedure contract_in")
    check(definition.contract_out, CONTRACT_OUT, "Procedure contract_out")
    check(
        definition.parameter_contract,
        PARAMETER_CONTRACT,
        "Procedure parameter_contract",
    )

    for node in definition.nodes:
        prefix = f"Procedure node {node.node_id!r}"
        if isinstance(node, InvokeNode):
            check(
                node.procedure, PinExpectation((("procedure", "Procedure"),)), f"{prefix} procedure"
            )
        elif isinstance(node, ReadNode):
            check(
                node.claim_type,
                PinExpectation((("claim-type", "ClaimType"),)),
                f"{prefix} claim_type",
            )
            check(node.query, QUERY, f"{prefix} query")
        elif isinstance(node, ProposeChangeSetNode):
            for pin in node.claim_types:
                check(pin, PinExpectation((("claim-type", "ClaimType"),)), f"{prefix} claim_type")
        elif isinstance(node, CallNode):
            check(node.effect_policy, EFFECT_POLICY, f"{prefix} effect_policy")
            for field, capture in node.captures.items():
                check(capture, CAPTURE_CONTRACT, f"{prefix} captures.{field}")
        elif isinstance(node, ProjectNode | SelectNode):
            check(node.contract_out, CONTRACT_OUT, f"{prefix} contract_out")
        elif isinstance(node, RepeatNode):
            for body in node.body:
                body_prefix = f"{prefix} repeat body {body.node_id!r}"
                check(body.effect_policy, EFFECT_POLICY, f"{body_prefix} effect_policy")
        elif isinstance(node, CaptureEgressNode):
            check(node.capture_contract, CAPTURE_CONTRACT, f"{prefix} capture_contract")


__all__ = [
    "PinExpectation",
    "TRIGGER_CADENCE_POLICY",
    "TRIGGER_CAPTURE_CONTRACT",
    "TRIGGER_LANDING_FILTER",
    "TRIGGER_WINDOW_POLICY",
    "validate_exact_pin_expectation",
    "validate_procedure_pin_expectations",
]
