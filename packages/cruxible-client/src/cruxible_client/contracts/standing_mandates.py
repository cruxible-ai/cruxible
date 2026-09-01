"""Finite, queryable authority ceilings for unattended evidence refresh."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    Sha256Value,
    artifact_bytes_for_path,
    artifact_path_matches,
    canonical_bytes,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress

_MANDATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_DELTA_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class StandingMandateError(PlaybillFormatError):
    """A StandingMandate is malformed, expired, out of scope, or too weak."""


class _StrictMandateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MandateGrantV1(_StrictMandateModel):
    tag: Literal["playbill-mandate-grant-v1"] = "playbill-mandate-grant-v1"
    settlement: Literal["propose_only", "settle_named_deltas"]
    permitted_operations: tuple[
        Literal["compile_capture", "propose_change_set", "activate_change_set"], ...
    ]

    @field_validator("permitted_operations")
    @classmethod
    def _operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value))):
            raise ValueError("mandate operations must be nonempty, sorted, and unique")
        return value

    @model_validator(mode="after")
    def _ceiling(self) -> "MandateGrantV1":
        if self.settlement == "propose_only" and "activate_change_set" in (
            self.permitted_operations
        ):
            raise ValueError("a propose-only mandate cannot activate a change set")
        return self


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes]:
    return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")


class StandingMandate(_StrictMandateModel):
    artifact_format: Literal["playbill-standing-mandate-v1"] = "playbill-standing-mandate-v1"
    identity: ArtifactIdentity
    provider: ArtifactIdentity
    capture_contract_digest: str
    claim_type_scope: tuple[ArtifactIdentity, ...]
    subject_scope: tuple[SemanticAddress, ...] | None
    permitted_delta_classes: tuple[str, ...]
    authority_ceiling: MandateGrantV1
    valid_from: datetime
    valid_until: datetime
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("capture_contract_digest")
    @classmethod
    def _contract_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("claim_type_scope")
    @classmethod
    def _claim_types(cls, value: tuple[ArtifactIdentity, ...]) -> tuple[ArtifactIdentity, ...]:
        keys = tuple(item.qualified for item in value)
        if not value or keys != tuple(sorted(set(keys))):
            raise ValueError("mandate ClaimType scope must be nonempty, sorted, and unique")
        if any(item.kind != "ClaimType" for item in value):
            raise ValueError("mandate ClaimType scope contains a non-ClaimType identity")
        return value

    @field_validator("subject_scope")
    @classmethod
    def _subjects(
        cls, value: tuple[SemanticAddress, ...] | None
    ) -> tuple[SemanticAddress, ...] | None:
        if value is not None:
            encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
            if not value or encoded != tuple(sorted(set(encoded))):
                raise ValueError("finite mandate Subject scope must be sorted and unique")
        return value

    @field_validator("permitted_delta_classes")
    @classmethod
    def _deltas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value))):
            raise ValueError("mandate delta classes must be nonempty, sorted, and unique")
        if any(not _DELTA_RE.fullmatch(item) for item in value):
            raise ValueError("mandate delta class is not canonical")
        return value

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("mandate validity must be timezone-aware")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("mandate pins must be sorted")
        keys = tuple((item.role, item.target.qualified) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("mandate pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "StandingMandate":
        if self.identity.kind != "StandingMandate" or not _MANDATE_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("StandingMandate identity is not path-addressable")
        if self.provider.kind != "Provider":
            raise ValueError("StandingMandate must name a Provider")
        if self.valid_until <= self.valid_from:
            raise ValueError("StandingMandate requires a finite increasing interval")
        required = {
            ("provider", self.provider.qualified),
            *(("claim-type", item.qualified) for item in self.claim_type_scope),
        }
        actual = {(pin.role, pin.target.qualified) for pin in self.pins}
        if not required.issubset(actual):
            raise ValueError("StandingMandate must exactly pin Provider and ClaimTypes")
        if not any(
            pin.role == "capture-contract" and pin.artifact_digest == self.capture_contract_digest
            for pin in self.pins
        ):
            raise ValueError("StandingMandate must pin its exact CaptureContract")
        return self


def standing_mandate_path(name: str) -> str:
    if not _MANDATE_NAME_RE.fullmatch(name):
        raise StandingMandateError("StandingMandate identity is not path-addressable")
    return f"standing-mandates/{name}.json"


def render_standing_mandate(mandate: StandingMandate) -> bytes:
    return pretty_canonical_bytes(mandate.model_dump(mode="json"))


def parse_standing_mandate(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> StandingMandate:
    try:
        mandate = StandingMandate.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StandingMandateError("StandingMandate failed strict v1 validation") from exc
    if not artifact_path_matches(standing_mandate_path(mandate.identity.name), path, codec=codec):
        raise StandingMandateError("StandingMandate identity/path disagreement")
    if artifact_bytes_for_path(render_standing_mandate(mandate), path, codec=codec) != content:
        raise StandingMandateError("StandingMandate is not in canonical wire form")
    return mandate


def standing_mandate_digest(mandate: StandingMandate) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        mandate.model_dump(mode="json"),
    )


class AcceptedStandingMandateV1(_StrictMandateModel):
    path: str
    mandate: StandingMandate
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedStandingMandateV1":
        if self.path != standing_mandate_path(self.mandate.identity.name) or (
            self.artifact_digest != standing_mandate_digest(self.mandate).tagged
        ):
            raise ValueError("accepted StandingMandate does not reproduce")
        return self


class StandingMandateLawResultV1(_StrictMandateModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _law_refusal(code: str, message: str, *, path: str) -> StandingMandateLawResultV1:
    return StandingMandateLawResultV1(
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


def evaluate_standing_mandate_law(
    mandate: StandingMandate,
    *,
    path: str,
    predecessor: AcceptedStandingMandateV1 | None,
) -> StandingMandateLawResultV1:
    if path != standing_mandate_path(mandate.identity.name):
        return _law_refusal(
            "playbill.mandate.path_mismatch",
            "StandingMandate identity/path disagreement.",
            path=path,
        )
    if predecessor is None and mandate.lifecycle.predecessor_digest is not None:
        return _law_refusal(
            "playbill.mandate.predecessor_missing",
            "A new StandingMandate cannot name a predecessor.",
            path=path,
        )
    if predecessor is not None:
        if mandate.identity != predecessor.mandate.identity or (
            mandate.lifecycle.predecessor_digest != predecessor.artifact_digest
        ):
            return _law_refusal(
                "playbill.mandate.predecessor_mismatch",
                "StandingMandate successor identity or predecessor differs.",
                path=path,
            )
    return StandingMandateLawResultV1(
        verdict="accepted",
        artifact_digest=standing_mandate_digest(mandate).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


class MandateInvocationV1(_StrictMandateModel):
    tag: Literal["playbill-mandate-invocation-v1"] = "playbill-mandate-invocation-v1"
    provider: ArtifactIdentity
    capture_contract_digest: str
    claim_type: ArtifactIdentity
    subject: SemanticAddress
    delta_class: str
    operation: Literal["compile_capture", "propose_change_set", "activate_change_set"]
    evaluation_time: datetime
    accepted_authority_digest: str | None = None

    @field_validator("capture_contract_digest", "accepted_authority_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("mandate evaluation time must be timezone-aware")
        return value


class MandateRuntimeCapV1(_StrictMandateModel):
    """A runtime ceiling that can restrict, but never grant, mandate authority."""

    tag: Literal["playbill-mandate-runtime-cap-v1"] = "playbill-mandate-runtime-cap-v1"
    cap_kind: Literal["calibration", "safety", "transport_effect"]
    permitted_operations: (
        tuple[Literal["compile_capture", "propose_change_set", "activate_change_set"], ...] | None
    ) = None
    permitted_delta_classes: tuple[str, ...] | None = None
    valid_until: datetime | None = None
    suspended: bool = False

    @field_validator("permitted_operations")
    @classmethod
    def _operations(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and (not value or value != tuple(sorted(set(value)))):
            raise ValueError("runtime-cap operations must be nonempty, sorted, and unique")
        return value

    @field_validator("permitted_delta_classes")
    @classmethod
    def _deltas(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None:
            if not value or value != tuple(sorted(set(value))):
                raise ValueError("runtime-cap delta classes must be nonempty, sorted, and unique")
            if any(not _DELTA_RE.fullmatch(item) for item in value):
                raise ValueError("runtime-cap delta class is not canonical")
        return value

    @field_validator("valid_until")
    @classmethod
    def _valid_until(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("runtime-cap validity must be timezone-aware")
        return value


class MandateEvaluationV1(_StrictMandateModel):
    tag: Literal["playbill-mandate-evaluation-v1"] = "playbill-mandate-evaluation-v1"
    verdict: Literal["permitted", "refused"]
    mandate_digest: str
    operation: str
    refusal_codes: tuple[str, ...] = ()


def evaluate_standing_mandate(
    mandate: StandingMandate,
    invocation: MandateInvocationV1,
    *,
    runtime_caps: tuple[MandateRuntimeCapV1, ...] = (),
) -> MandateEvaluationV1:
    refusals: set[str] = set()
    if mandate.lifecycle.state != "live":
        refusals.add("playbill.mandate.retired")
    if not (mandate.valid_from <= invocation.evaluation_time < mandate.valid_until):
        refusals.add("playbill.mandate.expired")
    if invocation.provider != mandate.provider:
        refusals.add("playbill.mandate.provider_out_of_scope")
    if invocation.capture_contract_digest != mandate.capture_contract_digest:
        refusals.add("playbill.mandate.capture_contract_out_of_scope")
    if invocation.claim_type not in mandate.claim_type_scope:
        refusals.add("playbill.mandate.claim_type_out_of_scope")
    if mandate.subject_scope is not None and invocation.subject not in mandate.subject_scope:
        refusals.add("playbill.mandate.subject_out_of_scope")
    if invocation.delta_class not in mandate.permitted_delta_classes:
        refusals.add("playbill.mandate.delta_out_of_scope")
    if invocation.operation not in mandate.authority_ceiling.permitted_operations:
        refusals.add("playbill.mandate.operation_out_of_scope")
    if invocation.operation == "activate_change_set":
        if mandate.authority_ceiling.settlement != "settle_named_deltas":
            refusals.add("playbill.mandate.propose_only")
        if invocation.accepted_authority_digest is None:
            refusals.add("playbill.mandate.accepted_authority_missing")
    for cap in runtime_caps:
        prefix = f"playbill.mandate.{cap.cap_kind}"
        if cap.suspended:
            refusals.add(f"{prefix}_suspended")
        if cap.valid_until is not None and invocation.evaluation_time >= cap.valid_until:
            refusals.add(f"{prefix}_expired")
        if (
            cap.permitted_operations is not None
            and invocation.operation not in cap.permitted_operations
        ):
            refusals.add(f"{prefix}_operation_capped")
        if (
            cap.permitted_delta_classes is not None
            and invocation.delta_class not in cap.permitted_delta_classes
        ):
            refusals.add(f"{prefix}_delta_capped")
    return MandateEvaluationV1(
        verdict="refused" if refusals else "permitted",
        mandate_digest=standing_mandate_digest(mandate).tagged,
        operation=invocation.operation,
        refusal_codes=tuple(sorted(refusals)),
    )


class StandingMandateQueryResultV1(_StrictMandateModel):
    tag: Literal["playbill-standing-mandate-query-v1"] = "playbill-standing-mandate-query-v1"
    coordinate: AcceptedCoordinate
    mandate: StandingMandate
    mandate_digest: str

    @model_validator(mode="after")
    def _digest(self) -> "StandingMandateQueryResultV1":
        if self.mandate_digest != standing_mandate_digest(self.mandate).tagged:
            raise ValueError("queried StandingMandate digest does not reproduce")
        return self


__all__ = [
    "AcceptedStandingMandateV1",
    "MandateEvaluationV1",
    "MandateGrantV1",
    "MandateInvocationV1",
    "MandateRuntimeCapV1",
    "StandingMandate",
    "StandingMandateError",
    "StandingMandateLawResultV1",
    "StandingMandateQueryResultV1",
    "evaluate_standing_mandate",
    "evaluate_standing_mandate_law",
    "parse_standing_mandate",
    "render_standing_mandate",
    "standing_mandate_digest",
    "standing_mandate_path",
]
