"""Governed LineSpec artifact: one stable instantiation of an accepted Procedure."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.acquisition_policies import (
    ACQUISITION_POLICY_PIN_ROLE,
    SourceAcquisitionPolicyAny,
)
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    normalize_canonical,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.models import (
    AUTHORITY_RUNG,
    RUNG_AUTHORITY,
    AuthorityVerb,
    ExhaustTapNode,
    ProcedureDefinition,
    SourceNode,
)
from cruxible_client.contracts.procedures.pin_expectations import (
    TRIGGER_CAPTURE_CONTRACT,
    validate_exact_pin_expectation,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelector,
    NamedCaptureEventSelector,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
)
from cruxible_client.contracts.providers import (
    AcceptedProvider,
)
from cruxible_client.contracts.semantic import SemanticAddress

_LINE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class LineSpecFormatError(FormatError):
    """A LineSpec artifact, closure, or successor transition is invalid."""


class _StrictLineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _artifact_digest(value: str) -> str:
    ArtifactDigest.from_tagged(value)
    return value


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


def _decimal_wrapper(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or tuple(value) != ("$decimal",):
        raise ValueError("LineSpec epsilon must be a canonical $decimal wrapper")
    spelling = value["$decimal"]
    if not isinstance(spelling, str):
        raise ValueError("LineSpec epsilon decimal must be text")
    try:
        decimal = Decimal(spelling)
    except InvalidOperation as exc:
        raise ValueError("LineSpec epsilon is not a decimal") from exc
    if not decimal.is_finite() or decimal < 0 or decimal > 1:
        raise ValueError("LineSpec epsilon must be in [0,1]")
    canonical = format(decimal, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical in {"", "-0"}:
        canonical = "0"
    if spelling != canonical:
        raise ValueError("LineSpec epsilon decimal spelling is not canonical")
    return {"$decimal": spelling}


def _canonical_object(value: object) -> object:
    normalized = normalize_canonical(value)
    if not isinstance(normalized, dict):
        raise ValueError("LineSpec parameters and budgets must be canonical objects")
    return normalized


def _sorted_pins(value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
    if value != tuple(sorted(value, key=_pin_key)):
        raise ValueError("LineSpec pins must be canonically sorted")
    keys = tuple((pin.role, pin.target.qualified) for pin in value)
    if len(set(keys)) != len(keys):
        raise ValueError("LineSpec pins must be unique by role and target")
    return value


LineAuthority = AuthorityVerb


class LineSpec(_StrictLineModel):
    """A Line with no embedded trigger: Trigger artifacts aim at it by identity.

    When it runs is not the Line's to say: every Trigger aimed at it is its own
    governed artifact, and Triggers drive it only while the Line is enabled.
    ``line run`` runs one manual occurrence at any time. What stays with the
    Line is what it accepts: ``trigger_input`` binds the
    triggering Capture to one named Source input, and ``trigger_event`` declares
    the exact Capture event that input accepts, so a Trigger aimed at the Line
    must fire on that event. ``occurrence_epoch`` advances exactly when that
    acceptance changes. Compiler revision 32.
    """

    artifact_format: Literal["playbill-line-v6"] = "playbill-line-v6"
    identity: ArtifactIdentity
    occurrence_epoch: int = Field(ge=1, le=2**63 - 1)
    procedure: ArtifactPin
    parameters: object
    acquisition_policy: ArtifactPin | None = None
    max_authority: LineAuthority
    trigger_input: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    trigger_event: CaptureEventSelector | None = None
    budgets: object
    epsilon: object
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    _canonical_objects = field_validator("parameters", "budgets", mode="before")(_canonical_object)
    _epsilon = field_validator("epsilon", mode="before")(_decimal_wrapper)
    _pins = field_validator("pins", check_fields=False)(_sorted_pins)

    @model_validator(mode="after")
    def _shape(self) -> "LineSpec":
        if self.identity.kind != "Line" or not _LINE_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Line identity must be path-addressable and kind Line")
        if self.procedure.role != "procedure" or self.procedure.target.kind != "Procedure":
            raise ValueError("LineSpec procedure must be an exact role=procedure Procedure pin")
        if self.acquisition_policy is not None and (
            self.acquisition_policy.role != ACQUISITION_POLICY_PIN_ROLE
            or self.acquisition_policy.target.kind != "SourceAcquisitionPolicy"
        ):
            raise ValueError("LineSpec acquisition_policy pin has the wrong role or kind")
        required = {self.procedure}
        if self.acquisition_policy is not None:
            required.add(self.acquisition_policy)
        if not required.issubset(set(self.pins)):
            raise ValueError("LineSpec envelope pins do not contain its exact dependencies")
        if (self.trigger_input is None) != (self.trigger_event is None):
            raise ValueError("a trigger input and its accepted trigger_event come together")
        event_pins = tuple(pin for pin in self.pins if pin.role == "trigger-capture-contract")
        expected = (
            ()
            if self.trigger_event is None
            else (
                ArtifactPin(
                    role="trigger-capture-contract",
                    target=self.trigger_event.capture_contract_identity,
                    artifact_digest=self.trigger_event.capture_contract_digest,
                ),
            )
        )
        if event_pins != expected:
            raise ValueError("a Line pins exactly the CaptureContract its trigger_event names")
        for pin in event_pins:
            validate_exact_pin_expectation(
                pin, TRIGGER_CAPTURE_CONTRACT, location="LineSpec trigger_event pin"
            )
        return self


class NamedLineSpec(LineSpec):
    artifact_format: Literal["cruxible-line-v7"] = "cruxible-line-v7"  # type: ignore[assignment]
    procedure: ArtifactNameRef  # type: ignore[assignment]
    bindings: dict[str, ArtifactNameRef] = Field(default_factory=dict, exclude_if=lambda v: not v)
    acquisition_policy: ArtifactNameRef | None = None  # type: ignore[assignment]
    trigger_event: NamedCaptureEventSelector | None = None
    pins: ClassVar[tuple[ArtifactPin, ...]] = ()  # type: ignore[misc]

    @model_validator(mode="after")
    def _shape(self) -> NamedLineSpec:
        if self.identity.kind != "Line" or not _LINE_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Line identity must be path-addressable and kind Line")
        if any(ref.target.kind != "Provider" for ref in self.bindings.values()):
            raise ValueError("Line bindings name Providers")
        if self.procedure.target.kind != "Procedure":
            raise ValueError("a Line names a Procedure")
        if (
            self.acquisition_policy is not None
            and self.acquisition_policy.target.kind != "SourceAcquisitionPolicy"
        ):
            raise ValueError("a Line names an acquisition policy")
        if (self.trigger_input is None) != (self.trigger_event is None):
            raise ValueError("a trigger input and its accepted event come together")
        return self


def line_requested_rung(line: LineSpec) -> Literal[1, 2, 3]:
    """The internal ordering value of what a Line asks to do."""

    return AUTHORITY_RUNG[line.max_authority]


def trigger_capture_source(
    line: LineSpec, procedure: AcceptedProcedure, *, by_name: bool = True
) -> SourceNode:
    """Resolve the single input and verify its closed CaptureContract pin."""
    if line.trigger_input is None:
        raise ValueError("this Line binds no trigger input")
    nodes = [
        n
        for n in procedure.procedure.definition.nodes
        if getattr(n, "as_", None) == line.trigger_input
    ]
    if len(nodes) != 1 or not isinstance(nodes[0], SourceNode):
        raise ValueError("trigger_input must name exactly one Source input")
    node = nodes[0]
    pin = node.capture_contract
    selector = line.trigger_event
    if (
        not isinstance(pin, ArtifactPin | ArtifactNameRef)
        or selector is None
        or (pin.target != selector.capture_contract_identity)
        or (
            not by_name
            and isinstance(pin, ArtifactPin)
            and pin.artifact_digest != selector.capture_contract_digest
        )
    ):
        raise ValueError("trigger input CaptureContract differs from the event selector")
    return node


_LINE_SPEC_ADAPTER: TypeAdapter[LineSpec | NamedLineSpec] = TypeAdapter(LineSpec | NamedLineSpec)


def line_spec_path(name: str) -> str:
    if not _LINE_NAME_RE.fullmatch(name):
        raise LineSpecFormatError("Line identity is not path-addressable")
    return f"lines/{name}.json"


def render_line_spec(line: LineSpec) -> bytes:
    return pretty_canonical_bytes(line.model_dump(mode="json"))


def parse_line_spec(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> LineSpec:
    try:
        line = _LINE_SPEC_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LineSpecFormatError("LineSpec failed strict versioned validation") from exc
    if not artifact_path_matches(line_spec_path(line.identity.name), path, codec=codec):
        raise LineSpecFormatError("LineSpec identity/path disagreement")
    if artifact_bytes_for_path(render_line_spec(line), path, codec=codec) != content:
        raise LineSpecFormatError("LineSpec is not in canonical wire form")
    return line


def line_spec_digest(line: LineSpec) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        line.model_dump(mode="json"),
    )


LINE_IDENTITY_DIGEST_DOMAIN = "playbill-line-journal-partition-v1"


def line_identity_digest(identity: ArtifactIdentity) -> str:
    """Return the stable journal-partition identity of one Line."""

    if identity.kind != "Line":
        raise ValueError("Line identity digest requires kind Line")
    return typed_digest(
        ArtifactDigest,
        LINE_IDENTITY_DIGEST_DOMAIN,
        {"line_identity": identity.model_dump(mode="json")},
    ).tagged


class AcceptedLineSpec(_StrictLineModel):
    path: str
    line: Annotated[LineSpec | NamedLineSpec, Field(discriminator="artifact_format")]
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedLineSpec":
        if self.path != line_spec_path(self.line.identity.name):
            raise ValueError("accepted LineSpec path does not reproduce")
        if self.artifact_digest != line_spec_digest(self.line).tagged:
            raise ValueError("accepted LineSpec digest does not reproduce")
        return self


class LineSpecLawResult(_StrictLineModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> LineSpecLawResult:
    return LineSpecLawResult(
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


def _budget_int(budgets: object, key: str) -> int | None:
    if not isinstance(budgets, dict):
        return None
    value = budgets.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def evaluate_line_spec_law(
    line: LineSpec,
    *,
    path: str,
    procedure: AcceptedProcedure,
    predecessor: AcceptedLineSpec | None,
    default_acquisition_policy: SourceAcquisitionPolicyAny | None = None,
    by_name: bool = False,
    providers: Mapping[str, AcceptedProvider] | None = None,
    provider_interfaces: Mapping[
        str,
        AcceptedProviderInterfaceRegistration,
    ]
    | None = None,
) -> LineSpecLawResult:
    if path != line_spec_path(line.identity.name):
        return _refusal(
            "cruxible.line.path_mismatch", "Line identity/path disagreement.", path=path
        )
    if line.procedure.target != procedure.procedure.identity or (
        not by_name
        and not isinstance(line, NamedLineSpec)
        and line.procedure.artifact_digest != procedure.artifact_digest
    ):
        return _refusal(
            "cruxible.line.procedure_pin_mismatch",
            "LineSpec does not pin the supplied accepted Procedure.",
            path=path,
        )
    definition = procedure.procedure.definition
    if (definition.open_slots or definition.pin_slots) and definition.graph_format == 6:
        return _refusal(
            "cruxible.line.procedure_open_slots",
            "A Line runs an exact Procedure; instantiate the Blueprint first.",
            path=path,
        )
    exhaust_taps = tuple(
        node.node_id for node in definition.nodes if isinstance(node, ExhaustTapNode)
    )
    if exhaust_taps:
        return _refusal(
            "cruxible.line.exhaust_tap_unsupported",
            "No v1 path runs a Procedure with an exhaust_tap node "
            f"({', '.join(exhaust_taps)}); a Line over it could never run.",
            path=path,
        )
    if line.trigger_input is not None:
        try:
            trigger_capture_source(
                line, procedure, by_name=by_name or isinstance(line, NamedLineSpec)
            )
        except ValueError as exc:
            return _refusal("cruxible.line.trigger_input_mismatch", str(exc), path=path)
    if by_name or isinstance(line, NamedLineSpec):
        from cruxible_client.contracts.procedures.contracts import (
            OwnedProcedureContractValidator,
            ProcedureContractValidationError,
        )

        if isinstance(definition.contract_in, ArtifactPin | ArtifactNameRef):
            try:
                OwnedProcedureContractValidator(procedure).validate_contract(
                    contract=definition.contract_in,
                    payload=normalize_canonical(line.parameters),
                    direction="input",
                )
            except ProcedureContractValidationError as exc:
                return _refusal("cruxible.line.parameters_invalid", str(exc), path=path)
    provider_result = _verify_provider_pins(
        definition,
        providers={} if providers is None else providers,
        provider_interfaces={} if provider_interfaces is None else provider_interfaces,
        by_name=by_name or isinstance(line, NamedLineSpec),
        bindings=getattr(line, "bindings", {}),
    )
    if provider_result is not None:
        code, message = provider_result
        return _refusal(code, message, path=path)
    if line_requested_rung(line) > definition.terminal_capability:
        return _refusal(
            "cruxible.line.rung_exceeds_procedure_cap",
            "The Line's max_authority exceeds what its Procedure's terminals can do.",
            path=path,
        )
    caps = definition.hard_caps
    limits = {
        "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
        "max_provider_calls": caps.max_provider_calls,
        "max_capture_bytes": caps.max_capture_bytes,
        "max_items": caps.max_items,
    }
    if caps.max_result_bytes is not None:
        limits["max_result_bytes"] = caps.max_result_bytes
    for key, hard_cap in limits.items():
        value = _budget_int(line.budgets, key)
        if value is None or value < 0 or value > hard_cap:
            return _refusal(
                "cruxible.line.budget_exceeds_procedure_cap",
                f"LineSpec budget {key!r} is missing, invalid, or exceeds the Procedure cap.",
                path=path,
            )
    needs_acquisition = any(isinstance(node, SourceNode) for node in definition.nodes)
    if (
        needs_acquisition
        and line.acquisition_policy is None
        and (
            default_acquisition_policy is None
            or default_acquisition_policy.lifecycle.state != "live"
            or default_acquisition_policy.identity.name != "default"
        )
    ):
        return _refusal(
            "cruxible.line.acquisition_policy_missing",
            "Source paths require SourceAcquisitionPolicy:default or an explicitly named "
            "live policy; "
            "accept the default or name one.",
            path=path,
        )
    if predecessor is None:
        if line.lifecycle.predecessor_digest is not None or line.occurrence_epoch != 1:
            return _refusal(
                "cruxible.line.invalid_genesis",
                "Line genesis requires epoch 1 and no predecessor digest.",
                path=path,
            )
    else:
        if line.identity != predecessor.line.identity:
            return _refusal(
                "cruxible.line.stable_identity_changed",
                "Line successor must retain stable identity.",
                path=path,
            )
        if line.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.line.predecessor_mismatch",
                "Line successor does not pin its exact predecessor.",
                path=path,
            )

        def event_key(value: CaptureEventSelector | None) -> object:
            if not by_name and not isinstance(line, NamedLineSpec):
                return value
            return (
                None if value is None else (value.capture_contract_identity, value.logical_sources)
            )

        acceptance_changed = (line.trigger_input, event_key(line.trigger_event)) != (
            predecessor.line.trigger_input,
            event_key(predecessor.line.trigger_event),
        )
        expected_epoch = predecessor.line.occurrence_epoch + (1 if acceptance_changed else 0)
        if line.occurrence_epoch != expected_epoch:
            return _refusal(
                "cruxible.line.occurrence_epoch_mismatch",
                "Line occurrence epoch must advance exactly when trigger semantics change.",
                path=path,
            )
    return LineSpecLawResult(
        verdict="accepted",
        artifact_digest=line_spec_digest(line).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


def _verify_provider_pins(
    definition: ProcedureDefinition,
    *,
    providers: Mapping[str, AcceptedProvider],
    provider_interfaces: Mapping[str, AcceptedProviderInterfaceRegistration],
    by_name: bool = False,
    bindings: Mapping[str, ArtifactNameRef] | None = None,
) -> tuple[str, str] | None:
    from cruxible_client.contracts.procedures.artifacts import _evaluate_provider_pins

    declared = {
        slot.slot_name: slot for slot in getattr(definition, "_authored", definition).pin_slots
    }
    if set(bindings or {}) - set(declared):
        return (
            "cruxible.procedure.slot_undeclared",
            f"Unknown Provider slots: {sorted(set(bindings or {}) - set(declared))}; "
            f"declared slots: {sorted(declared)}.",
        )
    # The occurrence law reads only graph, interface and provider metadata.
    return _evaluate_provider_pins(
        definition,
        procedure=None,
        providers=providers,
        provider_interfaces=provider_interfaces,
        by_name=by_name,
        bindings=bindings,
    )


__all__ = [
    "AUTHORITY_RUNG",
    "AcceptedLineSpec",
    "LineAuthority",
    "LineSpec",
    "LineSpecFormatError",
    "LineSpecLawResult",
    "RUNG_AUTHORITY",
    "evaluate_line_spec_law",
    "line_identity_digest",
    "line_requested_rung",
    "line_spec_digest",
    "line_spec_path",
    "parse_line_spec",
    "render_line_spec",
    "trigger_capture_source",
]
