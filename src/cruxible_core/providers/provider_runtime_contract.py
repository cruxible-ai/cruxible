"""Exact local mirror of the cruxible-provider-runtime protocol 2.0 wire contract.

Core deliberately does not import ``cruxible_provider_runtime``. The vendored
contract fixture (``tests/fixtures/provider_runtime_contract_v2.json``, taken
from cruxible-providers at ``PROVIDER_RUNTIME_CONTRACT_COMMIT``) and its guard
are the coupling point: provider code produces the reviewed vectors, while this
module speaks and validates those bytes.

A run context names one operation of one accepted build. Any other protocol
major refuses ``unsupported_protocol`` before anything else in the document is
read. A refusal code is either one the runtime reserves for its own machinery
(``PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES``) or one the bound operation
declares; the two vocabularies are disjoint, so the spelling decides.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.errors import ExecutionError

PROVIDER_RUNTIME_PROTOCOL = "2.0"
#: The cruxible-providers commit the vendored contract fixture was taken from
#: (``tests/fixtures/provider_runtime_contract_v2.json``).
PROVIDER_RUNTIME_CONTRACT_COMMIT = "1961ce24f056784890e40849ba447e80542e3469"
PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS = (
    "dynamic:target-from-configuration",
    "dynamic:target-from-run-input",
)
MAX_PROVIDER_SECRET_BUNDLE_BYTES = 65_536
_OPERATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: The spelling of every refusal code, the runtime's or an operation's.
PROVIDER_REFUSAL_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: The codes the provider runtime reserves for its own machinery. No operation
#: may declare one, so a refusal's spelling alone says whose it is.
PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "acceptance_divergence",
        "air_gapped_cache_miss",
        "artifact_hash_mismatch",
        "budget_cost",
        "budget_output_size",
        "budget_wall_clock",
        "cache_integrity",
        "cache_permissions",
        "environment_divergence",
        "image_provenance_mismatch",
        "index_not_pinned",
        "index_redirect",
        "lock_ambiguous_fork",
        "lock_bytes_mismatch",
        "lock_mismatch",
        "lock_missing_hash",
        "manifest_divergence",
        "network_disabled",
        "no_compatible_artifact",
        "non_finite_output",
        "provider_protocol_violation",
        "secret_bundle_too_large",
        "secret_leak",
        "unaccepted_provider",
        "undeclared_egress",
        "undeclared_operation",
        "undeclared_refusal",
        "unknown_extra",
        "unknown_manifest_field",
        "unknown_run_context_field",
        "unresolvable_source",
        "unresolved_secret_ref",
        "unsupported_backend",
        "unsupported_protocol",
    }
)

#: The runtime codes the child harness itself produces: the only runtime codes
#: a result envelope may carry. Every other runtime code is an executor's own
#: observation, and an envelope naming one is a protocol violation.
PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "non_finite_output",
        "provider_protocol_violation",
        "undeclared_operation",
        "unknown_run_context_field",
        "unresolved_secret_ref",
        "unsupported_protocol",
    }
)
if not PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES <= PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES:
    raise RuntimeError("the harness refusal codes are runtime codes")  # pragma: no cover


class ProviderRuntimeWireError(ExecutionError):
    """A runtime context or envelope failed the frozen provider wire contract."""

    def __init__(self, code: str, message: str) -> None:
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


class ProviderRuntimeProtocolVersion(_StrictRuntimeModel):
    major: int
    minor: int

    @classmethod
    def parse(cls, value: str) -> ProviderRuntimeProtocolVersion:
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


def require_supported_protocol(value: str, *, where: str) -> ProviderRuntimeProtocolVersion:
    """Parse ``value`` and refuse any major other than the one core speaks."""

    requested = ProviderRuntimeProtocolVersion.parse(value)
    if requested.major != ProviderRuntimeProtocolVersion.parse(PROVIDER_RUNTIME_PROTOCOL).major:
        raise ProviderRuntimeWireError(
            "unsupported_protocol",
            f"{where} is protocol {value}; core speaks {PROVIDER_RUNTIME_PROTOCOL}",
        )
    return requested


class ProviderRuntimeBudgets(_StrictRuntimeModel):
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


class ProviderRuntimeSecretRef(_StrictRuntimeModel):
    ref: str
    purpose: str = ""


class ProviderRuntimeSecretChannelSpec(_StrictRuntimeModel):
    kind: Literal["fd"] = "fd"
    fd: int
    refs: tuple[ProviderRuntimeSecretRef, ...] = ()

    @field_validator("fd")
    @classmethod
    def _not_standard_stream(cls, value: int) -> int:
        if value <= 2:
            raise ValueError("the secret channel must not reuse a standard stream")
        return value


class ProviderRuntimeRunContext(_StrictRuntimeModel):
    """One operation of one accepted build: everything core passes a provider process.

    ``operation`` and ``operation_signature_digest`` are the exact operation and
    ``{in, out, effect}`` signature the Line bound; ``build_digest`` is the
    accepted Provider build; ``entrypoint`` is the object that serves it.
    """

    protocol_version: str
    run_id: str
    operation: str
    operation_signature_digest: str
    build_digest: str
    implementation_digest: str
    entrypoint: str
    coordinates: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
    capture_contract: str | None = None
    budgets: ProviderRuntimeBudgets
    declared_endpoints: tuple[str, ...] = ()
    secret_channel: ProviderRuntimeSecretChannelSpec | None = None
    additive: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _closed_keys(cls, value: Any) -> Any:
        _reject_non_string_keys(value, where="provider run context")
        return value

    @field_validator("operation_signature_digest", "build_digest", "implementation_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError(f"expected a sha256:<hex> digest, got {value!r}")
        return value

    @field_validator("operation")
    @classmethod
    def _operation_name(cls, value: str) -> str:
        if not _OPERATION_NAME_RE.fullmatch(value):
            raise ValueError(f"operation {value!r} is not a canonical operation name")
        return value

    @model_validator(mode="after")
    def _version(self) -> Self:
        require_supported_protocol(self.protocol_version, where="the run context")
        return self

    def to_json(self) -> bytes:
        return self.model_dump_json().encode("utf-8")


class ProviderRuntimeTrace(_StrictRuntimeModel):
    endpoints_contacted: list[str] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class ProviderRuntimeRefusal(_StrictRuntimeModel):
    """A runtime code (reserved) or a code the bound operation declares."""

    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def _canonical_code(cls, value: str) -> str:
        if not PROVIDER_REFUSAL_CODE_RE.fullmatch(value):
            raise ValueError(f"refusal code {value!r} is not a canonical identifier")
        return value

    @property
    def is_runtime(self) -> bool:
        return self.code in PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES


class ProviderRuntimeProviderErrorPayload(_StrictRuntimeModel):
    kind: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ProviderRuntimeResultEnvelope(_StrictRuntimeModel):
    protocol_version: str
    run_id: str
    status: Literal["ok", "refused", "error"]
    output: dict[str, Any] | None = None
    refusal: ProviderRuntimeRefusal | None = None
    error: ProviderRuntimeProviderErrorPayload | None = None
    trace: ProviderRuntimeTrace = Field(default_factory=ProviderRuntimeTrace)

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
        require_supported_protocol(self.protocol_version, where="the result envelope")
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


def _version_first(document: Any, *, where: str) -> None:
    """Refuse another major before the strict schema reads anything else.

    A 1.x document carries fields 2.0 does not know; the version is the fault,
    so it is the refusal.
    """

    if isinstance(document, Mapping):
        version = document.get("protocol_version")
        if isinstance(version, str):
            require_supported_protocol(version, where=where)


def parse_provider_runtime_context(raw: bytes) -> ProviderRuntimeRunContext:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderRuntimeWireError(
            "provider_protocol_violation", "run context is not valid UTF-8 JSON"
        ) from exc
    _version_first(document, where="the run context")
    try:
        return ProviderRuntimeRunContext.model_validate(document)
    except ProviderRuntimeWireError:
        raise
    except ValidationError as exc:
        extras = [
            ".".join(str(part) for part in item["loc"])
            for item in exc.errors()
            if item["type"] == "extra_forbidden"
        ]
        code = "unknown_run_context_field" if extras else "provider_protocol_violation"
        raise ProviderRuntimeWireError(code, "run context failed schema validation") from exc


def parse_provider_runtime_result(raw: bytes) -> ProviderRuntimeResultEnvelope:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderRuntimeWireError(
            "provider_protocol_violation", "provider did not return valid UTF-8 JSON"
        ) from exc
    _version_first(document, where="the result envelope")
    try:
        return ProviderRuntimeResultEnvelope.model_validate(document)
    except ProviderRuntimeWireError:
        raise
    except ValidationError as exc:
        raise ProviderRuntimeWireError(
            "provider_protocol_violation", "provider returned a malformed result envelope"
        ) from exc


def provider_runtime_refusal_codes() -> tuple[str, ...]:
    """The runtime's reserved codes in byte order."""

    return tuple(
        sorted(PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES, key=lambda value: value.encode("utf-8"))
    )


__all__ = [
    "MAX_PROVIDER_SECRET_BUNDLE_BYTES",
    "PROVIDER_RUNTIME_CONTRACT_COMMIT",
    "PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS",
    "PROVIDER_REFUSAL_CODE_RE",
    "PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES",
    "PROVIDER_RUNTIME_PROTOCOL",
    "PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES",
    "ProviderRuntimeBudgets",
    "ProviderRuntimeProtocolVersion",
    "ProviderRuntimeProviderErrorPayload",
    "ProviderRuntimeRefusal",
    "ProviderRuntimeResultEnvelope",
    "ProviderRuntimeRunContext",
    "ProviderRuntimeSecretChannelSpec",
    "ProviderRuntimeSecretRef",
    "ProviderRuntimeTrace",
    "ProviderRuntimeWireError",
    "parse_provider_runtime_context",
    "parse_provider_runtime_result",
    "provider_runtime_refusal_codes",
    "require_supported_protocol",
]
