"""§8.5 effective-rung cap, typed terminal egress, and the v1 effect gate.

Terminal authority is never read from one place.  §8.5.1 computes effective
capability as the minimum of five independent terms — the Procedure hard cap,
the LineSpec's requested rung, the propagated sensitivity cap, the live mandate
grant, and the calibration cap — and every one of them may only narrow.  A
missing or expired Procedure mandate contributes nothing: it leaves the
mandate-free ceiling at rung 1 rather than lifting it, so no trigger, landing event, or
calibration reading can ever manufacture settlement authority.  Because the cap
is a minimum, the refusal it produces names the exact limiting term; honest
scarcity is a property of the refusal, not a later reconstruction.

Egress above the cap does not exist, and egress below it is typed by kind:
``emit_capture`` emits inert evidence, ``post_inbox`` posts human attention,
``propose_change_set`` reaches proposal *receive* only under a rung-2 grant, and
``mandate_settlement`` traverses the exact pinned target Claim law under the
resolved mandate.  A receipt cannot relabel one as another: its disposition is
keyed to its kind, so a proposal can never report itself settled.

External effects are not a rung.  V1 registers no effect grant at all, so a
provider call carrying an ``effect_policy`` may prepare a durable intent and
nothing else unless an authenticated actor invocation initiated the run.  A
future grant tag is refused rather than reinterpreted; the refusal path is
reserved here, the artifact is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    CaptureObjectStoreProtocol,
    CaptureRunCoordinateV1,
    CaptureRunCoordinateV2,
    build_cas_capture,
    build_procedure_capture_v2,
    capture_contract_digest,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateInvocationV1,
    ProcedureMandateV1,
    evaluate_procedure_mandate,
)
from cruxible_client.contracts.procedures.models import TERMINAL_REQUIRED_RUNGS, ProcedureHardCapsV3
from cruxible_client.contracts.repairs import (
    HandEditInstructionV1,
    HandEditRepairV1,
    RepairOperationV1,
    ServedRepairV1,
)
from cruxible_client.contracts.standing_mandates import MandateGrantV1, MandateRuntimeCapV1
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.procedures.terminal_dependencies import (
    TAINT_ACCEPTED_STATE,
    TAINT_CONSERVATIVE_DEFAULT,
    TAINT_OMITTED_OPTIONAL,
    TAINT_UNPROMOTED_EXHAUST,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

if TYPE_CHECKING:
    from cruxible_core.playbill.procedures.execution import ProcedureRunAdmissionV1

TerminalEgressKindV1 = Literal[
    "emit_capture",
    "post_inbox",
    "propose_change_set",
    "mandate_settlement",
]

TerminalEgressDispositionV1 = Literal["emitted", "posted", "received", "settled"]

MandateOperationV1 = Literal["compile_capture", "propose_change_set", "activate_change_set"]

EffectiveRungTermV1 = Literal[
    "procedure_terminal_capability",
    "line_requested_rung",
    "propagated_sensitivity",
    "mandate_grant",
    "calibration",
]

#: The five §8.5.1 terms, in the order a tie between equal minima resolves.
EFFECTIVE_RUNG_TERMS: tuple[EffectiveRungTermV1, ...] = (
    "procedure_terminal_capability",
    "line_requested_rung",
    "propagated_sensitivity",
    "mandate_grant",
    "calibration",
)

#: Below rung 0 there is no governed egress at all.  A term reaches this value
#: only by refusing to interpret something, never by grading it.
NO_TERMINAL_EGRESS = -1

#: The highest rung reachable without an exact live Procedure mandate. Capture
#: and human attention remain free; proposals and settlement consume authority.
MANDATE_FREE_RUNG_CEILING = 1

#: The mandate operation each rung consumes.  Rung 1 posts human attention and
#: consumes none, which is why a cap is folded as a monotone prefix.
RUNG_REQUIRED_OPERATIONS: dict[int, MandateOperationV1 | None] = {
    0: "compile_capture",
    1: None,
    2: "propose_change_set",
    3: "activate_change_set",
}

#: Kind-keyed dispositions.  A sink reports the one its kind allows or none.
TERMINAL_EGRESS_DISPOSITIONS: dict[TerminalEgressKindV1, TerminalEgressDispositionV1] = {
    "emit_capture": "emitted",
    "post_inbox": "posted",
    "propose_change_set": "received",
    "mandate_settlement": "settled",
}

#: Terminal kinds whose egress traverses one exact pinned artifact: the
#: CaptureContract an emission is written under, and the target Claim law a
#: settlement must traverse.  The other two kinds pin nothing of their own.
TERMINAL_EGRESS_BOUND_KINDS: frozenset[TerminalEgressKindV1] = frozenset(
    {"emit_capture", "mandate_settlement"}
)

#: How far material carrying one derived taint label may propagate.  Reading
#: accepted state constrains nothing; a reduction over unpromoted exhaust, a
#: conservative default, and an omitted optional input each stop short of
#: unattended settlement.  An unrecognized label is not graded, it is refused.
SENSITIVITY_TAINT_CEILINGS: dict[str, int] = {
    TAINT_ACCEPTED_STATE: 3,
    TAINT_CONSERVATIVE_DEFAULT: 2,
    TAINT_OMITTED_OPTIONAL: 2,
    TAINT_UNPROMOTED_EXHAUST: 2,
}

#: A pseudonymous-selector observation may inform an untrusted proposal or a
#: human, never an unattended settlement of directly addressed accepted state.
SELECTOR_PRIVACY_CEILINGS: dict[str, int] = {
    "direct_allowed": 3,
    "pseudonymous_required": 2,
}

#: V1 registers no external-effect grant.  Any declared grant tag is unknown by
#: construction and is refused rather than reinterpreted as one.
RECOGNIZED_EFFECT_GRANT_TAGS: frozenset[str] = frozenset()


class TerminalEgressError(PlaybillFormatError):
    """A terminal egress request, receipt, or rung binding is not admissible."""


class _StrictEgressModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _tagged(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


# ---------------------------------------------------------------------------
# The five-term effective rung
# ---------------------------------------------------------------------------


class EffectiveRungTermReadingV1(_StrictEgressModel):
    """One independent §8.5.1 ceiling and the exact thing that set it."""

    tag: Literal["playbill-effective-rung-term-v1"] = "playbill-effective-rung-term-v1"
    term: EffectiveRungTermV1
    rung: int = Field(ge=NO_TERMINAL_EGRESS, le=3)
    reason: str
    basis_digest: str | None = None

    @field_validator("basis_digest")
    @classmethod
    def _basis(cls, value: str | None) -> str | None:
        return None if value is None else _tagged(value)


class EffectiveRungV1(_StrictEgressModel):
    """The minimum of five independent terms, bound to the run that computed it."""

    tag: Literal["playbill-effective-rung-v1"] = "playbill-effective-rung-v1"
    procedure_definition_digest: str
    line_spec_digest: str
    sensitivity_policy_digest: str
    mandate_coordinate_digest: str
    calibration_coordinate_digest: str
    mandate_basis_digests: tuple[str, ...] = ()
    terms: tuple[EffectiveRungTermReadingV1, ...]
    effective_rung: int = Field(ge=NO_TERMINAL_EGRESS, le=3)
    limiting_term: EffectiveRungTermV1

    _digests = field_validator(
        "procedure_definition_digest",
        "line_spec_digest",
        "sensitivity_policy_digest",
        "mandate_coordinate_digest",
        "calibration_coordinate_digest",
    )(_tagged)

    @field_validator("mandate_basis_digests")
    @classmethod
    def _basis(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("resolved mandate basis digests must be sorted and unique")
        for item in value:
            _tagged(item)
        return value

    @model_validator(mode="after")
    def _minimum(self) -> "EffectiveRungV1":
        if tuple(item.term for item in self.terms) != EFFECTIVE_RUNG_TERMS:
            raise ValueError("an effective rung binds exactly the five declared terms in order")
        lowest = min(item.rung for item in self.terms)
        if self.effective_rung != lowest:
            raise ValueError("effective rung is not the minimum of its own terms")
        limiting = next(item.term for item in self.terms if item.rung == lowest)
        if self.limiting_term != limiting:
            raise ValueError("limiting term is not the first term reaching the minimum")
        return self

    def term(self, name: EffectiveRungTermV1) -> EffectiveRungTermReadingV1:
        return next(item for item in self.terms if item.term == name)

    def permits(self, kind: TerminalEgressKindV1) -> bool:
        return TERMINAL_REQUIRED_RUNGS[kind] <= self.effective_rung

    def granted_operation(self, kind: TerminalEgressKindV1) -> MandateOperationV1 | None:
        return RUNG_REQUIRED_OPERATIONS[TERMINAL_REQUIRED_RUNGS[kind]]

    @property
    def refusal_code(self) -> str:
        """Return the typed code naming which term capped this run."""

        return f"terminal_rung_capped_by_{self.limiting_term}"


def effective_rung_digest(rung: EffectiveRungV1) -> str:
    payload = rung.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, "playbill-effective-rung-v1", payload).tagged


def _sensitivity_term(
    *,
    selector_privacies: Mapping[str, str],
    taint_labels: tuple[str, ...],
    sensitivity_policy_digest: str,
) -> EffectiveRungTermReadingV1:
    rung = 3
    reason = "No admitted input narrows propagation."
    for input_name in sorted(selector_privacies, key=lambda item: item.encode("utf-8")):
        privacy = selector_privacies[input_name]
        ceiling = SELECTOR_PRIVACY_CEILINGS.get(privacy, NO_TERMINAL_EGRESS)
        if ceiling < rung:
            rung = ceiling
            reason = f"Admitted input {input_name!r} declares selector privacy {privacy!r}."
    for label in sorted(set(taint_labels), key=lambda item: item.encode("utf-8")):
        ceiling = SENSITIVITY_TAINT_CEILINGS.get(label, NO_TERMINAL_EGRESS)
        if ceiling < rung:
            rung = ceiling
            reason = f"Admitted taint label {label!r} narrows propagation."
    return EffectiveRungTermReadingV1(
        term="propagated_sensitivity",
        rung=rung,
        reason=reason,
        basis_digest=sensitivity_policy_digest,
    )


def _mandate_term(
    *,
    mandate_grants: Mapping[str, MandateGrantV1],
    mandate_coordinate_digest: str,
    procedure_mandate_rung: int | None,
) -> EffectiveRungTermReadingV1:
    # StandingMandate is a Provider/CaptureContract/ClaimType grant and cannot
    # be reinterpreted as Procedure authority. P2-C binds the exact
    # ProcedureMandate in the dark v2 request; the executable fold follows B2.
    del mandate_grants
    rung = MANDATE_FREE_RUNG_CEILING if procedure_mandate_rung is None else procedure_mandate_rung
    return EffectiveRungTermReadingV1(
        term="mandate_grant",
        rung=rung,
        reason=(
            "No exact Procedure mandate is bound; rung 2 and rung 3 are unavailable."
            if procedure_mandate_rung is None
            else f"The exact accepted Procedure mandate grants rung {procedure_mandate_rung}."
        ),
        basis_digest=mandate_coordinate_digest,
    )


def _calibration_term(
    *,
    calibration_caps: tuple[MandateRuntimeCapV1, ...],
    calibration_coordinate_digest: str,
    evaluation_time: datetime,
) -> EffectiveRungTermReadingV1:
    rung = 3
    reason = "No calibration cap narrows this run."
    for cap in calibration_caps:
        if cap.cap_kind != "calibration":
            raise TerminalEgressError(
                f"the calibration rung term accepts calibration caps only, not {cap.cap_kind!r}"
            )
        if cap.suspended:
            ceiling, detail = NO_TERMINAL_EGRESS, "is suspended"
        elif cap.valid_until is not None and evaluation_time >= ensure_utc(cap.valid_until):
            ceiling, detail = NO_TERMINAL_EGRESS, "has expired"
        elif cap.permitted_operations is None:
            continue
        else:
            ceiling, detail = 3, "permits every rung"
            for candidate in (0, 1, 2, 3):
                operation = RUNG_REQUIRED_OPERATIONS[candidate]
                if operation is not None and operation not in cap.permitted_operations:
                    ceiling = candidate - 1
                    detail = f"withholds {operation!r}"
                    break
        if ceiling < rung:
            rung = ceiling
            reason = f"The calibration cap {detail}."
    return EffectiveRungTermReadingV1(
        term="calibration",
        rung=rung,
        reason=reason,
        basis_digest=calibration_coordinate_digest,
    )


def compute_effective_rung(
    *,
    procedure_terminal_capability: int,
    requested_terminal_rung: int,
    selector_privacies: Mapping[str, str],
    taint_labels: tuple[str, ...],
    mandate_grants: Mapping[str, MandateGrantV1],
    calibration_caps: tuple[MandateRuntimeCapV1, ...],
    evaluation_time: datetime,
    procedure_definition_digest: str,
    line_spec_digest: str,
    sensitivity_policy_digest: str,
    mandate_coordinate_digest: str,
    calibration_coordinate_digest: str,
    procedure_mandate_rung: int | None = None,
) -> EffectiveRungV1:
    """Fold the five §8.5.1 terms; each may narrow the result and none may widen it.

    ``mandate_grants`` carries only grants that already resolved live through
    ``resolve_authority_basis``.  Nothing else may reach this computation: an
    expired, superseded, or absent mandate simply is not in the mapping, and its
    absence leaves :data:`MANDATE_FREE_RUNG_CEILING` untouched.
    """

    terms = (
        EffectiveRungTermReadingV1(
            term="procedure_terminal_capability",
            rung=procedure_terminal_capability,
            reason=(
                f"The accepted Procedure declares terminal capability "
                f"{procedure_terminal_capability}."
            ),
            basis_digest=procedure_definition_digest,
        ),
        EffectiveRungTermReadingV1(
            term="line_requested_rung",
            rung=requested_terminal_rung,
            reason=f"The accepted LineSpec requests terminal rung {requested_terminal_rung}.",
            basis_digest=line_spec_digest,
        ),
        _sensitivity_term(
            selector_privacies=selector_privacies,
            taint_labels=taint_labels,
            sensitivity_policy_digest=sensitivity_policy_digest,
        ),
        _mandate_term(
            mandate_grants=mandate_grants,
            mandate_coordinate_digest=mandate_coordinate_digest,
            procedure_mandate_rung=procedure_mandate_rung,
        ),
        _calibration_term(
            calibration_caps=calibration_caps,
            calibration_coordinate_digest=calibration_coordinate_digest,
            evaluation_time=ensure_utc(evaluation_time),
        ),
    )
    lowest = min(item.rung for item in terms)
    return EffectiveRungV1(
        procedure_definition_digest=procedure_definition_digest,
        line_spec_digest=line_spec_digest,
        sensitivity_policy_digest=sensitivity_policy_digest,
        mandate_coordinate_digest=mandate_coordinate_digest,
        calibration_coordinate_digest=calibration_coordinate_digest,
        mandate_basis_digests=tuple(sorted(mandate_grants, key=lambda item: item.encode("ascii"))),
        terms=terms,
        effective_rung=lowest,
        limiting_term=next(item.term for item in terms if item.rung == lowest),
    )


# ---------------------------------------------------------------------------
# Typed egress
# ---------------------------------------------------------------------------


class TerminalEgressItemV1(_StrictEgressModel):
    """One fanout child, addressed by the manifest its own closure produced."""

    tag: Literal["playbill-terminal-egress-item-v1"] = "playbill-terminal-egress-item-v1"
    child_index: int = Field(ge=0)
    item_key: str
    manifest_digest: str
    value: object

    _manifest = field_validator("manifest_digest")(_tagged)

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)


class TerminalEgressRequestV1(_StrictEgressModel):
    """Exactly what one authorized terminal hands its sink, and nothing more."""

    tag: Literal["playbill-terminal-egress-request-v1"] = "playbill-terminal-egress-request-v1"
    kind: TerminalEgressKindV1
    run_id: str
    node_id: str
    accepted_coordinate: AcceptedCoordinate
    procedure_identity: ArtifactIdentity
    procedure_artifact_digest: str
    admission_binding_digest: str
    effective_rung: int = Field(ge=0, le=3)
    required_rung: int = Field(ge=0, le=3)
    limiting_term: EffectiveRungTermV1
    granted_operation: MandateOperationV1 | None = None
    bound_artifact_pin: ArtifactPin | None = None
    mandate_pin: ArtifactPin | None = None
    mandate_basis_digests: tuple[str, ...] = ()
    actor_context: GovernedActorContext
    items: tuple[TerminalEgressItemV1, ...]
    prepared_at: datetime = Field(description="Reads EVALUATION INSTANT.")

    _digests = field_validator("procedure_artifact_digest", "admission_binding_digest")(_tagged)

    @field_validator("mandate_basis_digests")
    @classmethod
    def _basis(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("mandate basis digests must be sorted and unique")
        for item in value:
            _tagged(item)
        return value

    @field_validator("prepared_at")
    @classmethod
    def _prepared_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _shape(self) -> "TerminalEgressRequestV1":
        if self.required_rung != TERMINAL_REQUIRED_RUNGS[self.kind]:
            raise ValueError("terminal egress required rung disagrees with its kind")
        if self.required_rung > self.effective_rung:
            raise ValueError("terminal egress above the effective rung is never requested")
        if self.granted_operation != RUNG_REQUIRED_OPERATIONS[self.required_rung]:
            raise ValueError("terminal egress operation disagrees with its rung")
        if (self.bound_artifact_pin is not None) != (self.kind in TERMINAL_EGRESS_BOUND_KINDS):
            raise ValueError(f"{self.kind} egress binds exactly the artifact its law traverses")
        settlement = self.kind == "mandate_settlement"
        if settlement != (self.mandate_pin is not None):
            raise ValueError("only mandate settlement pins a mandate")
        if settlement != bool(self.mandate_basis_digests):
            raise ValueError("only mandate settlement carries resolved mandate basis")
        if not self.items:
            raise ValueError("terminal egress requires at least one child item")
        if tuple(item.child_index for item in self.items) != tuple(range(len(self.items))):
            raise ValueError("terminal egress children must be declared fanout order")
        return self


class TerminalEgressChildReceiptV1(_StrictEgressModel):
    """One delivered child and the exact handle its sink produced for it."""

    tag: Literal["playbill-terminal-egress-child-receipt-v1"] = (
        "playbill-terminal-egress-child-receipt-v1"
    )
    child_index: int = Field(ge=0)
    item_key: str
    egress_digest: str

    _egress = field_validator("egress_digest")(_tagged)


class TerminalEgressReceiptV1(_StrictEgressModel):
    """What a sink may report.  Its disposition is keyed to its kind, so a
    proposal can never report itself activated or settled."""

    tag: Literal["playbill-terminal-egress-receipt-v1"] = "playbill-terminal-egress-receipt-v1"
    kind: TerminalEgressKindV1
    run_id: str
    node_id: str
    disposition: TerminalEgressDispositionV1
    bound_artifact_digest: str | None = None
    children: tuple[TerminalEgressChildReceiptV1, ...]

    @field_validator("bound_artifact_digest")
    @classmethod
    def _bound(cls, value: str | None) -> str | None:
        return None if value is None else _tagged(value)

    @model_validator(mode="after")
    def _shape(self) -> "TerminalEgressReceiptV1":
        expected = TERMINAL_EGRESS_DISPOSITIONS[self.kind]
        if self.disposition != expected:
            raise ValueError(f"a {self.kind} egress reports {expected!r}, nothing else")
        if (self.bound_artifact_digest is not None) != (self.kind in TERMINAL_EGRESS_BOUND_KINDS):
            raise ValueError(f"{self.kind} egress reports exactly the artifact it traversed")
        if not self.children:
            raise ValueError("terminal egress reports at least one delivered child")
        if tuple(item.child_index for item in self.children) != tuple(range(len(self.children))):
            raise ValueError("terminal egress children must be reported in fanout order")
        return self


class ProcedureProducerReceiptV1(_StrictEgressModel):
    """Pre-egress producer commitment that keeps the Capture graph acyclic."""

    tag: Literal["playbill-procedure-producer-receipt-v1"] = (
        "playbill-procedure-producer-receipt-v1"
    )
    admission_binding_digest: str
    run_id: str
    accepted_coordinate: AcceptedCoordinate
    procedure_identity: ArtifactIdentity
    procedure_artifact_digest: str
    terminal_node_id: str
    item_manifest_digests: tuple[str, ...]
    capture_contract_digest: str

    @field_validator(
        "admission_binding_digest",
        "procedure_artifact_digest",
        "capture_contract_digest",
    )
    @classmethod
    def _receipt_digests(cls, value: str) -> str:
        return _tagged(value)

    @field_validator("item_manifest_digests")
    @classmethod
    def _item_manifests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("producer receipt requires at least one item manifest")
        for digest in value:
            _tagged(digest)
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureProducerReceiptV1":
        if self.procedure_identity.kind != "Procedure":
            raise ValueError("producer receipt must name a Procedure")
        return self


def procedure_producer_receipt_digest(receipt: ProcedureProducerReceiptV1) -> str:
    payload = receipt.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-procedure-producer-receipt-v1",
        payload,
    ).tagged


def terminal_operation_key(request: TerminalEgressRequestV1) -> str:
    """Derive one retry key from semantic run inputs, never delivery time."""

    return typed_digest(
        Sha256Value,
        "playbill-terminal-operation-key-v1",
        {
            "admission_binding_digest": request.admission_binding_digest,
            "kind": request.kind,
            "node_id": request.node_id,
            "item_manifest_digests": [item.manifest_digest for item in request.items],
            "target_paths": list(getattr(request, "target_paths", ())),
            "procedure_mandate_digest": getattr(request, "procedure_mandate_digest", None),
            "procedure_artifact_digest": request.procedure_artifact_digest,
        },
    ).tagged


class TerminalEgressRequestV2(TerminalEgressRequestV1):
    """Dark P2-C request; B2 will parent it from the final admission carrier."""

    tag: Literal["playbill-terminal-egress-request-v2"] = "playbill-terminal-egress-request-v2"  # type: ignore[assignment]
    procedure_mandate_digest: str | None = None
    calibration_reading_digests: tuple[str, ...] = ()
    requested_authority: ProcedureHardCapsV3
    target_paths: tuple[str, ...] = ()
    evaluation_time: datetime = Field(description="Reads EVALUATION INSTANT.")
    operation_key: str | None = None
    producer_receipt: ProcedureProducerReceiptV1 | None = None

    @field_validator("procedure_mandate_digest", "operation_key")
    @classmethod
    def _optional_digests(cls, value: str | None) -> str | None:
        return None if value is None else _tagged(value)

    @field_validator("calibration_reading_digests")
    @classmethod
    def _readings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("calibration reading digests must be sorted and unique")
        for digest in value:
            _tagged(digest)
        return value

    @field_validator("target_paths")
    @classmethod
    def _targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("terminal target paths must be sorted and unique")
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _shape(self) -> "TerminalEgressRequestV2":
        if self.required_rung != TERMINAL_REQUIRED_RUNGS[self.kind]:
            raise ValueError("terminal egress required rung disagrees with its kind")
        if self.required_rung > self.effective_rung:
            raise ValueError("terminal egress above the effective rung is never requested")
        if self.granted_operation != RUNG_REQUIRED_OPERATIONS[self.required_rung]:
            raise ValueError("terminal egress operation disagrees with its rung")
        if (self.bound_artifact_pin is not None) != (self.kind in TERMINAL_EGRESS_BOUND_KINDS):
            raise ValueError(f"{self.kind} egress binds exactly the artifact its law traverses")
        effectful = self.kind in {"propose_change_set", "mandate_settlement"}
        if bool(self.target_paths) != effectful or (self.operation_key is not None) != effectful:
            raise ValueError("effectful terminal egress requires exact targets and operation key")
        if (self.producer_receipt is not None) != (self.kind == "emit_capture"):
            raise ValueError("only Capture egress carries a producer receipt")
        if self.producer_receipt is not None:
            producer = self.producer_receipt
            pin = self.bound_artifact_pin
            if (
                producer.admission_binding_digest != self.admission_binding_digest
                or producer.run_id != self.run_id
                or producer.accepted_coordinate != self.accepted_coordinate
                or producer.procedure_identity != self.procedure_identity
                or producer.procedure_artifact_digest != self.procedure_artifact_digest
                or producer.terminal_node_id != self.node_id
                or producer.item_manifest_digests
                != tuple(item.manifest_digest for item in self.items)
                or pin is None
                or producer.capture_contract_digest != pin.artifact_digest
            ):
                raise ValueError("Capture producer receipt does not reproduce its egress request")
        if (self.procedure_mandate_digest is not None) != effectful:
            raise ValueError("effectful terminal egress requires an exact Procedure mandate")
        if self.mandate_pin is not None or self.mandate_basis_digests:
            raise ValueError("v2 terminal egress refuses inherited StandingMandate authority")
        expected_key = terminal_operation_key(self) if effectful else None
        if self.operation_key != expected_key:
            raise ValueError("terminal operation key does not reproduce from the request")
        if not self.items:
            raise ValueError("terminal egress requires at least one child item")
        if tuple(item.child_index for item in self.items) != tuple(range(len(self.items))):
            raise ValueError("terminal egress children must be declared fanout order")
        return self


class TerminalAuthorityRefusal(TerminalEgressError):
    """Repair-carrying refusal for the dark Procedure authority boundary."""

    def __init__(
        self,
        codes: str | tuple[str, ...],
        message: str,
        *,
        request: TerminalEgressRequestV1,
        repair_kind: Literal[
            "create_mandate",
            "author_successor",
            "rebind_admission",
            "use_declared_rung",
        ],
        repair: ServedRepairV1,
    ) -> None:
        normalized = (codes,) if isinstance(codes, str) else tuple(codes)
        if not normalized:
            raise ValueError("terminal authority refusal requires at least one code")
        self.codes = normalized
        self.code = normalized[0]
        self.procedure_name = request.procedure_identity.name
        self.required_rung = request.required_rung
        self.target_namespace = tuple(getattr(request, "target_paths", ()))
        self.repair_kind = repair_kind
        self.repair = repair
        super().__init__(
            f"{', '.join(normalized)}: {message} "
            f"Procedure={self.procedure_name!r}; required_rung={self.required_rung}; "
            f"target_namespace={list(self.target_namespace)!r}."
        )


PROCEDURE_MANDATE_REPAIR = RepairOperationV1(
    operation="playbill.authoring.create",
    arguments={"example": "procedure-mandate"},
)
PROCEDURE_ADMISSION_REPAIR = HandEditRepairV1(
    hand_edit=HandEditInstructionV1(
        target="terminal-egress/admission",
        required_change="rebuild_from_exact_admitted_run",
    )
)


def _validated_run_admission(
    request: TerminalEgressRequestV1,
    admission: ProcedureRunAdmissionV1,
) -> ProcedureRunAdmissionV1:
    """Order two EVALUATION INSTANT values while binding admitted authority."""

    from cruxible_core.playbill.procedures.execution import (
        ProcedureRunAdmissionV1,
        procedure_admission_digest,
    )

    try:
        if not isinstance(admission, ProcedureRunAdmissionV1):
            raise TypeError("not a Procedure admission")
        validated = type(admission).model_validate(admission.model_dump(mode="python"))
    except (TypeError, ValueError) as exc:
        raise TerminalAuthorityRefusal(
            "procedure_authority_admission_invalid",
            "The authority source is not a valid admitted Procedure run.",
            request=request,
            repair_kind="rebind_admission",
            repair=PROCEDURE_ADMISSION_REPAIR,
        ) from exc
    common_matches = (
        validated.admission_binding_digest == procedure_admission_digest(validated)
        and request.admission_binding_digest == validated.admission_binding_digest
        and request.run_id == validated.run_id
        and request.accepted_coordinate == validated.accepted_coordinate
        and request.procedure_identity == validated.procedure_identity
        and request.procedure_artifact_digest == validated.procedure_artifact_digest
        and request.actor_context == validated.actor_context
        and request.prepared_at >= validated.admitted_at
    )
    v2_matches = not isinstance(request, TerminalEgressRequestV2) or (
        request.requested_authority == validated.hard_caps
        and request.evaluation_time == request.prepared_at
    )
    if not common_matches or not v2_matches:
        raise TerminalAuthorityRefusal(
            "procedure_authority_admission_mismatch",
            "The terminal request does not reproduce its admitted authority and monotone time.",
            request=request,
            repair_kind="rebind_admission",
            repair=PROCEDURE_ADMISSION_REPAIR,
        )
    return validated


def build_terminal_egress_request_v2(
    request: TerminalEgressRequestV1,
    *,
    admission: ProcedureRunAdmissionV1,
    procedure_mandate_digest: str | None,
    calibration_reading_digests: tuple[str, ...],
    target_paths: tuple[str, ...],
) -> TerminalEgressRequestV2:
    """Successor a prepared request from its exact admission, never caller authority."""

    validated_admission = _validated_run_admission(request, admission)
    payload = {
        field_name: getattr(request, field_name)
        for field_name in TerminalEgressRequestV1.model_fields
        if field_name != "tag"
    }
    payload["mandate_pin"] = None
    payload["mandate_basis_digests"] = ()
    provisional = TerminalEgressRequestV2.model_construct(
        **payload,
        procedure_mandate_digest=procedure_mandate_digest,
        calibration_reading_digests=calibration_reading_digests,
        requested_authority=validated_admission.hard_caps,
        target_paths=target_paths,
        evaluation_time=request.prepared_at,
        operation_key=None,
        producer_receipt=(
            producer_receipt_for_request(request) if request.kind == "emit_capture" else None
        ),
    )
    operation_key = (
        terminal_operation_key(provisional)
        if request.kind in {"propose_change_set", "mandate_settlement"}
        else None
    )
    return TerminalEgressRequestV2.model_validate(
        provisional.model_copy(update={"operation_key": operation_key}).model_dump(mode="python")
    )


class TerminalEgressReceiptV2(TerminalEgressReceiptV1):
    tag: Literal["playbill-terminal-egress-receipt-v2"] = "playbill-terminal-egress-receipt-v2"  # type: ignore[assignment]
    producer_receipt_digest: str | None = None
    operation_key: str | None = None

    @field_validator("producer_receipt_digest", "operation_key")
    @classmethod
    def _v2_digests(cls, value: str | None) -> str | None:
        return None if value is None else _tagged(value)

    @model_validator(mode="after")
    def _shape(self) -> "TerminalEgressReceiptV2":
        expected = TERMINAL_EGRESS_DISPOSITIONS[self.kind]
        if self.disposition != expected:
            raise ValueError(f"a {self.kind} egress reports {expected!r}, nothing else")
        if (self.bound_artifact_digest is not None) != (self.kind in TERMINAL_EGRESS_BOUND_KINDS):
            raise ValueError(f"{self.kind} egress reports exactly the artifact it traversed")
        if (self.producer_receipt_digest is not None) != (self.kind == "emit_capture"):
            raise ValueError("only Capture egress reports a producer receipt")
        if (self.operation_key is not None) != (
            self.kind in {"propose_change_set", "mandate_settlement"}
        ):
            raise ValueError("only effectful egress reports an operation key")
        if not self.children:
            raise ValueError("terminal egress reports at least one delivered child")
        if tuple(item.child_index for item in self.children) != tuple(range(len(self.children))):
            raise ValueError("terminal egress children must be reported in fanout order")
        return self


def require_procedure_mandate(
    request: TerminalEgressRequestV2,
    *,
    admission: ProcedureRunAdmissionV1,
    accepted_mandates: Mapping[str, ProcedureMandateV1],
) -> ProcedureMandateV1:
    """Resolve and evaluate authority before any effectful adapter is invoked."""

    _validated_run_admission(request, admission)
    if request.kind not in {"propose_change_set", "mandate_settlement"}:
        raise TerminalAuthorityRefusal(
            "procedure_mandate_not_applicable",
            "Only rung-2 and rung-3 terminals consume Procedure mandates.",
            request=request,
            repair_kind="use_declared_rung",
            repair=HandEditRepairV1(
                hand_edit=HandEditInstructionV1(
                    target="terminal-egress/required-rung",
                    required_change="use_declared_terminal_rung",
                )
            ),
        )
    digest = request.procedure_mandate_digest
    mandate = None if digest is None else accepted_mandates.get(digest)
    if mandate is None:
        raise TerminalAuthorityRefusal(
            "procedure_mandate_required",
            "An exact accepted Procedure mandate is required before effect.",
            request=request,
            repair_kind="create_mandate",
            repair=PROCEDURE_MANDATE_REPAIR,
        )
    assert digest is not None
    evaluation = evaluate_procedure_mandate(
        mandate,
        ProcedureMandateInvocationV1(
            procedure_identity=request.procedure_identity,
            procedure_artifact_digest=request.procedure_artifact_digest,
            requested_rung=request.required_rung,  # type: ignore[arg-type]
            requested_authority=request.requested_authority,
            target_paths=request.target_paths,
            evaluation_time=request.prepared_at,
            accepted_mandate_digest=digest,
        ),
    )
    if evaluation.verdict == "refused":
        raise TerminalAuthorityRefusal(
            evaluation.refusal_codes,
            "The accepted Procedure mandate does not cover this terminal request.",
            request=request,
            repair_kind="author_successor",
            repair=PROCEDURE_MANDATE_REPAIR,
        )
    return mandate


def producer_receipt_for_request(
    request: TerminalEgressRequestV1,
) -> ProcedureProducerReceiptV1:
    if request.kind != "emit_capture" or request.bound_artifact_pin is None:
        raise TerminalEgressError("producer receipts are defined only for Capture egress")
    return ProcedureProducerReceiptV1(
        admission_binding_digest=request.admission_binding_digest,
        run_id=request.run_id,
        accepted_coordinate=request.accepted_coordinate,
        procedure_identity=request.procedure_identity,
        procedure_artifact_digest=request.procedure_artifact_digest,
        terminal_node_id=request.node_id,
        item_manifest_digests=tuple(item.manifest_digest for item in request.items),
        capture_contract_digest=request.bound_artifact_pin.artifact_digest,
    )


@runtime_checkable
class TerminalEgressSinkProtocol(Protocol):
    def deliver_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
    ) -> TerminalEgressReceiptV1: ...


def verify_terminal_egress_receipt(
    request: TerminalEgressRequestV1,
    receipt: TerminalEgressReceiptV1,
) -> None:
    """Refuse any receipt that renames, drops, adds, or re-targets a child."""

    if receipt.kind != request.kind or receipt.run_id != request.run_id:
        raise TerminalEgressError("terminal egress receipt names another run or kind")
    if receipt.node_id != request.node_id:
        raise TerminalEgressError("terminal egress receipt names another terminal node")
    expected_bound = (
        None if request.bound_artifact_pin is None else request.bound_artifact_pin.artifact_digest
    )
    if receipt.bound_artifact_digest != expected_bound:
        raise TerminalEgressError("terminal egress did not traverse the exact pinned artifact")
    if len(receipt.children) != len(request.items):
        raise TerminalEgressError("terminal egress receipt drops or invents a child")
    for item, child in zip(request.items, receipt.children, strict=True):
        if child.item_key != item.item_key:
            raise TerminalEgressError("terminal egress receipt renames a child item")
    if isinstance(receipt, TerminalEgressReceiptV2):
        if not isinstance(request, TerminalEgressRequestV2):
            raise TerminalEgressError("a v2 terminal receipt requires its exact v2 request")
        expected_producer = (
            None
            if request.producer_receipt is None
            else procedure_producer_receipt_digest(request.producer_receipt)
        )
        if receipt.producer_receipt_digest != expected_producer:
            raise TerminalEgressError("Capture egress did not bind its exact producer receipt")
        if receipt.operation_key != request.operation_key:
            raise TerminalEgressError("effectful egress did not reproduce its operation key")
    elif isinstance(request, TerminalEgressRequestV2):
        raise TerminalEgressError("v2 terminal egress requires a v2 receipt")


class CaptureTerminalEgressSink:
    """Rung-0 egress: inert Capture emission through the accepted Capture machinery.

    This sink deliberately serves ``emit_capture`` alone.  Inbox, proposal, and
    settlement egress traverse governed surfaces the run plane does not own, and
    a sink that quietly served them here would be a second authority.
    """

    def __init__(
        self,
        *,
        store: CaptureObjectStoreProtocol,
        contracts: Mapping[str, CaptureContractV1],
        producer: ArtifactIdentity,
        producer_binding_digest: str,
    ) -> None:
        self.store = store
        self.contracts = dict(contracts)
        self.producer = producer
        self.producer_binding_digest = producer_binding_digest

    def deliver_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
    ) -> TerminalEgressReceiptV1:
        if request.kind != "emit_capture":
            raise TerminalEgressError(
                f"the Capture egress sink serves emit_capture only, not {request.kind!r}"
            )
        pin = request.bound_artifact_pin
        if pin is None:  # pragma: no cover - request law binds the contract
            raise TerminalEgressError("emit_capture egress requires its pinned CaptureContract")
        contract = self.contracts.get(pin.artifact_digest)
        if contract is None or capture_contract_digest(contract).tagged != pin.artifact_digest:
            raise TerminalEgressError("no accepted CaptureContract reproduces this egress pin")
        request_v2 = request if isinstance(request, TerminalEgressRequestV2) else None
        if request_v2 is not None and (
            self.producer != request.procedure_identity
            or self.producer_binding_digest != request.procedure_artifact_digest
        ):
            raise TerminalEgressError(
                "Capture egress sink producer differs from the Procedure request"
            )
        run_coordinate = (
            CaptureRunCoordinateV2(
                run_kind="procedure",
                run_id=request.run_id,
                bound_generation=request.accepted_coordinate.generation_root,
                executable_identity=self.producer,
                executable_digest=self.producer_binding_digest,
            )
            if request_v2 is not None
            else CaptureRunCoordinateV1(
                run_kind="procedure",
                run_id=request.run_id,
                bound_generation=request.accepted_coordinate.generation_root,
                executable_identity=self.producer,
                executable_digest=self.producer_binding_digest,
            )
        )
        children: list[TerminalEgressChildReceiptV1] = []
        producer_receipt_digest = (
            procedure_producer_receipt_digest(request_v2.producer_receipt)
            if request_v2 is not None and request_v2.producer_receipt is not None
            else None
        )
        capture_observed_at = (
            request_v2.evaluation_time if request_v2 is not None else request.prepared_at
        )
        for item in request.items:
            built = (
                build_procedure_capture_v2(
                    store=self.store,
                    contract=contract,
                    source_body=canonical_bytes(item.value),
                    run_coordinate=cast(CaptureRunCoordinateV2, run_coordinate),
                    producer_receipt_digest=producer_receipt_digest,
                    producer=self.producer,
                    producer_binding_digest=self.producer_binding_digest,
                    observed_at=capture_observed_at,
                )
                if producer_receipt_digest is not None
                else build_cas_capture(
                    store=self.store,
                    contract=contract,
                    source_body=canonical_bytes(item.value),
                    run_coordinate=cast(CaptureRunCoordinateV1, run_coordinate),
                    run_receipt_digest=request.admission_binding_digest,
                    producer=self.producer,
                    producer_binding_digest=self.producer_binding_digest,
                    observed_at=request.prepared_at,
                )
            )
            children.append(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index,
                    item_key=item.item_key,
                    egress_digest=built.capture_digest,
                )
            )
        if producer_receipt_digest is not None:
            return TerminalEgressReceiptV2(
                kind="emit_capture",
                run_id=request.run_id,
                node_id=request.node_id,
                disposition="emitted",
                bound_artifact_digest=pin.artifact_digest,
                children=tuple(children),
                producer_receipt_digest=producer_receipt_digest,
            )
        return TerminalEgressReceiptV1(
            kind="emit_capture",
            run_id=request.run_id,
            node_id=request.node_id,
            disposition="emitted",
            bound_artifact_digest=pin.artifact_digest,
            children=tuple(children),
        )


# ---------------------------------------------------------------------------
# The v1 effect gate
# ---------------------------------------------------------------------------


def effect_dispatch_refusal(
    *,
    invocation_origin: Literal["actor", "line"],
    actor_context: GovernedActorContext,
    declared_effect_grants: tuple[str, ...] = (),
) -> tuple[str, str] | None:
    """Return the typed refusal that stops an unattended effect, or ``None``.

    V1 has no unattended effect grant to check, so an unknown grant tag is
    refused before anything else: reinterpreting one as the future
    ``invoke_named_effect`` operation is exactly the mistake §4.4 forbids.
    """

    unknown = tuple(
        sorted(
            set(declared_effect_grants) - RECOGNIZED_EFFECT_GRANT_TAGS,
            key=lambda item: item.encode("utf-8"),
        )
    )
    if unknown:
        return (
            "effect_grant_unrecognized",
            f"V1 registers no external-effect grant; {list(unknown)} is refused, not interpreted.",
        )
    if invocation_origin != "actor":
        return (
            "effect_dispatch_requires_actor",
            "A line-originated run may prepare an effect intent but never dispatch the effect.",
        )
    if actor_context.actor_type == "system":
        return (
            "effect_dispatch_requires_authenticated_actor",
            "A system context is not the authenticated actor request v1 effects require.",
        )
    return None


__all__ = [
    "EFFECTIVE_RUNG_TERMS",
    "MANDATE_FREE_RUNG_CEILING",
    "NO_TERMINAL_EGRESS",
    "RECOGNIZED_EFFECT_GRANT_TAGS",
    "RUNG_REQUIRED_OPERATIONS",
    "SELECTOR_PRIVACY_CEILINGS",
    "SENSITIVITY_TAINT_CEILINGS",
    "TERMINAL_EGRESS_BOUND_KINDS",
    "TERMINAL_EGRESS_DISPOSITIONS",
    "CaptureTerminalEgressSink",
    "EffectiveRungTermReadingV1",
    "EffectiveRungTermV1",
    "EffectiveRungV1",
    "MandateOperationV1",
    "TerminalEgressChildReceiptV1",
    "TerminalEgressDispositionV1",
    "TerminalEgressError",
    "TerminalEgressItemV1",
    "TerminalEgressKindV1",
    "TerminalEgressReceiptV1",
    "TerminalEgressReceiptV2",
    "TerminalEgressRequestV1",
    "TerminalEgressRequestV2",
    "TerminalEgressSinkProtocol",
    "build_terminal_egress_request_v2",
    "compute_effective_rung",
    "effect_dispatch_refusal",
    "effective_rung_digest",
    "verify_terminal_egress_receipt",
    "ProcedureProducerReceiptV1",
    "TerminalAuthorityRefusal",
    "PROCEDURE_MANDATE_REPAIR",
    "procedure_producer_receipt_digest",
    "producer_receipt_for_request",
    "require_procedure_mandate",
    "terminal_operation_key",
]
