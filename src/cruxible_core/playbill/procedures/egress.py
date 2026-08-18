"""§8.5 effective-rung cap, typed terminal egress, and the v1 effect gate.

Terminal authority is never read from one place.  §8.5.1 computes effective
capability as the minimum of five independent terms — the Procedure hard cap,
the LineSpec's requested rung, the propagated sensitivity cap, the live mandate
grant, and the calibration cap — and every one of them may only narrow.  A
missing or expired mandate contributes nothing: it leaves the mandate-free
ceiling in place rather than lifting it, so no trigger, landing event, or
calibration reading can ever manufacture settlement authority.  Because the cap
is a minimum, the refusal it produces names the exact limiting term; honest
scarcity is a property of the refusal, not a later reconstruction.

Egress above the cap does not exist, and egress below it is typed by kind:
``emit_capture`` emits inert evidence, ``post_inbox`` posts human attention,
``propose_change_set`` reaches proposal *receive* only, and
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
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.captures import (
    CaptureContractV1,
    CaptureObjectStoreProtocol,
    CaptureRunCoordinateV1,
    build_cas_capture,
    capture_contract_digest,
)
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.procedures.models import TERMINAL_REQUIRED_RUNGS
from cruxible_core.playbill.procedures.terminal_dependencies import (
    TAINT_ACCEPTED_STATE,
    TAINT_CONSERVATIVE_DEFAULT,
    TAINT_OMITTED_OPTIONAL,
    TAINT_UNPROMOTED_EXHAUST,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.standing_mandates import MandateGrantV1, MandateRuntimeCapV1
from cruxible_core.temporal import ensure_utc

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

#: The highest rung reachable without any live mandate.  Inert evidence, human
#: attention, and an untrusted proposal need no granted authority; settlement
#: does, so absence and expiry leave this ceiling exactly where it is.
MANDATE_FREE_RUNG_CEILING = 2

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
) -> EffectiveRungTermReadingV1:
    for basis_digest in sorted(mandate_grants, key=lambda item: item.encode("ascii")):
        grant = mandate_grants[basis_digest]
        if grant.settlement == "settle_named_deltas" and (
            "activate_change_set" in grant.permitted_operations
        ):
            return EffectiveRungTermReadingV1(
                term="mandate_grant",
                rung=3,
                reason="A live mandate grant permits settling named deltas.",
                basis_digest=basis_digest,
            )
    return EffectiveRungTermReadingV1(
        term="mandate_grant",
        rung=MANDATE_FREE_RUNG_CEILING,
        reason="No live mandate grant permits activation; absence and expiry contribute nothing.",
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
    prepared_at: datetime

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
        run_coordinate = CaptureRunCoordinateV1(
            run_kind="procedure",
            run_id=request.run_id,
            bound_generation=request.accepted_coordinate.generation_root,
            executable_identity=request.procedure_identity,
            executable_digest=request.procedure_artifact_digest,
        )
        children: list[TerminalEgressChildReceiptV1] = []
        for item in request.items:
            built = build_cas_capture(
                store=self.store,
                contract=contract,
                source_body=canonical_bytes(item.value),
                run_coordinate=run_coordinate,
                run_receipt_digest=request.admission_binding_digest,
                producer=self.producer,
                producer_binding_digest=self.producer_binding_digest,
                observed_at=request.prepared_at,
            )
            children.append(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index,
                    item_key=item.item_key,
                    egress_digest=built.capture_digest,
                )
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
    "TerminalEgressRequestV1",
    "TerminalEgressSinkProtocol",
    "compute_effective_rung",
    "effect_dispatch_refusal",
    "effective_rung_digest",
    "verify_terminal_egress_receipt",
]
