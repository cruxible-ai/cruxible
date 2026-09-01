"""Governed LineSpec artifact: one stable instantiation of an accepted Procedure."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

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
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.closure import (
    LineSlotBindingV1,
    ProcedurePinClosureError,
    ProviderExtrasEnvironmentPinMapV1,
    ProviderImplementationClosureV1,
    close_procedure_pin_slots,
)
from cruxible_client.contracts.procedures.models import (
    ExhaustTapNodeV3,
    ProcedureDefinitionV4,
    ProcedurePinSlotRefV1,
    ProviderNodeV4,
    RepeatBodyNodeV4,
    RepeatNodeV4,
    SourceNodeV3,
    SourceNodeV4,
)
from cruxible_client.contracts.procedures.pin_expectations import (
    TRIGGER_CADENCE_POLICY,
    TRIGGER_CAPTURE_CONTRACT,
    TRIGGER_LANDING_FILTER,
    TRIGGER_WINDOW_POLICY,
    PinExpectation,
    validate_exact_pin_expectation,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderLocalMaterializationReferenceV1,
    ProviderV2,
)
from cruxible_client.contracts.semantic import SemanticAddress

_LINE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class LineSpecFormatError(PlaybillFormatError):
    """A LineSpec artifact, closure, or successor transition is invalid."""


class _StrictLineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _artifact_digest(value: str) -> str:
    ArtifactDigest.from_tagged(value)
    return value


class CadenceTriggerPolicyV1(_StrictLineModel):
    tag: Literal["playbill-cadence-trigger-v1"] = "playbill-cadence-trigger-v1"
    kind: Literal["cadence"] = "cadence"
    cadence_policy_digest: str

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
    tag: Literal["playbill-window-close-trigger-v1"] = "playbill-window-close-trigger-v1"
    kind: Literal["window_close"] = "window_close"
    window_policy_digest: str

    _digest = field_validator("window_policy_digest")(_artifact_digest)


class ManualTriggerPolicyV1(_StrictLineModel):
    tag: Literal["playbill-manual-trigger-v1"] = "playbill-manual-trigger-v1"
    kind: Literal["manual"] = "manual"


TriggerPolicyV1 = Annotated[
    CadenceTriggerPolicyV1
    | CaptureLandingTriggerPolicyV1
    | WindowCloseTriggerPolicyV1
    | ManualTriggerPolicyV1,
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


class LineSpecV1(_StrictLineModel):
    artifact_format: Literal["playbill-line-v1"] = "playbill-line-v1"
    identity: ArtifactIdentity
    occurrence_epoch: int = Field(ge=1, le=2**63 - 1)
    procedure: ArtifactPin
    parameters: object
    slot_bindings: tuple[LineSlotBindingV1, ...]
    trigger_policy: TriggerPolicyV1
    acquisition_policy: ArtifactPin | None = None
    requested_terminal_rung: Literal[1, 2, 3]
    budgets: object
    epsilon: object
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("parameters", "budgets", mode="before")
    @classmethod
    def _canonical_objects(cls, value: object) -> object:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("LineSpec parameters and budgets must be canonical objects")
        return normalized

    @field_validator("epsilon", mode="before")
    @classmethod
    def _epsilon(cls, value: object) -> object:
        return _decimal_wrapper(value)

    @field_validator("slot_bindings")
    @classmethod
    def _bindings(cls, value: tuple[LineSlotBindingV1, ...]) -> tuple[LineSlotBindingV1, ...]:
        names = tuple(item.slot_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("LineSpec slot bindings must be sorted and unique")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("LineSpec pins must be canonically sorted")
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(set(keys)) != len(keys):
            raise ValueError("LineSpec pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "LineSpecV1":
        if self.identity.kind != "Line" or not _LINE_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Line identity must be path-addressable and kind Line")
        if self.procedure.role != "procedure" or self.procedure.target.kind != "Procedure":
            raise ValueError("LineSpec procedure must be an exact role=procedure Procedure pin")
        if self.acquisition_policy is not None and (
            self.acquisition_policy.role != "acquisition-policy"
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
    provider_implementation_closures: tuple[ProviderImplementationClosureV1, ...]

    @field_validator("provider_implementation_closures")
    @classmethod
    def _provider_closures(
        cls,
        value: tuple[ProviderImplementationClosureV1, ...],
    ) -> tuple[ProviderImplementationClosureV1, ...]:
        def closure_key(
            item: ProviderImplementationClosureV1,
        ) -> tuple[bytes, bytes]:
            return item.node_id.encode("utf-8"), item.slot_name.encode("utf-8")

        if value != tuple(sorted(value, key=closure_key)):
            raise ValueError("Line Provider closures must be canonically node/slot sorted")
        coordinates = tuple((item.node_id, item.slot_name) for item in value)
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("Line Provider closures must be node/slot unique")
        return value


LineSpecAny: TypeAlias = Annotated[
    LineSpecV1 | LineSpecV2,
    Field(discriminator="artifact_format"),
]
_LINE_SPEC_ADAPTER: TypeAdapter[LineSpecAny] = TypeAdapter(LineSpecAny)


def _trigger_pin_requirements(
    trigger: TriggerPolicyV1,
) -> tuple[tuple[str, str, PinExpectation], ...]:
    if isinstance(trigger, CadenceTriggerPolicyV1):
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


class AcceptedLineSpecV1(_StrictLineModel):
    path: str
    line: LineSpecAny
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedLineSpecV1":
        if self.path != line_spec_path(self.line.identity.name):
            raise ValueError("accepted LineSpec path does not reproduce")
        if self.artifact_digest != line_spec_digest(self.line).tagged:
            raise ValueError("accepted LineSpec digest does not reproduce")
        return self


class LineSpecLawResultV1(_StrictLineModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> LineSpecLawResultV1:
    return LineSpecLawResultV1(
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
    procedure: AcceptedProcedureV1,
    interface_digests: dict[str, str],
    predecessor: AcceptedLineSpecV1 | None,
    providers: Mapping[str, AcceptedProviderV1] | None = None,
    provider_interfaces: Mapping[
        str,
        AcceptedProviderInterfaceRegistrationV1,
    ]
    | None = None,
) -> LineSpecLawResultV1:
    if path != line_spec_path(line.identity.name):
        return _refusal(
            "playbill.line.path_mismatch", "Line identity/path disagreement.", path=path
        )
    if line.procedure.artifact_digest != procedure.artifact_digest:
        return _refusal(
            "playbill.line.procedure_pin_mismatch",
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
        return _refusal("playbill.line.slot_closure_failed", str(exc), path=path)
    definition = procedure.procedure.definition
    if isinstance(definition, ProcedureDefinitionV4):
        if not isinstance(line, LineSpecV2):
            return _refusal(
                "playbill.line.provider_closure_successor_required",
                "A graph-v4 Procedure requires a playbill-line-v2 closure.",
                path=path,
            )
        provider_result = _verify_provider_implementation_closures(
            line,
            definition=definition,
            providers={} if providers is None else providers,
            provider_interfaces={} if provider_interfaces is None else provider_interfaces,
        )
        if provider_result is not None:
            code, message = provider_result
            return _refusal(code, message, path=path)
    elif isinstance(line, LineSpecV2):
        return _refusal(
            "playbill.line.graph_v4_required",
            "A playbill-line-v2 closure must instantiate a graph-v4 Procedure.",
            path=path,
        )
    if line.requested_terminal_rung > definition.terminal_capability:
        return _refusal(
            "playbill.line.rung_exceeds_procedure_cap",
            "LineSpec requested terminal rung exceeds the Procedure hard cap.",
            path=path,
        )
    caps = definition.hard_caps
    limits = {
        "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
        "max_provider_calls": caps.max_provider_calls,
        "max_capture_bytes": caps.max_capture_bytes,
        "max_items": caps.max_items,
    }
    for key, hard_cap in limits.items():
        value = _budget_int(line.budgets, key)
        if value is None or value < 0 or value > hard_cap:
            return _refusal(
                "playbill.line.budget_exceeds_procedure_cap",
                f"LineSpec budget {key!r} is missing, invalid, or exceeds the Procedure cap.",
                path=path,
            )
    needs_acquisition = any(
        isinstance(node, SourceNodeV3 | SourceNodeV4 | ExhaustTapNodeV3)
        for node in definition.nodes
    )
    if needs_acquisition and line.acquisition_policy is None:
        return _refusal(
            "playbill.line.acquisition_policy_missing",
            "Source and exhaust paths require an exact SourceAcquisitionPolicy pin.",
            path=path,
        )
    if predecessor is None:
        if line.lifecycle.predecessor_digest is not None or line.occurrence_epoch != 1:
            return _refusal(
                "playbill.line.invalid_genesis",
                "Line genesis requires epoch 1 and no predecessor digest.",
                path=path,
            )
    else:
        if line.identity != predecessor.line.identity:
            return _refusal(
                "playbill.line.stable_identity_changed",
                "Line successor must retain stable identity.",
                path=path,
            )
        if isinstance(predecessor.line, LineSpecV2) and not isinstance(line, LineSpecV2):
            return _refusal(
                "playbill.line.wire_downgrade",
                "A Line v2 lineage cannot be succeeded by the historical v1 wire.",
                path=path,
            )
        if line.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "playbill.line.predecessor_mismatch",
                "Line successor does not pin its exact predecessor.",
                path=path,
            )
        trigger_changed = line.trigger_policy != predecessor.line.trigger_policy
        expected_epoch = predecessor.line.occurrence_epoch + (1 if trigger_changed else 0)
        if line.occurrence_epoch != expected_epoch:
            return _refusal(
                "playbill.line.occurrence_epoch_mismatch",
                "Line occurrence epoch must advance exactly when trigger semantics change.",
                path=path,
            )
    return LineSpecLawResultV1(
        verdict="accepted",
        artifact_digest=line_spec_digest(line).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


ProviderOccurrenceV4: TypeAlias = SourceNodeV4 | ProviderNodeV4 | RepeatBodyNodeV4


def _provider_occurrences(
    definition: ProcedureDefinitionV4,
) -> tuple[tuple[str, ProviderOccurrenceV4], ...]:
    occurrences: list[tuple[str, ProviderOccurrenceV4]] = []
    for node in definition.nodes:
        if isinstance(node, SourceNodeV4 | ProviderNodeV4):
            occurrences.append((node.node_id, node))
        elif isinstance(node, RepeatNodeV4):
            occurrences.extend(
                (f"{node.node_id}.{body.node_id}", body)
                for body in node.body
                if body.operation == "provider"
            )
    return tuple(sorted(occurrences, key=lambda item: item[0].encode("utf-8")))


def _slot_provider_occurrences(
    definition: ProcedureDefinitionV4,
) -> tuple[tuple[str, ProviderOccurrenceV4], ...]:
    result: list[tuple[str, ProviderOccurrenceV4]] = []
    for node_id, node in _provider_occurrences(definition):
        if isinstance(node.provider, ProcedurePinSlotRefV1):
            result.append((node_id, node))
    return tuple(result)


def _verify_provider_implementation_closures(
    line: LineSpecV2,
    *,
    definition: ProcedureDefinitionV4,
    providers: Mapping[str, AcceptedProviderV1],
    provider_interfaces: Mapping[str, AcceptedProviderInterfaceRegistrationV1],
) -> tuple[str, str] | None:
    occurrences = _provider_occurrences(definition)
    slot_occurrences = _slot_provider_occurrences(definition)
    occurrence_coordinates: list[tuple[str, str]] = []
    for node_id, node in slot_occurrences:
        provider = node.provider
        if not isinstance(provider, ProcedurePinSlotRefV1):  # pragma: no cover - filtered above
            raise AssertionError("slot occurrence lost its slot binding")
        occurrence_coordinates.append((node_id, provider.slot_name))
    closure_coordinates = tuple(
        (item.node_id, item.slot_name) for item in line.provider_implementation_closures
    )
    if tuple(occurrence_coordinates) != closure_coordinates:
        return (
            "playbill.line.provider_implementation_closure_incomplete",
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
            if not isinstance(provider_binding, ProcedurePinSlotRefV1):
                return (
                    "playbill.line.provider_interface_pin_mismatch",
                    f"Provider occurrence {node_id!r} has an unsupported binding.",
                )
            provider_pin = bindings.get(provider_binding.slot_name)
            slot_name = provider_binding.slot_name
            if closure is None or closure.slot_name != slot_name:
                return (
                    "playbill.line.provider_implementation_closure_incomplete",
                    f"Provider occurrence {node_id!r} lacks its exact slot closure.",
                )
            implementation_digest = closure.implementation_digest
        if provider_pin is None:
            return (
                "playbill.line.provider_implementation_unavailable",
                f"Provider occurrence {node_id!r} has no exact Provider pin.",
            )
        accepted_provider = providers.get(provider_pin.artifact_digest)
        if accepted_provider is None or not isinstance(accepted_provider.provider, ProviderV2):
            return (
                "playbill.line.provider_runtime_manifest_required",
                f"Provider occurrence {node_id!r} does not bind an accepted Provider v2.",
            )
        accepted_interface = provider_interfaces.get(interface_pin.artifact_digest)
        if accepted_interface is None:
            return (
                "playbill.line.provider_interface_pin_mismatch",
                f"Provider occurrence {node_id!r} lacks its accepted interface registration.",
            )
        registration = accepted_interface.registration
        if registration.interface_digest != interface_digest:
            return (
                "playbill.line.provider_interface_pin_mismatch",
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
                "playbill.line.provider_implementation_unavailable",
                f"Provider occurrence {node_id!r} implementation is unavailable.",
            )
        if len(matches) != 1:
            return (
                "playbill.line.provider_implementation_ambiguous",
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
                "playbill.line.provider_implementation_ambiguous",
                f"Provider occurrence {node_id!r} manifest row is not singular.",
            )
        manifest = manifest_matches[0]
        expected_environment_map = ProviderExtrasEnvironmentPinMapV1(
            required_extras=tuple(
                sorted(manifest.requires_extras, key=lambda item: item.encode("utf-8"))
            ),
            eligible_environment_pin_keys=tuple(
                reference.environment_pin_key
                for reference in record.materialization_references
                if isinstance(reference, ProviderLocalMaterializationReferenceV1)
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
                    "playbill.line.provider_implementation_pin_mismatch",
                    f"Provider occurrence {node_id!r} closure does not reproduce exact pins.",
                )
    return None


__all__ = [
    "AcceptedLineSpecV1",
    "CadenceTriggerPolicyV1",
    "CaptureLandingTriggerPolicyV1",
    "LineSpecFormatError",
    "LineSpecLawResultV1",
    "LineSpecAny",
    "LineSpecV1",
    "LineSpecV2",
    "ManualTriggerPolicyV1",
    "TriggerPolicyV1",
    "WindowCloseTriggerPolicyV1",
    "evaluate_line_spec_law",
    "line_spec_digest",
    "line_spec_path",
    "parse_line_spec",
    "render_line_spec",
]
