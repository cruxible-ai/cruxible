"""Governed, exact-Procedure authority grants for effectful terminals."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    Sha256Value,
    artifact_bytes_for_path,
    artifact_path_matches,
    normalize_ledger_path,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.models import ProcedureHardCapsV3
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc, format_datetime

if TYPE_CHECKING:
    from cruxible_client.contracts.query.definitions import (
        AcceptedQueryDefinitionV1,
        QueryDefinitionV1,
    )

_MANDATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class ProcedureMandateError(PlaybillFormatError):
    """A ProcedureMandate is malformed or cannot authorize an invocation."""


class _StrictProcedureMandateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _namespace_key(value: str) -> bytes:
    return value.encode("utf-8")


def _validate_namespace(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value or value != tuple(sorted(set(value), key=_namespace_key)):
        raise ValueError("ProcedureMandate namespace must be nonempty, sorted, and unique")
    for member in value:
        if normalize_ledger_path(member) != member:
            raise ValueError("ProcedureMandate namespace must use canonical ledger prefixes")
    return value


PROCEDURE_MANDATE_CLOCK_SKEW: Final = timedelta(minutes=5)
"""Tolerance between a caller's asserted instant and the daemon clock.

A ProcedureMandate's ``valid_from``/``expires_at`` are VALIDITY WINDOW bounds
and the instant tested against them is an EVALUATION INSTANT. A served caller
may assert that instant, so the assertion is admitted only within this bound:
without it a caller could enter a mandate window, or mint a scheduled
occurrence, by claiming a time rather than by waiting for one.
"""


class ProcedureMandateV1(_StrictProcedureMandateModel):
    """One finite grant pinned to one exact accepted Procedure artifact."""

    artifact_format: Literal["playbill-procedure-mandate-v1"] = "playbill-procedure-mandate-v1"
    identity: ArtifactIdentity
    procedure: ArtifactPin
    rung: Literal[2, 3]
    authority_ceiling: ProcedureHardCapsV3
    namespace: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_namespace(value)

    @field_validator("valid_from", "expires_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("valid_from", "expires_at", when_used="json")
    def _serialize_time(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureMandateV1":
        if self.identity.kind != "ProcedureMandate" or not _MANDATE_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("ProcedureMandate identity is not path-addressable")
        if self.procedure.role != "procedure" or self.procedure.target.kind != "Procedure":
            raise ValueError("ProcedureMandate must pin one exact Procedure")
        if self.expires_at <= self.valid_from:
            raise ValueError("ProcedureMandate requires a finite increasing interval")
        return self

    @property
    def pins(self) -> tuple[ArtifactPin, ...]:
        return (self.procedure,)


def procedure_mandate_path(name: str) -> str:
    if not _MANDATE_NAME_RE.fullmatch(name):
        raise ProcedureMandateError("ProcedureMandate identity is not path-addressable")
    return f"procedure-mandates/{name}.json"


def render_procedure_mandate(mandate: "ProcedureMandateV1 | ProcedureMandateV2") -> bytes:
    return pretty_canonical_bytes(mandate.model_dump(mode="json"))


def parse_procedure_mandate(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProcedureMandateV1:
    try:
        mandate = ProcedureMandateV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureMandateError("ProcedureMandate failed strict v1 validation") from exc
    if not artifact_path_matches(procedure_mandate_path(mandate.identity.name), path, codec=codec):
        raise ProcedureMandateError("ProcedureMandate identity/path disagreement")
    if artifact_bytes_for_path(render_procedure_mandate(mandate), path, codec=codec) != content:
        raise ProcedureMandateError("ProcedureMandate is not in canonical wire form")
    return mandate


def procedure_mandate_digest(mandate: "ProcedureMandateV1 | ProcedureMandateV2") -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        mandate.model_dump(mode="json"),
    )


class AcceptedProcedureMandateV1(_StrictProcedureMandateModel):
    path: str
    mandate: "ProcedureMandateV1 | ProcedureMandateV2"
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedProcedureMandateV1":
        if self.path != procedure_mandate_path(self.mandate.identity.name) or (
            self.artifact_digest != procedure_mandate_digest(self.mandate).tagged
        ):
            raise ValueError("accepted ProcedureMandate does not reproduce")
        return self


class ProcedureMandateLawResultV1(_StrictProcedureMandateModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()
    # A successor that only removes authority takes the fast path: it never
    # needs the independent approval that granting or widening authority does.
    narrowing: bool = False


def _law_refusal(code: str, message: str, *, path: str) -> ProcedureMandateLawResultV1:
    return ProcedureMandateLawResultV1(
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


def _ceiling_within(
    ceiling: ProcedureHardCapsV3,
    hard_caps: ProcedureHardCapsV3,
) -> bool:
    return (
        ceiling.max_wall_clock.microseconds <= hard_caps.max_wall_clock.microseconds
        and ceiling.max_provider_calls <= hard_caps.max_provider_calls
        and ceiling.max_capture_bytes <= hard_caps.max_capture_bytes
        and (
            hard_caps.max_result_bytes is None
            or (
                ceiling.max_result_bytes is not None
                and ceiling.max_result_bytes <= hard_caps.max_result_bytes
            )
        )
        and ceiling.max_items <= hard_caps.max_items
        and ceiling.max_repeat_attempts <= hard_caps.max_repeat_attempts
    )


def evaluate_procedure_mandate_law(
    mandate: ProcedureMandateV1,
    *,
    path: str,
    predecessor: AcceptedProcedureMandateV1 | None,
    procedure: AcceptedProcedureV1,
) -> ProcedureMandateLawResultV1:
    if path != procedure_mandate_path(mandate.identity.name):
        return _law_refusal(
            "playbill.procedure_mandate.path_mismatch",
            "ProcedureMandate identity/path disagreement.",
            path=path,
        )
    if mandate.procedure.target != procedure.procedure.identity or (
        mandate.procedure.artifact_digest != procedure.artifact_digest
    ):
        return _law_refusal(
            "playbill.procedure_mandate.procedure_mismatch",
            "ProcedureMandate must pin the exact candidate Procedure artifact.",
            path=path,
        )
    if not _ceiling_within(mandate.authority_ceiling, procedure.procedure.definition.hard_caps):
        return _law_refusal(
            "playbill.procedure_mandate.authority_ceiling_widens_procedure",
            "ProcedureMandate authority_ceiling may narrow but never widen Procedure hard caps.",
            path=path,
        )
    if predecessor is None and mandate.lifecycle.predecessor_digest is not None:
        return _law_refusal(
            "playbill.procedure_mandate.predecessor_missing",
            "A new ProcedureMandate cannot name a predecessor.",
            path=path,
        )
    if predecessor is not None and (
        mandate.identity != predecessor.mandate.identity
        or mandate.lifecycle.predecessor_digest != predecessor.artifact_digest
    ):
        return _law_refusal(
            "playbill.procedure_mandate.predecessor_mismatch",
            "ProcedureMandate successor identity or predecessor differs.",
            path=path,
        )
    return ProcedureMandateLawResultV1(
        verdict="accepted",
        artifact_digest=procedure_mandate_digest(mandate).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


class ScopedClaimTypeV1(_StrictProcedureMandateModel):
    """What the v2 law needs to know about one accepted ClaimType a scope pins."""

    identity: ArtifactIdentity
    artifact_digest: str
    object_kind: Literal["literal", "subject", "exact_content"]
    allowed_subject_kinds: tuple[str, ...]
    allowed_object_subject_kinds: tuple[str, ...] = ()


def evaluate_procedure_mandate_v2_law(
    mandate: ProcedureMandateV2,
    *,
    path: str,
    predecessor: AcceptedProcedureMandateV1 | None,
    procedure: AcceptedProcedureV1,
    claim_types: Mapping[ArtifactIdentity, ScopedClaimTypeV1],
    condition_query: "AcceptedQueryDefinitionV1 | None",
) -> ProcedureMandateLawResultV1:
    """Accept a v2 grant only when every pin is exact and its predicate fails closed."""

    if path != procedure_mandate_path(mandate.identity.name):
        return _law_refusal(
            "playbill.procedure_mandate.path_mismatch",
            "ProcedureMandate identity/path disagreement.",
            path=path,
        )
    if mandate.procedure.target != procedure.procedure.identity or (
        mandate.procedure.artifact_digest != procedure.artifact_digest
    ):
        return _law_refusal(
            "playbill.procedure_mandate.procedure_mismatch",
            "ProcedureMandate must pin the exact candidate Procedure artifact.",
            path=path,
        )
    definition = procedure.procedure.definition
    if not _ceiling_within(mandate.resource_ceiling, definition.hard_caps):
        return _law_refusal(
            "playbill.procedure_mandate.resource_ceiling_widens_procedure",
            "ProcedureMandate resource_ceiling may narrow but never widen Procedure hard caps.",
            path=path,
        )
    needed = 3 if mandate.grants == "settle" else 2
    if definition.terminal_capability < needed:
        return _law_refusal(
            "playbill.procedure_mandate.grant_exceeds_procedure",
            f"A {mandate.grants} grant needs a Procedure whose terminal can {mandate.grants}.",
            path=path,
        )
    if predecessor is None and mandate.lifecycle.predecessor_digest is not None:
        return _law_refusal(
            "playbill.procedure_mandate.predecessor_missing",
            "A new ProcedureMandate cannot name a predecessor.",
            path=path,
        )
    if predecessor is not None and (
        mandate.identity != predecessor.mandate.identity
        or mandate.lifecycle.predecessor_digest != predecessor.artifact_digest
    ):
        return _law_refusal(
            "playbill.procedure_mandate.predecessor_mismatch",
            "ProcedureMandate successor identity or predecessor differs.",
            path=path,
        )
    binding_kinds: set[str] = set()
    for item in mandate.scope:
        claim_type = claim_types.get(item.claim_type.target)
        if claim_type is None or claim_type.artifact_digest != item.claim_type.artifact_digest:
            return _law_refusal(
                "playbill.procedure_mandate.scope_claim_type_unresolved",
                f"Scope ClaimType {item.claim_type.target.qualified} is not accepted at the "
                "pinned digest.",
                path=path,
            )
        if item.binding_subject_role == "object":
            if claim_type.object_kind != "subject":
                return _law_refusal(
                    "playbill.procedure_mandate.binding_role_unavailable",
                    f"ClaimType {claim_type.identity.qualified} has no object Subject to bind.",
                    path=path,
                )
            binding_kinds.update(claim_type.allowed_object_subject_kinds)
        else:
            binding_kinds.update(claim_type.allowed_subject_kinds)
    condition = mandate.condition
    if condition is not None:
        if condition_query is None or (
            condition_query.query.identity != condition.query.target
            or condition_query.artifact_digest != condition.query.artifact_digest
        ):
            return _law_refusal(
                "playbill.procedure_mandate.condition_query_unresolved",
                "The condition query is not accepted at the pinned digest.",
                path=path,
            )
        refusal = condition_query_refusal(condition_query.query, condition)
        if refusal is not None:
            return _law_refusal(refusal[0], refusal[1], path=path)
        entry_kinds = set(getattr(condition_query.query.entry, "subject_kinds", ()))
        if not binding_kinds <= entry_kinds:
            return _law_refusal(
                "playbill.procedure_mandate.condition_subject_kinds_uncovered",
                "The condition query must enter at every Subject kind a scoped change can bind: "
                f"missing {', '.join(sorted(binding_kinds - entry_kinds))}.",
                path=path,
            )
    return ProcedureMandateLawResultV1(
        verdict="accepted",
        artifact_digest=procedure_mandate_digest(mandate).tagged,
        required_tier="governed_write",
        approval_scope=(),
        narrowing=predecessor is not None
        and mandate_change_is_narrowing(mandate, predecessor.mandate),
    )


class ProcedureMandateInvocationV1(_StrictProcedureMandateModel):
    tag: Literal["playbill-procedure-mandate-invocation-v1"] = (
        "playbill-procedure-mandate-invocation-v1"
    )
    procedure_identity: ArtifactIdentity
    procedure_artifact_digest: str
    requested_rung: Literal[2, 3]
    requested_authority: ProcedureHardCapsV3
    target_paths: tuple[str, ...]
    evaluation_time: datetime
    accepted_mandate_digest: str

    @field_validator("procedure_artifact_digest", "accepted_mandate_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("target_paths")
    @classmethod
    def _target_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value), key=_namespace_key)):
            raise ValueError("ProcedureMandate target paths must be nonempty, sorted, and unique")
        for path in value:
            if normalize_ledger_path(path) != path:
                raise ValueError("ProcedureMandate target paths must be canonical ledger paths")
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_evaluation_time(self, value: datetime) -> str | None:
        return format_datetime(value)


class ProcedureMandateEvaluationV1(_StrictProcedureMandateModel):
    tag: Literal["playbill-procedure-mandate-evaluation-v1"] = (
        "playbill-procedure-mandate-evaluation-v1"
    )
    verdict: Literal["permitted", "refused"]
    mandate_digest: str
    refusal_codes: tuple[str, ...] = ()


def procedure_mandate_evaluation_digest(evaluation: ProcedureMandateEvaluationV1) -> str:
    payload = evaluation.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-procedure-mandate-evaluation-v1",
        payload,
    ).tagged


def _path_is_in_namespace(path: str, namespace: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in namespace)


def evaluate_procedure_mandate(
    mandate: ProcedureMandateV1,
    invocation: ProcedureMandateInvocationV1,
) -> ProcedureMandateEvaluationV1:
    """Test EVALUATION INSTANT membership in the mandate VALIDITY WINDOW."""
    refusals: set[str] = set()
    digest = procedure_mandate_digest(mandate).tagged
    if invocation.accepted_mandate_digest != digest or mandate.lifecycle.state != "live":
        refusals.add("procedure_mandate_superseded")
    if not (mandate.valid_from <= invocation.evaluation_time < mandate.expires_at):
        refusals.add("procedure_mandate_expired")
    if (
        invocation.procedure_identity != mandate.procedure.target
        or invocation.procedure_artifact_digest != mandate.procedure.artifact_digest
    ):
        refusals.add("procedure_mandate_procedure_mismatch")
    if invocation.requested_rung > mandate.rung:
        refusals.add("procedure_mandate_rung_insufficient")
    if not _ceiling_within(invocation.requested_authority, mandate.authority_ceiling):
        refusals.add("procedure_mandate_authority_ceiling_insufficient")
    if any(not _path_is_in_namespace(path, mandate.namespace) for path in invocation.target_paths):
        refusals.add("procedure_mandate_namespace_mismatch")
    return ProcedureMandateEvaluationV1(
        verdict="refused" if refusals else "permitted",
        mandate_digest=digest,
        refusal_codes=tuple(sorted(refusals)),
    )


# -- Successor: conditional settle authority (compiler revision 30) ---------------

MandateGrant = Literal["propose", "settle"]
MandateChangeKind = Literal["create", "revise", "retire"]
_GRANT_ORDER: Final = {"propose": 0, "settle": 1}
_PARAMETER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Settle authority may only reach accepted Claims; a mandate can never cover the
# artifacts that govern authority itself (mandates, laws, policy, ClaimTypes).
SETTLE_NAMESPACE_ROOT: Final = "claims"


def mandate_grant(mandate: "ProcedureMandateAny") -> MandateGrant:
    """The verb a mandate grants; historical v1 rungs map 2->propose, 3->settle."""

    if isinstance(mandate, ProcedureMandateV2):
        return mandate.grants
    return "settle" if mandate.rung == 3 else "propose"


class MandateClaimScopeV1(_StrictProcedureMandateModel):
    """One ClaimType a settle grant covers, and the change kinds it may settle."""

    tag: Literal["playbill-mandate-claim-scope-v1"] = "playbill-mandate-claim-scope-v1"
    claim_type: ArtifactPin
    change_kinds: tuple[MandateChangeKind, ...]
    # Which referent of the changed Claim binds the condition query's parameter.
    binding_subject_role: Literal["subject", "object"] = "subject"

    @model_validator(mode="after")
    def _shape(self) -> "MandateClaimScopeV1":
        if self.claim_type.role != "claim-type" or self.claim_type.target.kind != "ClaimType":
            raise ValueError("a mandate Claim scope must pin one exact ClaimType")
        order = ("create", "revise", "retire")
        if not self.change_kinds or self.change_kinds != tuple(
            kind for kind in order if kind in self.change_kinds
        ):
            raise ValueError(
                "mandate change kinds must be nonempty, unique and canonically ordered"
            )
        return self


class MandateConditionV1(_StrictProcedureMandateModel):
    """A pinned accepted query that IS the settlement predicate.

    Core binds ``binding_parameter`` from each target's binding subject; every
    other declared parameter is fixed here, so no caller can steer the check.
    Authority requires exactly one complete, conflict-free row per target with
    every ``required_fields`` value present.
    """

    tag: Literal["playbill-mandate-condition-v1"] = "playbill-mandate-condition-v1"
    query: ArtifactPin
    binding_parameter: str
    fixed_parameters: dict[str, object] = {}
    required_fields: tuple[str, ...]
    fallback: Literal["refuse", "propose"]

    @field_validator("fixed_parameters", mode="before")
    @classmethod
    def _fixed(cls, value: object) -> object:
        from cruxible_client.contracts.canonical import normalize_canonical

        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("mandate fixed_parameters must be an object")
        return normalized

    @model_validator(mode="after")
    def _shape(self) -> "MandateConditionV1":
        if self.query.role != "condition-query" or self.query.target.kind != "QueryDefinition":
            raise ValueError("a mandate condition must pin one exact QueryDefinition")
        if not _PARAMETER_RE.fullmatch(self.binding_parameter):
            raise ValueError("mandate binding_parameter must be a canonical parameter name")
        if self.binding_parameter in self.fixed_parameters:
            raise ValueError("the binding parameter is bound by Core, never fixed by the mandate")
        if not self.required_fields or self.required_fields != tuple(
            sorted(set(self.required_fields))
        ):
            raise ValueError("mandate required_fields must be nonempty, sorted and unique")
        return self


class ProcedureMandateV2(_StrictProcedureMandateModel):
    """A propose or conditional settle grant pinned to one exact Procedure."""

    artifact_format: Literal["playbill-procedure-mandate-v2"] = "playbill-procedure-mandate-v2"
    identity: ArtifactIdentity
    procedure: ArtifactPin
    grants: MandateGrant
    resource_ceiling: ProcedureHardCapsV3
    namespace: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    scope: tuple[MandateClaimScopeV1, ...] = ()
    subject_scope: tuple[SemanticAddress, ...] | None = None
    condition: MandateConditionV1 | None = None
    # The fast-path kill switch: a suspended mandate grants nothing until a
    # governed successor clears it.
    suspended: bool = False
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_namespace(value)

    @field_validator("valid_from", "expires_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("valid_from", "expires_at", when_used="json")
    def _serialize_time(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureMandateV2":
        if self.identity.kind != "ProcedureMandate" or not _MANDATE_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("ProcedureMandate identity is not path-addressable")
        if self.procedure.role != "procedure" or self.procedure.target.kind != "Procedure":
            raise ValueError("ProcedureMandate must pin one exact Procedure")
        if self.expires_at <= self.valid_from:
            raise ValueError("ProcedureMandate requires a finite increasing interval")
        claim_types = [item.claim_type.target for item in self.scope]
        if len(set(claim_types)) != len(claim_types) or claim_types != sorted(
            claim_types, key=lambda item: item.qualified.encode("utf-8")
        ):
            raise ValueError("mandate scope must name each ClaimType once, sorted")
        if self.subject_scope is not None and (
            not self.subject_scope
            or list(self.subject_scope)
            != sorted(set(self.subject_scope), key=lambda item: item.artifact_path)
        ):
            raise ValueError("mandate subject_scope must be None or nonempty, sorted and unique")
        if self.grants == "settle":
            if not self.scope or self.condition is None:
                raise ValueError("a settle grant requires a Claim scope and a condition query")
            if any(
                member != SETTLE_NAMESPACE_ROOT
                and not member.startswith(SETTLE_NAMESPACE_ROOT + "/")
                for member in self.namespace
            ):
                raise ValueError("a settle grant may only reach accepted Claims")
        elif self.scope or self.condition is not None or self.subject_scope is not None:
            raise ValueError("a propose grant carries no settle scope or condition")
        return self

    @property
    def pins(self) -> tuple[ArtifactPin, ...]:
        condition = () if self.condition is None else (self.condition.query,)
        return (self.procedure, *(item.claim_type for item in self.scope), *condition)


ProcedureMandateAny: TypeAlias = ProcedureMandateV1 | ProcedureMandateV2


def _within_window(new: ProcedureMandateAny, old: ProcedureMandateAny) -> bool:
    return new.valid_from >= old.valid_from and new.expires_at <= old.expires_at


def _resources(mandate: ProcedureMandateAny) -> ProcedureHardCapsV3:
    return (
        mandate.resource_ceiling
        if isinstance(mandate, ProcedureMandateV2)
        else mandate.authority_ceiling
    )


def mandate_change_is_narrowing(new: ProcedureMandateAny, old: ProcedureMandateAny) -> bool:
    """Whether a successor only removes authority, so it may take the fast path.

    Narrowing, suspending and retiring never need the independent approval that
    widening does. Anything else -- including a changed condition, which changes
    the predicate itself -- is widening.
    """

    if new.procedure != old.procedure:
        return False
    if new.lifecycle.state == "retired":
        return True
    if _GRANT_ORDER[mandate_grant(new)] > _GRANT_ORDER[mandate_grant(old)]:
        return False
    if not _ceiling_within(_resources(new), _resources(old)):
        return False
    if not set(new.namespace) <= set(old.namespace) or not _within_window(new, old):
        return False
    if (
        isinstance(old, ProcedureMandateV2)
        and old.suspended
        and not (isinstance(new, ProcedureMandateV2) and new.suspended)
    ):
        return False
    if not isinstance(new, ProcedureMandateV2) or new.grants == "propose":
        return True
    if not isinstance(old, ProcedureMandateV2) or old.grants != "settle":
        return False
    if new.condition != old.condition:
        return False
    old_scope = {item.claim_type: item for item in old.scope}
    for item in new.scope:
        previous = old_scope.get(item.claim_type)
        if (
            previous is None
            or previous.binding_subject_role != item.binding_subject_role
            or not set(item.change_kinds) <= set(previous.change_kinds)
        ):
            return False
    if old.subject_scope is not None and (
        new.subject_scope is None or not set(new.subject_scope) <= set(old.subject_scope)
    ):
        return False
    return True


def parse_procedure_mandate_any(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProcedureMandateAny:
    """Parse either mandate generation; the compiler decides which may be accepted."""

    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureMandateError("ProcedureMandate is not JSON") from exc
    if not isinstance(raw, dict) or raw.get("artifact_format") != "playbill-procedure-mandate-v2":
        return parse_procedure_mandate(content, path=path, codec=codec)
    try:
        mandate = ProcedureMandateV2.model_validate(raw)
    except ValueError as exc:
        raise ProcedureMandateError("ProcedureMandate failed strict v2 validation") from exc
    if not artifact_path_matches(procedure_mandate_path(mandate.identity.name), path, codec=codec):
        raise ProcedureMandateError("ProcedureMandate identity/path disagreement")
    if artifact_bytes_for_path(render_procedure_mandate(mandate), path, codec=codec) != content:
        raise ProcedureMandateError("ProcedureMandate is not in canonical wire form")
    return mandate


def condition_query_refusal(
    query: "QueryDefinitionV1", condition: MandateConditionV1
) -> tuple[str, str] | None:
    """Why a query cannot be a settlement predicate, or None when it can.

    A predicate that grants authority must fail closed: every filter form whose
    absent value evaluates true (``not``, negated membership, negated claim
    presence) is refused, and the query must enter by the bound target so each
    row is that target by construction.
    """

    from cruxible_client.contracts.query.grammar import (
        QueryClaimPresenceFilterV1,
        QueryConjunctionFilterV1,
        QueryDisjunctionFilterV1,
        QueryEntryV1,
        QueryMembershipFilterV1,
        QueryNegationFilterV1,
        QueryParameterRefV1,
    )

    def fails_open(filter_: object) -> bool:
        if isinstance(filter_, QueryNegationFilterV1):
            return True
        if isinstance(filter_, QueryMembershipFilterV1 | QueryClaimPresenceFilterV1):
            return filter_.negated
        if isinstance(filter_, QueryConjunctionFilterV1 | QueryDisjunctionFilterV1):
            return any(fails_open(item) for item in filter_.filters)
        return False

    filters = [
        query.where,
        *(step.where for step in query.traversal),
        *(include.where for include in query.includes),
    ]
    if any(item is not None and fails_open(item) for item in filters):
        return (
            "playbill.procedure_mandate.condition_fails_open",
            "A condition query may not use negation: an absent fact would grant authority.",
        )
    entry = query.entry
    if (
        not isinstance(entry, QueryEntryV1)
        or not isinstance(entry.subject_id, QueryParameterRefV1)
        or entry.subject_id.parameter != condition.binding_parameter
        or query.result_binding != entry.binding
    ):
        return (
            "playbill.procedure_mandate.condition_not_target_bound",
            "A condition query must enter at its binding parameter and return that entry.",
        )
    declared = {item.name: item for item in query.parameters}
    fixed = set(condition.fixed_parameters)
    if condition.binding_parameter not in declared or (fixed | {condition.binding_parameter}) != {
        name for name, item in declared.items() if item.required or name in fixed
    }:
        return (
            "playbill.procedure_mandate.condition_parameters_mismatch",
            "Core binds exactly the binding parameter; the mandate fixes every other one.",
        )
    projected = set() if query.projection is None else {f.name for f in query.projection.fields}
    if not set(condition.required_fields) <= projected:
        return (
            "playbill.procedure_mandate.condition_fields_unprojected",
            "Every required field must be a projected field of the condition query.",
        )
    return None


__all__ = [
    "MandateChangeKind",
    "MandateClaimScopeV1",
    "MandateConditionV1",
    "MandateGrant",
    "ProcedureMandateAny",
    "ProcedureMandateV2",
    "SETTLE_NAMESPACE_ROOT",
    "ScopedClaimTypeV1",
    "condition_query_refusal",
    "evaluate_procedure_mandate_v2_law",
    "mandate_change_is_narrowing",
    "mandate_grant",
    "parse_procedure_mandate_any",
    "AcceptedProcedureMandateV1",
    "ProcedureMandateError",
    "ProcedureMandateEvaluationV1",
    "ProcedureMandateInvocationV1",
    "ProcedureMandateLawResultV1",
    "ProcedureMandateV1",
    "evaluate_procedure_mandate",
    "evaluate_procedure_mandate_law",
    "parse_procedure_mandate",
    "procedure_mandate_digest",
    "procedure_mandate_evaluation_digest",
    "procedure_mandate_path",
    "render_procedure_mandate",
]

AcceptedProcedureMandateV1.model_rebuild()
