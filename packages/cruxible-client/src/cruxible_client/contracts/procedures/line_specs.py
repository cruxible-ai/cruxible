"""Governed LineSpec artifact: one stable instantiation of an accepted Procedure."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_client.contracts.acquisition_policies import ACQUISITION_POLICY_PIN_ROLE
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
    normalize_canonical,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.closure import (
    LineSlotBinding,
    ProcedurePinClosureError,
    ProviderExtrasEnvironmentPinMap,
    ProviderImplementationClosure,
    close_procedure_pin_slots,
)
from cruxible_client.contracts.procedures.models import (
    AUTHORITY_RUNG,
    RUNG_AUTHORITY,
    AuthorityVerb,
    ExhaustTapNode,
    ProcedureDefinitionV4,
    ProcedurePinSlotRef,
    ProviderNode,
    RepeatBodyNodeV4,
    RepeatNodeV4,
    SourceNode,
    SourceNodeV3,
)
from cruxible_client.contracts.procedures.pin_expectations import (
    TRIGGER_CADENCE_POLICY,
    TRIGGER_CAPTURE_CONTRACT,
    TRIGGER_LANDING_FILTER,
    TRIGGER_WINDOW_POLICY,
    PinExpectation,
    validate_exact_pin_expectation,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelector,
    CaptureEventWindow,
    ObservationWindow,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
)
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    ProviderLocalMaterializationReference,
    ProviderV2,
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


class CadenceTriggerPolicy(_StrictLineModel):
    """A cadence trigger whose period is part of the accepted Line artifact.

    ``cadence_policy_digest`` still pins the governing Policy exactly, but the
    Policy pin target kind has no ledger artifact envelope, so nothing in an
    accepted tree can be read for the period. The period therefore lives here,
    on the artifact the registry does produce, and is accepted, digested, and
    succeeded with the Line itself.
    """

    tag: Literal["playbill-cadence-trigger-v1"] = "playbill-cadence-trigger-v1"
    kind: Literal["cadence"] = "cadence"
    cadence_policy_digest: str
    interval_seconds: int = Field(
        gt=0,
        description="Reads VALIDITY WINDOW.",
    )

    _digest = field_validator("cadence_policy_digest")(_artifact_digest)


class CaptureLandingTriggerPolicyV1(_StrictLineModel):
    tag: Literal["playbill-capture-landing-trigger-v1"] = "playbill-capture-landing-trigger-v1"
    kind: Literal["capture_landing"] = "capture_landing"
    anchor_capture_contract_digest: str
    landing_filter_digest: str

    _digests = field_validator("anchor_capture_contract_digest", "landing_filter_digest")(
        _artifact_digest
    )


class WindowCloseTriggerPolicyV1(_StrictLineModel):
    """A window-close trigger whose window is part of the accepted Line artifact."""

    tag: Literal["playbill-window-close-trigger-v1"] = "playbill-window-close-trigger-v1"
    kind: Literal["window_close"] = "window_close"
    window_policy_digest: str
    window_seconds: int = Field(
        gt=0,
        description="Reads VALIDITY WINDOW.",
    )

    _digest = field_validator("window_policy_digest")(_artifact_digest)


class CaptureLandingTriggerPolicy(_StrictLineModel):
    tag: Literal["playbill-capture-landing-trigger-v2"] = "playbill-capture-landing-trigger-v2"
    kind: Literal["capture_landing"] = "capture_landing"
    event: CaptureEventSelector


class WindowCloseTriggerPolicy(_StrictLineModel):
    tag: Literal["playbill-window-close-trigger-v2"] = "playbill-window-close-trigger-v2"
    kind: Literal["window_close"] = "window_close"
    window: ObservationWindow


class ManualTriggerPolicy(_StrictLineModel):
    tag: Literal["playbill-manual-trigger-v1"] = "playbill-manual-trigger-v1"
    kind: Literal["manual"] = "manual"


TriggerPolicyV1 = Annotated[
    CadenceTriggerPolicy
    | CaptureLandingTriggerPolicyV1
    | WindowCloseTriggerPolicyV1
    | ManualTriggerPolicy,
    Field(discriminator="kind"),
]


TriggerPolicy: TypeAlias = Annotated[
    CadenceTriggerPolicy
    | CaptureLandingTriggerPolicy
    | WindowCloseTriggerPolicy
    | ManualTriggerPolicy,
    Field(discriminator="kind"),
]


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


def _sorted_bindings(value: tuple[LineSlotBinding, ...]) -> tuple[LineSlotBinding, ...]:
    names = tuple(item.slot_name for item in value)
    if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
        raise ValueError("LineSpec slot bindings must be sorted and unique")
    return value


def _sorted_pins(value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
    if value != tuple(sorted(value, key=_pin_key)):
        raise ValueError("LineSpec pins must be canonically sorted")
    keys = tuple((pin.role, pin.target.qualified) for pin in value)
    if len(set(keys)) != len(keys):
        raise ValueError("LineSpec pins must be unique by role and target")
    return value


def _sorted_closures(
    value: tuple[ProviderImplementationClosure, ...],
) -> tuple[ProviderImplementationClosure, ...]:
    def closure_key(item: ProviderImplementationClosure) -> tuple[bytes, bytes]:
        return item.node_id.encode("utf-8"), item.slot_name.encode("utf-8")

    if value != tuple(sorted(value, key=closure_key)):
        raise ValueError("Line Provider closures must be canonically node/slot sorted")
    coordinates = tuple((item.node_id, item.slot_name) for item in value)
    if len(coordinates) != len(set(coordinates)):
        raise ValueError("Line Provider closures must be node/slot unique")
    return value


class LineSpecV1(_StrictLineModel):
    artifact_format: Literal["playbill-line-v1"] = "playbill-line-v1"
    identity: ArtifactIdentity
    occurrence_epoch: int = Field(ge=1, le=2**63 - 1)
    procedure: ArtifactPin
    parameters: object
    slot_bindings: tuple[LineSlotBinding, ...]
    trigger_policy: TriggerPolicyV1
    acquisition_policy: ArtifactPin | None = None
    requested_terminal_rung: Literal[1, 2, 3]
    budgets: object
    epsilon: object
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    _canonical_objects = field_validator("parameters", "budgets", mode="before")(_canonical_object)
    _epsilon = field_validator("epsilon", mode="before")(_decimal_wrapper)
    _bindings = field_validator("slot_bindings")(_sorted_bindings)
    _pins = field_validator("pins")(_sorted_pins)

    @model_validator(mode="after")
    def _shape(self) -> "LineSpecV1":
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
        required.update(binding.artifact_pin for binding in self.slot_bindings)
        if not required.issubset(set(self.pins)):
            raise ValueError("LineSpec envelope pins do not contain its exact dependencies")
        for role, digest, expectation in _trigger_pin_requirements(self.trigger_policy):
            matches = tuple(
                pin for pin in self.pins if pin.role == role and pin.artifact_digest == digest
            )
            if len(matches) != 1:
                raise ValueError(
                    "LineSpec trigger digest lacks one exact role-named pin: "
                    f"role={role!r} digest={digest!r}"
                )
            validate_exact_pin_expectation(
                matches[0],
                expectation,
                location=f"LineSpec trigger pin {role!r}",
            )
        return self


class LineSpecV2(LineSpecV1):
    """Line successor freezing every graph-v4 Provider occurrence closure."""

    artifact_format: Literal["playbill-line-v2"] = "playbill-line-v2"  # type: ignore[assignment]
    provider_implementation_closures: tuple[ProviderImplementationClosure, ...]

    _provider_closures = field_validator("provider_implementation_closures")(_sorted_closures)


class LineSpecV3(LineSpecV2):
    """Line with verifiable event triggers and fixed observation windows."""

    artifact_format: Literal["playbill-line-v3"] = "playbill-line-v3"  # type: ignore[assignment]
    trigger_policy: TriggerPolicy  # type: ignore[assignment]


LineAuthority = AuthorityVerb


class LineSpecV4(LineSpecV3):
    """Bind the triggering Capture to one named Source input, without re-fetching.

    Compiler revision 30's Line format, retained exactly for accepted history.
    """

    artifact_format: Literal["playbill-line-v4"] = "playbill-line-v4"  # type: ignore[assignment]
    trigger_input: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")

    @model_validator(mode="after")
    def _event_input(self) -> "LineSpecV4":
        if trigger_capture_selector(self) is None:
            raise ValueError("a trigger input requires a Capture event trigger or event window")
        return self


class LineSpecV5(LineSpecV3):
    """A Line that states its authority as a verb and may consume its triggering Capture.

    ``max_authority`` caps what this Line may do below its Procedure's own
    capability; authoring defaults it to that capability, so the artifact always
    states it. ``trigger_input`` optionally binds the event's exact Capture to
    one named Source input, without re-fetching. Compiler revision 31.
    """

    artifact_format: Literal["playbill-line-v5"] = "playbill-line-v5"  # type: ignore[assignment]
    requested_terminal_rung: None = None  # type: ignore[assignment]
    max_authority: LineAuthority
    trigger_input: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")

    @model_validator(mode="after")
    def _event_input(self) -> "LineSpecV5":
        if self.trigger_input is not None and trigger_capture_selector(self) is None:
            raise ValueError("a trigger input requires a Capture event trigger or event window")
        return self

    @model_serializer(mode="wrap")
    def _wire(self, handler: Any) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        # The numeric rung is not part of this wire; the verb is.
        data.pop("requested_terminal_rung", None)
        return data


class LineSpec(_StrictLineModel):
    """A Line with no embedded trigger: Trigger artifacts aim at it by identity.

    When it runs is no longer the Line's to say; every Trigger aimed at it is its
    own governed artifact, and a Line with none runs only when run explicitly.
    What stays with the Line is what it accepts: ``trigger_input`` binds the
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
    slot_bindings: tuple[LineSlotBinding, ...]
    acquisition_policy: ArtifactPin | None = None
    max_authority: LineAuthority
    trigger_input: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    trigger_event: CaptureEventSelector | None = None
    budgets: object
    epsilon: object
    pins: tuple[ArtifactPin, ...]
    provider_implementation_closures: tuple[ProviderImplementationClosure, ...]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    _canonical_objects = field_validator("parameters", "budgets", mode="before")(_canonical_object)
    _epsilon = field_validator("epsilon", mode="before")(_decimal_wrapper)
    _bindings = field_validator("slot_bindings")(_sorted_bindings)
    _pins = field_validator("pins")(_sorted_pins)
    _provider_closures = field_validator("provider_implementation_closures")(_sorted_closures)

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
        required.update(binding.artifact_pin for binding in self.slot_bindings)
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


def line_requested_rung(line: "LineSpecV1 | LineSpec") -> Literal[1, 2, 3]:
    """The internal ordering value of what a Line asks to do, for either generation."""

    if isinstance(line, LineSpecV5 | LineSpec):
        return AUTHORITY_RUNG[line.max_authority]
    rung = line.requested_terminal_rung
    assert rung is not None
    return rung


def trigger_capture_selector(line: "LineSpecV3 | LineSpec") -> CaptureEventSelector | None:
    if isinstance(line, LineSpec):
        return line.trigger_event
    trigger = line.trigger_policy
    if isinstance(trigger, CaptureLandingTriggerPolicy):
        return trigger.event
    if isinstance(trigger, WindowCloseTriggerPolicy) and isinstance(
        trigger.window, CaptureEventWindow
    ):
        return trigger.window.event
    return None


def trigger_capture_source(
    line: "LineSpecV4 | LineSpecV5 | LineSpec", procedure: AcceptedProcedure
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
        raise ValueError("trigger_input must name exactly one graph-v4 Source input")
    node = nodes[0]
    binding = node.capture_contract
    pin: ArtifactPin | None = (
        next((b.artifact_pin for b in line.slot_bindings if b.slot_name == binding.slot_name), None)
        if isinstance(binding, ProcedurePinSlotRef)
        else binding
    )
    selector = trigger_capture_selector(line)
    if (
        pin is None
        or selector is None
        or (
            pin.target != selector.capture_contract_identity
            or pin.artifact_digest != selector.capture_contract_digest
        )
    ):
        raise ValueError("trigger input CaptureContract differs from the event selector")
    return node


LineSpecAny: TypeAlias = Annotated[
    LineSpecV1 | LineSpecV2 | LineSpecV3 | LineSpecV4 | LineSpecV5 | LineSpec,
    Field(discriminator="artifact_format"),
]
#: The Line formats that embed their own trigger, retained only to read history.
EMBEDDED_TRIGGER_LINE_FORMATS = frozenset(
    {
        "playbill-line-v1",
        "playbill-line-v2",
        "playbill-line-v3",
        "playbill-line-v4",
        "playbill-line-v5",
    }
)


def line_has_provider_closures(line: LineSpecAny) -> bool:
    """Whether a Line freezes every graph-v4 Provider occurrence closure (v2 onward)."""

    return isinstance(line, LineSpecV2 | LineSpec)


def _line_acceptance(line: LineSpecAny) -> object:
    """What decides a Line's occurrence identities: its epoch advances exactly on change.

    An embedded-trigger Line's trigger policy; a v6 Line's accepted event binding.
    """

    if isinstance(line, LineSpec):
        return ("accepts", line.trigger_input, line.trigger_event)
    return line.trigger_policy


_LINE_SPEC_ADAPTER: TypeAdapter[LineSpecAny] = TypeAdapter(LineSpecAny)


def _trigger_pin_requirements(
    trigger: TriggerPolicyV1 | TriggerPolicy,
) -> tuple[tuple[str, str, PinExpectation], ...]:
    if isinstance(trigger, (CaptureLandingTriggerPolicy, WindowCloseTriggerPolicy)):
        selector = (
            trigger.event
            if isinstance(trigger, CaptureLandingTriggerPolicy)
            else (trigger.window.event if isinstance(trigger.window, CaptureEventWindow) else None)
        )
        return (
            ()
            if selector is None
            else (
                (
                    "trigger-capture-contract",
                    selector.capture_contract_digest,
                    TRIGGER_CAPTURE_CONTRACT,
                ),
            )
        )
    if isinstance(trigger, CadenceTriggerPolicy):
        return (
            (
                "trigger-cadence-policy",
                trigger.cadence_policy_digest,
                TRIGGER_CADENCE_POLICY,
            ),
        )
    if isinstance(trigger, CaptureLandingTriggerPolicyV1):
        return (
            (
                "trigger-capture-contract",
                trigger.anchor_capture_contract_digest,
                TRIGGER_CAPTURE_CONTRACT,
            ),
            (
                "trigger-landing-filter",
                trigger.landing_filter_digest,
                TRIGGER_LANDING_FILTER,
            ),
        )
    if isinstance(trigger, WindowCloseTriggerPolicyV1):
        return (
            (
                "trigger-window-policy",
                trigger.window_policy_digest,
                TRIGGER_WINDOW_POLICY,
            ),
        )
    return ()


def line_spec_path(name: str) -> str:
    if not _LINE_NAME_RE.fullmatch(name):
        raise LineSpecFormatError("Line identity is not path-addressable")
    return f"lines/{name}.json"


def render_line_spec(line: LineSpecAny) -> bytes:
    return pretty_canonical_bytes(line.model_dump(mode="json"))


def parse_line_spec(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> LineSpecAny:
    try:
        line = _LINE_SPEC_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LineSpecFormatError("LineSpec failed strict versioned validation") from exc
    if not artifact_path_matches(line_spec_path(line.identity.name), path, codec=codec):
        raise LineSpecFormatError("LineSpec identity/path disagreement")
    if artifact_bytes_for_path(render_line_spec(line), path, codec=codec) != content:
        raise LineSpecFormatError("LineSpec is not in canonical wire form")
    return line


def line_spec_digest(line: LineSpecAny) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        line.model_dump(mode="json"),
    )


LINE_IDENTITY_DIGEST_DOMAIN = "playbill-line-journal-partition-v1"


def line_identity_digest(identity: ArtifactIdentity) -> str:
    """Return the stable identity carried by served Line-run requests."""

    if identity.kind != "Line":
        raise ValueError("Line identity digest requires kind Line")
    return typed_digest(
        ArtifactDigest,
        LINE_IDENTITY_DIGEST_DOMAIN,
        {"line_identity": identity.model_dump(mode="json")},
    ).tagged


class AcceptedLineSpec(_StrictLineModel):
    path: str
    line: LineSpecAny
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
    line: LineSpecAny,
    *,
    path: str,
    procedure: AcceptedProcedure,
    interface_digests: dict[str, str],
    predecessor: AcceptedLineSpec | None,
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
    if line.procedure.artifact_digest != procedure.artifact_digest:
        return _refusal(
            "cruxible.line.procedure_pin_mismatch",
            "LineSpec does not pin the supplied accepted Procedure.",
            path=path,
        )
    try:
        close_procedure_pin_slots(
            procedure.procedure,
            bindings=line.slot_bindings,
            interface_digests=interface_digests,
        )
    except ProcedurePinClosureError as exc:
        return _refusal("cruxible.line.slot_closure_failed", str(exc), path=path)
    definition = procedure.procedure.definition
    if isinstance(line, LineSpecV4 | LineSpecV5 | LineSpec) and line.trigger_input is not None:
        try:
            trigger_capture_source(line, procedure)
        except ValueError as exc:
            return _refusal("cruxible.line.trigger_input_mismatch", str(exc), path=path)
    if isinstance(definition, ProcedureDefinitionV4):
        if not line_has_provider_closures(line):
            return _refusal(
                "cruxible.line.provider_closure_successor_required",
                "A graph-v4 Procedure requires a playbill-line-v2 closure.",
                path=path,
            )
        assert isinstance(line, LineSpecV2 | LineSpec)
        provider_result = _verify_provider_implementation_closures(
            line,
            definition=definition,
            providers={} if providers is None else providers,
            provider_interfaces={} if provider_interfaces is None else provider_interfaces,
        )
        if provider_result is not None:
            code, message = provider_result
            return _refusal(code, message, path=path)
    elif isinstance(line, LineSpecV2) or (
        isinstance(line, LineSpec) and line.provider_implementation_closures
    ):
        # A v6 Line serves every Procedure a Line could: an earlier graph has
        # no Provider occurrence to close, so it carries no closures.
        return _refusal(
            "cruxible.line.graph_v4_required",
            "Provider implementation closures must instantiate a graph-v4 Procedure.",
            path=path,
        )
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
    needs_acquisition = any(
        isinstance(node, SourceNodeV3 | SourceNode | ExhaustTapNode) for node in definition.nodes
    )
    if needs_acquisition and line.acquisition_policy is None:
        return _refusal(
            "cruxible.line.acquisition_policy_missing",
            "Source and exhaust paths require an exact SourceAcquisitionPolicy pin.",
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
        if (
            line_has_provider_closures(predecessor.line) and not line_has_provider_closures(line)
        ) or (isinstance(predecessor.line, LineSpec) and not isinstance(line, LineSpec)):
            return _refusal(
                "cruxible.line.wire_downgrade",
                "A Line lineage cannot be succeeded by an earlier Line wire.",
                path=path,
            )
        if line.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.line.predecessor_mismatch",
                "Line successor does not pin its exact predecessor.",
                path=path,
            )
        trigger_changed = _line_acceptance(line) != _line_acceptance(predecessor.line)
        expected_epoch = predecessor.line.occurrence_epoch + (1 if trigger_changed else 0)
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


ProviderOccurrence: TypeAlias = SourceNode | ProviderNode | RepeatBodyNodeV4


def _provider_occurrences(
    definition: ProcedureDefinitionV4,
) -> tuple[tuple[str, ProviderOccurrence], ...]:
    occurrences: list[tuple[str, ProviderOccurrence]] = []
    for node in definition.nodes:
        if isinstance(node, SourceNode | ProviderNode):
            occurrences.append((node.node_id, node))
        elif isinstance(node, RepeatNodeV4):
            occurrences.extend(
                (f"{node.node_id}.{body.node_id}", body)
                for body in node.body
                if body.operation in {"provider", "call"}
            )
    return tuple(sorted(occurrences, key=lambda item: item[0].encode("utf-8")))


def _slot_provider_occurrences(
    definition: ProcedureDefinitionV4,
) -> tuple[tuple[str, ProviderOccurrence], ...]:
    result: list[tuple[str, ProviderOccurrence]] = []
    for node_id, node in _provider_occurrences(definition):
        if isinstance(node.provider, ProcedurePinSlotRef):
            result.append((node_id, node))
    return tuple(result)


def _verify_provider_implementation_closures(
    line: LineSpecV2 | LineSpec,
    *,
    definition: ProcedureDefinitionV4,
    providers: Mapping[str, AcceptedProvider],
    provider_interfaces: Mapping[str, AcceptedProviderInterfaceRegistration],
) -> tuple[str, str] | None:
    occurrences = _provider_occurrences(definition)
    slot_occurrences = _slot_provider_occurrences(definition)
    occurrence_coordinates: list[tuple[str, str]] = []
    for node_id, node in slot_occurrences:
        provider = node.provider
        if not isinstance(provider, ProcedurePinSlotRef):  # pragma: no cover - filtered above
            raise AssertionError("slot occurrence lost its slot binding")
        occurrence_coordinates.append((node_id, provider.slot_name))
    closure_coordinates = tuple(
        (item.node_id, item.slot_name) for item in line.provider_implementation_closures
    )
    if tuple(occurrence_coordinates) != closure_coordinates:
        return (
            "cruxible.line.provider_implementation_closure_incomplete",
            "Line Provider closures must cover every slot-filled "
            "Source/Provider occurrence exactly.",
        )
    bindings = {item.slot_name: item.artifact_pin for item in line.slot_bindings}
    closures = {item.node_id: item for item in line.provider_implementation_closures}
    for node_id, node in occurrences:
        provider_binding = getattr(node, "provider")
        interface_pin = getattr(node, "interface")
        interface_digest = getattr(node, "interface_digest")
        node_implementation_digest = getattr(node, "implementation_digest")
        closure = closures.get(node_id)
        provider_pin: ArtifactPin | None
        if isinstance(provider_binding, ArtifactPin):
            provider_pin = provider_binding
            implementation_digest = node_implementation_digest
        else:
            if not isinstance(provider_binding, ProcedurePinSlotRef):
                return (
                    "cruxible.line.provider_interface_pin_mismatch",
                    f"Provider occurrence {node_id!r} has an unsupported binding.",
                )
            provider_pin = bindings.get(provider_binding.slot_name)
            slot_name = provider_binding.slot_name
            if closure is None or closure.slot_name != slot_name:
                return (
                    "cruxible.line.provider_implementation_closure_incomplete",
                    f"Provider occurrence {node_id!r} lacks its exact slot closure.",
                )
            implementation_digest = closure.implementation_digest
        if provider_pin is None:
            return (
                "cruxible.line.provider_implementation_unavailable",
                f"Provider occurrence {node_id!r} has no exact Provider pin.",
            )
        accepted_provider = providers.get(provider_pin.artifact_digest)
        if accepted_provider is None or not isinstance(accepted_provider.provider, ProviderV2):
            return (
                "cruxible.line.provider_runtime_manifest_required",
                f"Provider occurrence {node_id!r} does not bind an accepted Provider v2.",
            )
        accepted_interface = provider_interfaces.get(interface_pin.artifact_digest)
        if accepted_interface is None:
            return (
                "cruxible.line.provider_interface_pin_mismatch",
                f"Provider occurrence {node_id!r} lacks its accepted interface registration.",
            )
        registration = accepted_interface.registration
        if registration.interface_digest != interface_digest:
            return (
                "cruxible.line.provider_interface_pin_mismatch",
                f"Provider occurrence {node_id!r} interface digest does not reproduce.",
            )
        matches = tuple(
            record
            for record in accepted_provider.provider.implementations
            if record.implementation_digest == implementation_digest
            and record.interface_id == registration.interface_id
            and record.interface_digest == interface_digest
        )
        if not matches:
            return (
                "cruxible.line.provider_implementation_unavailable",
                f"Provider occurrence {node_id!r} implementation is unavailable.",
            )
        if len(matches) != 1:
            return (
                "cruxible.line.provider_implementation_ambiguous",
                f"Provider occurrence {node_id!r} implementation is ambiguous.",
            )
        record = matches[0]
        manifest_matches = tuple(
            item
            for item in accepted_provider.provider.runtime_artifact.manifest.implementations
            if item.interface_id == record.interface_id and item.entrypoint == record.entrypoint
        )
        if len(manifest_matches) != 1:
            return (
                "cruxible.line.provider_implementation_ambiguous",
                f"Provider occurrence {node_id!r} manifest row is not singular.",
            )
        manifest = manifest_matches[0]
        expected_environment_map = ProviderExtrasEnvironmentPinMap(
            required_extras=tuple(
                sorted(manifest.requires_extras, key=lambda item: item.encode("utf-8"))
            ),
            eligible_environment_pin_keys=tuple(
                reference.environment_pin_key
                for reference in record.materialization_references
                if isinstance(reference, ProviderLocalMaterializationReference)
            ),
        )
        if closure is not None:
            expected = {
                "slot_name": slot_name,
                "provider_artifact_digest": provider_pin.artifact_digest,
                "interface_artifact_digest": interface_pin.artifact_digest,
                "interface_digest": interface_digest,
                "implementation_digest": implementation_digest,
                "environment_pin_map": expected_environment_map,
            }
            if any(getattr(closure, key) != value for key, value in expected.items()):
                return (
                    "cruxible.line.provider_implementation_pin_mismatch",
                    f"Provider occurrence {node_id!r} closure does not reproduce exact pins.",
                )
    return None


__all__ = [
    "AcceptedLineSpec",
    "CadenceTriggerPolicy",
    "CaptureLandingTriggerPolicyV1",
    "LineSpecFormatError",
    "LineSpecLawResult",
    "LineSpecAny",
    "LineSpecV1",
    "LineSpecV2",
    "LineSpecV3",
    "LineSpecV4",
    "LineSpecV5",
    "LineSpec",
    "EMBEDDED_TRIGGER_LINE_FORMATS",
    "line_has_provider_closures",
    "AUTHORITY_RUNG",
    "LineAuthority",
    "RUNG_AUTHORITY",
    "line_requested_rung",
    "trigger_capture_selector",
    "trigger_capture_source",
    "TriggerPolicy",
    "CaptureLandingTriggerPolicy",
    "WindowCloseTriggerPolicy",
    "LINE_IDENTITY_DIGEST_DOMAIN",
    "ManualTriggerPolicy",
    "TriggerPolicyV1",
    "WindowCloseTriggerPolicyV1",
    "evaluate_line_spec_law",
    "line_identity_digest",
    "line_spec_digest",
    "line_spec_path",
    "parse_line_spec",
    "render_line_spec",
]
