"""Exact local mirror of the cruxible-provider-runtime v1 wire contract.

Core deliberately does not import ``cruxible_provider_runtime``.  The committed
contract fixture and its guard are the coupling point: provider code produces
the reviewed vectors, while this module speaks and validates those bytes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from typing import Any, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillExecutionError

PROVIDER_RUNTIME_PROTOCOL = "1.0"
PROVIDER_RUNTIME_CONTRACT_COMMIT = "389e9f44de56c1adebae731228cf4628c6fbeca8"
PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS = ("dynamic:target-from-run-input",)
MAX_PROVIDER_SECRET_BUNDLE_BYTES = 65_536

ProviderRuntimeRefusalCodeV1: TypeAlias = Literal[
    "unknown_manifest_field",
    "manifest_divergence",
    "acceptance_divergence",
    "unaccepted_provider",
    "undeclared_interface",
    "ambiguous_implementation",
    "unknown_interface",
    "interface_digest_mismatch",
    "bucket_fixture_missing",
    "invalid_bucket_vocabulary",
    "unsupported_protocol",
    "unknown_run_context_field",
    "provider_protocol_violation",
    "unsupported_backend",
    "lock_mismatch",
    "lock_bytes_mismatch",
    "lock_missing_hash",
    "lock_ambiguous_fork",
    "no_compatible_artifact",
    "unresolvable_source",
    "unknown_extra",
    "index_not_pinned",
    "index_redirect",
    "artifact_hash_mismatch",
    "air_gapped_cache_miss",
    "network_disabled",
    "cache_permissions",
    "cache_integrity",
    "environment_divergence",
    "unclaimed_bucket",
    "unclassified_input",
    "budget_wall_clock",
    "budget_output_size",
    "budget_cost",
    "undeclared_egress",
    "secret_leak",
    "provider_declined",
    "unresolved_secret_ref",
    "secret_bundle_too_large",
    "non_finite_output",
    "insufficient_series_length",
    "non_finite_input",
    "non_finite_result",
    "degenerate_scale",
    "mismatched_lengths",
    "unknown_method",
    "unknown_test_name",
    "declared_family_mismatch",
    "unsupported_aggregation",
    "unknown_column",
    "malformed_model_ref",
    "undeclared_match_parameters",
    "invalid_parameter",
    "cross_origin_credentialed_redirect",
    "unsupported_redirect_scheme",
    "redirect_limit",
    "image_provenance_mismatch",
]

_REFUSAL_CODE_ADAPTER: TypeAdapter[ProviderRuntimeRefusalCodeV1] = TypeAdapter(
    ProviderRuntimeRefusalCodeV1
)


class ProviderRuntimeWireError(PlaybillExecutionError):
    """A runtime context or envelope failed the frozen provider wire contract."""

    def __init__(self, code: ProviderRuntimeRefusalCodeV1, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class _StrictRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _walk_json(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            yield "non_finite", path or "<root>"
        return
    if isinstance(value, Mapping):
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                prefix = f"{path}." if path else ""
                yield "non_string_key", f"{prefix}<key[{index}]> ({type(key).__name__})"
                yield from _walk_json(item, f"{prefix}<value[{index}]>")
            else:
                yield from _walk_json(item, f"{path}.{key}" if path else key)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_json(item, f"{path}[{index}]")
        return
    yield "unsupported", f"{path or '<root>'} ({type(value).__name__})"


def _reject_non_string_keys(value: Any, *, where: str) -> None:
    paths = sorted({path for kind, path in _walk_json(value) if kind == "non_string_key"})
    if paths:
        raise ProviderRuntimeWireError(
            "provider_protocol_violation",
            f"{where} carries mapping keys canonical JSON cannot encode: {paths[:10]}",
        )


def _reject_non_finite(value: Any, *, where: str) -> None:
    findings = sorted(set(_walk_json(value)))
    non_finite = [path for kind, path in findings if kind == "non_finite"]
    if non_finite:
        raise ProviderRuntimeWireError(
            "non_finite_output",
            f"{where} carries non-finite numbers at {non_finite[:10]}",
        )
    unsupported = [path for kind, path in findings if kind in {"non_string_key", "unsupported"}]
    if unsupported:
        raise ProviderRuntimeWireError(
            "provider_protocol_violation",
            f"{where} carries values canonical JSON cannot encode: {unsupported[:10]}",
        )


class ProviderRuntimeProtocolVersionV1(_StrictRuntimeModel):
    major: int
    minor: int

    @classmethod
    def parse(cls, value: str) -> ProviderRuntimeProtocolVersionV1:
        major, separator, minor = value.partition(".")
        if separator != ".":
            raise ProviderRuntimeWireError(
                "unsupported_protocol", f"protocol version {value!r} is not major.minor"
            )
        try:
            return cls(major=int(major), minor=int(minor))
        except ValueError as exc:
            raise ProviderRuntimeWireError(
                "unsupported_protocol", f"protocol version {value!r} is not major.minor"
            ) from exc

    def render(self) -> str:
        return f"{self.major}.{self.minor}"


class ProviderRuntimeBudgetsV1(_StrictRuntimeModel):
    """Child-process budgets whose ``wall_clock_seconds`` reads VALIDITY WINDOW."""

    wall_clock_seconds: float
    output_bytes: int
    cost_units: float | None = None

    @field_validator("wall_clock_seconds")
    @classmethod
    def _positive_time(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("wall_clock_seconds must be positive")
        return value

    @field_validator("output_bytes")
    @classmethod
    def _positive_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("output_bytes must be positive")
        return value


class ProviderRuntimeSecretRefV1(_StrictRuntimeModel):
    ref: str
    purpose: str = ""


class ProviderRuntimeSecretChannelSpecV1(_StrictRuntimeModel):
    kind: Literal["fd"] = "fd"
    fd: int
    refs: tuple[ProviderRuntimeSecretRefV1, ...] = ()

    @field_validator("fd")
    @classmethod
    def _not_standard_stream(cls, value: int) -> int:
        if value <= 2:
            raise ValueError("the secret channel must not reuse a standard stream")
        return value


class ProviderRuntimeRunContextV1(_StrictRuntimeModel):
    protocol_version: str
    run_id: str
    interface_id: str
    interface_digest: str
    implementation_digest: str
    entrypoint: str
    coordinates: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
    input_bucket: str
    capture_contract: str | None = None
    budgets: ProviderRuntimeBudgetsV1
    declared_endpoints: tuple[str, ...] = ()
    secret_channel: ProviderRuntimeSecretChannelSpecV1 | None = None
    additive: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _closed_keys(cls, value: Any) -> Any:
        _reject_non_string_keys(value, where="provider run context")
        return value

    @model_validator(mode="after")
    def _version(self) -> Self:
        ProviderRuntimeProtocolVersionV1.parse(self.protocol_version)
        return self

    def to_json(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    def to_canonical_json(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json"))


class ProviderRuntimeTraceV1(_StrictRuntimeModel):
    endpoints_contacted: list[str] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class ProviderRuntimeRefusalV1(_StrictRuntimeModel):
    code: ProviderRuntimeRefusalCodeV1
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ProviderRuntimeProviderErrorPayloadV1(_StrictRuntimeModel):
    kind: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ProviderRuntimeResultEnvelopeV1(_StrictRuntimeModel):
    protocol_version: str
    run_id: str
    status: Literal["ok", "refused", "error"]
    output: dict[str, Any] | None = None
    refusal: ProviderRuntimeRefusalV1 | None = None
    error: ProviderRuntimeProviderErrorPayloadV1 | None = None
    trace: ProviderRuntimeTraceV1 = Field(default_factory=ProviderRuntimeTraceV1)

    @model_validator(mode="before")
    @classmethod
    def _closed_keys(cls, value: Any) -> Any:
        _reject_non_string_keys(value, where="provider result envelope")
        return value

    @model_validator(mode="after")
    def _status(self) -> Self:
        if self.status == "ok" and self.output is None:
            raise ValueError("an ok result must carry output")
        if self.status == "refused" and self.refusal is None:
            raise ValueError("a refused result must carry a refusal")
        if self.status == "error" and self.error is None:
            raise ValueError("an error result must carry an error")
        if self.status != "ok" and self.output is not None:
            raise ValueError("only an ok result may carry output")
        ProviderRuntimeProtocolVersionV1.parse(self.protocol_version)
        _reject_non_finite(self.output, where="provider output")
        _reject_non_finite(self.trace.metrics, where="provider trace metrics")
        _reject_non_finite(self.trace.events, where="provider trace events")
        if self.refusal is not None:
            _reject_non_finite(self.refusal.detail, where="refusal detail")
        if self.error is not None:
            _reject_non_finite(self.error.detail, where="provider error detail")
        return self

    def to_json(self) -> bytes:
        return self.model_dump_json().encode("utf-8")


def parse_provider_runtime_context(raw: bytes) -> ProviderRuntimeRunContextV1:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderRuntimeWireError(
            "provider_protocol_violation", "run context is not valid UTF-8 JSON"
        ) from exc
    try:
        return ProviderRuntimeRunContextV1.model_validate(document)
    except ProviderRuntimeWireError:
        raise
    except ValidationError as exc:
        extras = [
            ".".join(str(part) for part in item["loc"])
            for item in exc.errors()
            if item["type"] == "extra_forbidden"
        ]
        code: ProviderRuntimeRefusalCodeV1 = (
            "unknown_run_context_field" if extras else "provider_protocol_violation"
        )
        raise ProviderRuntimeWireError(code, "run context failed schema validation") from exc


def parse_provider_runtime_result(raw: bytes) -> ProviderRuntimeResultEnvelopeV1:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderRuntimeWireError(
            "provider_protocol_violation", "provider did not return valid UTF-8 JSON"
        ) from exc
    try:
        return ProviderRuntimeResultEnvelopeV1.model_validate(document)
    except ProviderRuntimeWireError:
        raise
    except ValidationError as exc:
        raise ProviderRuntimeWireError(
            "provider_protocol_violation", "provider returned a malformed result envelope"
        ) from exc


def provider_runtime_refusal_codes() -> tuple[str, ...]:
    """Return the exact closed runtime vocabulary in byte order."""

    schema = _REFUSAL_CODE_ADAPTER.json_schema()
    values = schema.get("enum", ())
    return tuple(sorted((str(value) for value in values), key=lambda value: value.encode("utf-8")))


__all__ = [
    "MAX_PROVIDER_SECRET_BUNDLE_BYTES",
    "PROVIDER_RUNTIME_CONTRACT_COMMIT",
    "PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS",
    "PROVIDER_RUNTIME_PROTOCOL",
    "ProviderRuntimeBudgetsV1",
    "ProviderRuntimeProtocolVersionV1",
    "ProviderRuntimeProviderErrorPayloadV1",
    "ProviderRuntimeRefusalCodeV1",
    "ProviderRuntimeRefusalV1",
    "ProviderRuntimeResultEnvelopeV1",
    "ProviderRuntimeRunContextV1",
    "ProviderRuntimeSecretChannelSpecV1",
    "ProviderRuntimeSecretRefV1",
    "ProviderRuntimeTraceV1",
    "ProviderRuntimeWireError",
    "parse_provider_runtime_context",
    "parse_provider_runtime_result",
    "provider_runtime_refusal_codes",
]
