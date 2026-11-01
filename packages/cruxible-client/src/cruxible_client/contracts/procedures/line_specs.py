"""Governed LineSpec artifact: one stable instantiation of an accepted Procedure."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    normalize_canonical,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.acquisition_rules import (
    LineAcquisitionRule,
    validate_acquisition_keys,
)
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.models import (
    AUTHORITY_RUNG,
    RUNG_AUTHORITY,
    AuthorityVerb,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
)
from cruxible_client.contracts.procedures.trigger_rules import (
    trigger_capture_source,
    validate_trigger_input,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventNameSelector,
)
from cruxible_client.contracts.provider_operations import (
    LineBinding,
    OperationSignature,
    ProviderOperation,
)
from cruxible_client.contracts.providers import AcceptedProvider, ProviderV2
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.shapes import Contract, ContractRef

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
        raise ValueError("LineSpec parameters must be a canonical object")
    return normalized


def _sorted_pins(value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
    if value != tuple(sorted(value, key=_pin_key)):
        raise ValueError("LineSpec pins must be canonically sorted")
    keys = tuple((pin.role, pin.target.qualified) for pin in value)
    if len(set(keys)) != len(keys):
        raise ValueError("LineSpec pins must be unique by role and target")
    return value


LineAuthority = AuthorityVerb

_Count = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
_Positive = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


class LineBudgets(_StrictLineModel):
    """A Line's operating budget: each limit within its Procedure's hard cap.

    Wall clock, provider calls and capture bytes are always stated (authoring
    fills an unstated one with the cap). Items, result bytes and read rows are
    optional: a Line that states none runs under the cap.
    """

    max_wall_clock_microseconds: _Positive
    max_provider_calls: _Count
    max_capture_bytes: _Count
    max_items: _Positive | None = Field(default=None, exclude_if=lambda value: value is None)
    max_result_bytes: _Positive | None = Field(default=None, exclude_if=lambda value: value is None)
    max_read_rows: _Positive | None = Field(default=None, exclude_if=lambda value: value is None)

    def widened(self, caps: ProcedureHardCaps) -> tuple[str, ...]:
        """Each limit that exceeds its cap, as ``name value > cap``."""

        limits: dict[str, int | None] = {
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
            "max_provider_calls": caps.max_provider_calls,
            "max_capture_bytes": caps.max_capture_bytes,
            "max_items": caps.max_items,
            "max_result_bytes": caps.max_result_bytes,
            "max_read_rows": caps.max_read_rows,
        }
        found = []
        for name, cap in limits.items():
            value = getattr(self, name)
            if value is not None and cap is not None and value > cap:
                found.append(f"{name} {value} > {cap}")
        return tuple(found)

    def budget(self, caps: ProcedureHardCaps) -> ProcedureBudget:
        """The run budget this Line states; an unstated optional limit is the cap's."""

        from cruxible_client.contracts.captures import CanonicalDuration
        from cruxible_client.contracts.procedures.reads import effective_read_rows

        return ProcedureBudget(
            wall_clock=CanonicalDuration(microseconds=self.max_wall_clock_microseconds),
            max_provider_calls=self.max_provider_calls,
            max_capture_bytes=self.max_capture_bytes,
            max_items=self.max_items,
            max_result_bytes=self.max_result_bytes
            if self.max_result_bytes is not None
            else caps.max_result_bytes,
            max_read_rows=effective_read_rows(self.max_read_rows, caps.max_read_rows),
        )


class LineBudgetsInput(_StrictLineModel):
    """The budget a Line author states; each unstated required limit defaults to the cap."""

    max_wall_clock_microseconds: _Positive | None = None
    max_provider_calls: _Count | None = None
    max_capture_bytes: _Count | None = None
    max_items: _Positive | None = None
    max_result_bytes: _Positive | None = None
    max_read_rows: _Positive | None = None

    def filled(self, caps: ProcedureHardCaps) -> LineBudgets:
        def stated(value: int | None, cap: int) -> int:
            return cap if value is None else value

        return LineBudgets(
            max_wall_clock_microseconds=stated(
                self.max_wall_clock_microseconds, caps.max_wall_clock.microseconds
            ),
            max_provider_calls=stated(self.max_provider_calls, caps.max_provider_calls),
            max_capture_bytes=stated(self.max_capture_bytes, caps.max_capture_bytes),
            max_items=self.max_items,
            max_result_bytes=self.max_result_bytes,
            max_read_rows=self.max_read_rows,
        )


def caps_line_budgets(caps: ProcedureHardCaps) -> LineBudgets:
    """The budget of a Line that states none: the hard caps themselves."""

    return LineBudgets(
        max_wall_clock_microseconds=caps.max_wall_clock.microseconds,
        max_provider_calls=caps.max_provider_calls,
        max_capture_bytes=caps.max_capture_bytes,
        max_items=caps.max_items,
    )


_BINDING_KEY_RE = re.compile(r"^(?:[a-z][a-z0-9_.-]{0,127}\.)*[a-z][a-z0-9_]{0,63}$")


def _binding_keys(value: dict[str, LineBinding]) -> dict[str, LineBinding]:
    for key in value:
        if not _BINDING_KEY_RE.fullmatch(key):
            raise ValueError(
                f"Line binding key {key!r} is a slot, or <invoke node path>.<slot> for a child"
            )
    return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))


class LineSpec(_StrictLineModel):
    """One instantiation of an exact Procedure version, with every slot bound.

    ``procedure`` pins the exact Procedure version the Line runs. ``bindings``
    holds one exact ``{build, operation}`` per slot, including the slots of
    invoked children (keyed ``<invoke node path>.<slot>``); core records them
    when the Line is accepted, and runs never rebind. ``acquisition`` declares
    the run's acquisition rule per capture output (``<alias>.<field>``); a
    capture output it names no rule for runs under the core default
    (``CORE_DEFAULT_ACQUISITION_RULE``).

    When it runs is not the Line's to say: every Trigger aimed at it is its own
    governed artifact, and Triggers drive it only while the Line is enabled.
    ``trigger_input`` binds the triggering Capture to one named Source input,
    and ``trigger_event`` declares the exact Capture event that input accepts.
    ``occurrence_epoch`` advances exactly when that acceptance changes.
    ``budgets`` is the Line's operating budget; it must fit within the
    Procedure's hard caps (the Procedure declares no budget of its own).

    A Mandate pins the Line's exact digest, so changing the Procedure version,
    the parameters or any binding is a new Line digest and renews the grant.
    """

    artifact_format: Literal["cruxible-line-v8"] = "cruxible-line-v8"
    identity: ArtifactIdentity
    occurrence_epoch: int = Field(ge=1, le=2**63 - 1)
    procedure: ArtifactPin
    parameters: object
    bindings: dict[str, LineBinding] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    acquisition: dict[str, LineAcquisitionRule] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    max_authority: LineAuthority
    trigger_input: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    trigger_event: CaptureEventNameSelector | None = None
    budgets: LineBudgets
    epsilon: object
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    _canonical_objects = field_validator("parameters", mode="before")(_canonical_object)
    _epsilon = field_validator("epsilon", mode="before")(_decimal_wrapper)
    _bindings = field_validator("bindings")(_binding_keys)
    _acquisition = field_validator("acquisition")(validate_acquisition_keys)

    @model_validator(mode="after")
    def _shape(self) -> "LineSpec":
        if self.identity.kind != "Line" or not _LINE_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Line identity must be path-addressable and kind Line")
        if self.procedure.role != "procedure" or self.procedure.target.kind != "Procedure":
            raise ValueError("a Line pins one exact Procedure version with role procedure")
        if (self.trigger_input is None) != (self.trigger_event is None):
            raise ValueError("a trigger input and its accepted trigger_event come together")
        return self

    @property
    def pins(self) -> tuple[ArtifactPin, ...]:
        """The exact versions this Line runs: its Procedure and every bound build."""

        found = {self.procedure, *(binding.build for binding in self.bindings.values())}
        return tuple(sorted(found, key=_pin_key))


class EphemeralLineSpec(_StrictLineModel):
    """What a direct run instantiates, recorded with the run by digest.

    A direct ``procedure run`` has no accepted Line: its exact Procedure
    version, parameters, every slot's binding (named or fitted when the request
    is admitted) and its acquisition overrides form this spec. A direct run never
    egresses (its lane runs no outward terminal), so it can act at most as
    propose would allow and in practice observes.
    """

    tag: Literal["cruxible-ephemeral-line-spec-v2"] = "cruxible-ephemeral-line-spec-v2"
    procedure: ArtifactPin
    parameters: object
    bindings: dict[str, LineBinding] = Field(default_factory=dict)
    acquisition: dict[str, LineAcquisitionRule] = Field(default_factory=dict)
    #: The run's operating budget: a direct run states none, so it runs under
    #: its Procedure's hard caps (``caps_budget``), recorded here by digest.
    budget: ProcedureBudget

    _canonical_parameters = field_validator("parameters", mode="before")(normalize_canonical)
    _bindings = field_validator("bindings")(_binding_keys)
    _acquisition = field_validator("acquisition")(validate_acquisition_keys)


def ephemeral_line_spec_digest(spec: EphemeralLineSpec) -> str:
    return typed_digest(
        ArtifactDigest,
        "cruxible-ephemeral-line-spec-v2",
        spec.model_dump(mode="json", exclude={"tag"}),
    ).tagged


def line_slot_signatures(
    definition: ProcedureDefinition,
    *,
    child: Mapping[str, ProcedureDefinition] | None = None,
    prefix: str = "",
    depth: int = 0,
) -> dict[str, OperationSignature]:
    """Every slot a Line must bind: this Procedure's, then each invoked child's.

    ``child`` maps an invoke node path (``a``, ``a.b``) to the definition it
    invokes. A child's slots are keyed ``<invoke node path>.<slot>``.
    """

    from cruxible_client.contracts.procedures.nodes import InvokeNode

    if depth > 16:
        raise ValueError("invoked Procedures nest too deeply to bind")
    found: dict[str, OperationSignature] = {
        prefix + name: slot for name, slot in definition.slots.items()
    }
    for node in definition.nodes:
        if isinstance(node, InvokeNode):
            path = prefix + node.node_id
            nested = (child or {}).get(path)
            if nested is None:
                raise ValueError(f"invoked Procedure at {path!r} is unavailable for binding")
            found.update(
                line_slot_signatures(nested, child=child, prefix=path + ".", depth=depth + 1)
            )
    return found


def line_capture_outputs(
    definition: ProcedureDefinition,
    *,
    child: Mapping[str, ProcedureDefinition] | None = None,
) -> frozenset[str]:
    """Every capture output a Line may name an acquisition rule for.

    The Procedure's own are ``<alias>.<field>``; an invoked child's are keyed
    ``<invoke node path>.<alias>.<field>`` (``child`` maps each invoke node
    path to the definition it invokes, as for :func:`line_slot_signatures`).
    """

    from cruxible_client.contracts.procedures.artifacts import acquisition_keys

    found = set(acquisition_keys(definition))
    for path, nested in (child or {}).items():
        found.update(f"{path}.{key}" for key in acquisition_keys(nested))
    return frozenset(found)


def line_requested_rung(line: LineSpec) -> Literal[1, 2, 3]:
    """The internal ordering value of what a Line asks to do."""

    return AUTHORITY_RUNG[line.max_authority]


_LINE_SPEC_ADAPTER: TypeAdapter[LineSpec] = TypeAdapter(LineSpec)


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
) -> LineSpec:
    try:
        line = _LINE_SPEC_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LineSpecFormatError("LineSpec failed strict versioned validation") from exc
    if line_spec_path(line.identity.name) != path:
        raise LineSpecFormatError("LineSpec identity/path disagreement")
    if render_line_spec(line) != content:
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
    line: LineSpec
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


def evaluate_line_spec_law(
    line: LineSpec,
    *,
    path: str,
    procedure: AcceptedProcedure,
    predecessor: AcceptedLineSpec | None,
    slots: Mapping[str, OperationSignature] | None = None,
    providers: Mapping[str, AcceptedProvider] | None = None,
    capture_outputs: frozenset[str] | None = None,
    contracts: Callable[[ContractRef], Contract | None] | None = None,
    by_name: bool = True,
) -> LineSpecLawResult:
    """The Line law.

    ``procedure`` is the exact accepted version the Line pins. ``slots`` is
    every slot the Line must bind (:func:`line_slot_signatures`), and
    ``providers`` the accepted builds by qualified identity. ``capture_outputs``
    is every capture output the Line may name a rule for, invoked children's
    included (:func:`line_capture_outputs`); omitted, the Procedure's own.
    ``contracts`` reads governed Contracts at the coordinate: the Line's
    parameters are checked against the Procedure's input Contract with it.
    """

    del by_name
    if path != line_spec_path(line.identity.name):
        return _refusal(
            "cruxible.line.path_mismatch", "Line identity/path disagreement.", path=path
        )
    if (
        line.procedure.target != procedure.procedure.identity
        or line.procedure.artifact_digest != procedure.artifact_digest
    ):
        return _refusal(
            "cruxible.line.procedure_pin_mismatch",
            "The Line does not pin the supplied accepted Procedure version.",
            path=path,
        )
    definition = procedure.procedure.definition
    trigger_problem = validate_trigger_input(line, procedure)
    if trigger_problem is not None:
        code = (
            "cruxible.line.trigger_target_ambiguous"
            if trigger_problem.startswith("trigger_target_ambiguous")
            else "cruxible.line.trigger_input_mismatch"
        )
        return _refusal(code, trigger_problem, path=path)
    from cruxible_client.contracts.procedures.contracts import (
        AcceptedProcedureContractValidator,
        ProcedureContractValidationError,
    )

    if contracts is not None:
        try:
            AcceptedProcedureContractValidator(contracts).validate_contract(
                contract=definition.contract_in,
                payload=normalize_canonical(line.parameters),
                direction="input",
            )
        except ProcedureContractValidationError as exc:
            return _refusal("cruxible.line.parameters_invalid", str(exc), path=path)
    binding_problem = line_binding_problem(
        line.bindings,
        slots=slots if slots is not None else dict(definition.slots),
        providers=providers or {},
    )
    if binding_problem is not None:
        return _refusal(binding_problem[0], binding_problem[1], path=path)
    known = line_capture_outputs(definition) if capture_outputs is None else capture_outputs
    unknown_keys = sorted(set(line.acquisition) - known)
    if unknown_keys:
        return _refusal(
            "cruxible.line.acquisition_unknown_source",
            "Line acquisition names no capture output (<alias>.<field>, or an invoked "
            f"child's <invoke node path>.<alias>.<field>) of its Procedure: {unknown_keys}.",
            path=path,
        )
    if line_requested_rung(line) > definition.terminal_capability:
        return _refusal(
            "cruxible.line.rung_exceeds_procedure_cap",
            "The Line's max_authority exceeds what its Procedure's terminals can do.",
            path=path,
        )
    widened = line.budgets.widened(definition.hard_caps)
    if widened:
        return _refusal(
            "cruxible.line.budget_exceeds_procedure_cap",
            "The Line's operating budget exceeds its Procedure's hard caps: "
            + ", ".join(widened)
            + ".",
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

        def event_key(value: CaptureEventNameSelector | None) -> object:
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


def bound_operation_of(
    binding: LineBinding, providers: Mapping[str, AcceptedProvider]
) -> ProviderOperation | None:
    """The exact operation a binding names, or ``None`` if its build is not accepted live."""

    accepted = providers.get(binding.build.target.qualified)
    if (
        accepted is None
        or accepted.artifact_digest != binding.build.artifact_digest
        or not isinstance(accepted.provider, ProviderV2)
        or accepted.provider.lifecycle.state != "live"
    ):
        return None
    return next(
        (item for item in accepted.provider.operations if item.name == binding.operation), None
    )


def line_binding_problem(
    bindings: Mapping[str, LineBinding],
    *,
    slots: Mapping[str, OperationSignature],
    providers: Mapping[str, AcceptedProvider],
) -> tuple[str, str] | None:
    """Every slot is bound to a live build's operation serving its exact signature."""

    missing = sorted(set(slots) - set(bindings))
    if missing:
        return (
            "cruxible.line.slot_unbound",
            f"Line leaves slots unbound: {missing}; bind each to a build#operation.",
        )
    extra = sorted(set(bindings) - set(slots))
    if extra:
        return (
            "cruxible.procedure.slot_undeclared",
            f"Unknown slots: {extra}; declared slots: {sorted(slots)}.",
        )
    for key, binding in bindings.items():
        operation = bound_operation_of(binding, providers)
        if operation is None:
            return (
                "cruxible.line.binding_unavailable",
                f"Slot {key!r} binds {binding.spelling}, which is not a live accepted "
                "build operation at that exact digest.",
            )
        try:
            fits = operation.signature().same_as(slots[key])
        except ValueError:
            fits = False
        if not fits:
            return (
                "cruxible.line.binding_signature_mismatch",
                f"Slot {key!r} needs {slots[key].spelling()}; {binding.spelling} serves "
                f"{operation.signature().spelling()}.",
            )
    return None


__all__ = [
    "AUTHORITY_RUNG",
    "EphemeralLineSpec",
    "LineBudgets",
    "LineBudgetsInput",
    "caps_line_budgets",
    "bound_operation_of",
    "ephemeral_line_spec_digest",
    "line_binding_problem",
    "line_slot_signatures",
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
