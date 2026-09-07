"""Dark P2-C adapters onto the existing proposal and settlement doors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    canonical_candidate_timestamp,
)
from cruxible_client.contracts.canonical import CandidateDigest, ProposalDigest, Sha256Value
from cruxible_client.contracts.errors import PlaybillFormatError, PrincipalIntegrityError
from cruxible_client.contracts.principals import principal_registry_from_tree
from cruxible_client.contracts.procedure_mandates import ProcedureMandateV1
from cruxible_client.contracts.procedures.results import ProcedureSettlementRefusalCodeV1
from cruxible_core.playbill.procedures.egress import (
    TerminalEgressChildReceiptV1,
    TerminalEgressChildReceiptV2,
    TerminalEgressError,
    TerminalEgressReceiptV2,
    TerminalEgressReceiptV3,
    TerminalEgressRequestV2,
    require_procedure_mandate,
    require_procedure_mandate_at_head,
)
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalResult,
    ProposalService,
)
from cruxible_core.playbill.service.documents import service_activate_playbill_proposal

if TYPE_CHECKING:
    from cruxible_core.playbill.instance import PlaybillInstance
    from cruxible_core.playbill.procedures.execution import ProcedureRunAdmissionV1


class EffectfulTerminalError(PlaybillFormatError):
    """An effectful terminal cannot traverse the governed service door."""


class ProposalDeliveryRefused(EffectfulTerminalError, TerminalEgressError):
    """Typed proposal-door refusal preserved as the run's node refusal.

    The code is a member of the served node-refusal vocabulary, so the run
    state names exactly why the proposal was not produced and the served
    repair catalog answers it; the details carry what the door observed.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: object | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = {} if details is None else details


class ProcedureSettlementRefused(EffectfulTerminalError, TerminalEgressError):
    """Typed settlement-door refusal preserved by Procedure run projection."""

    def __init__(
        self,
        code: ProcedureSettlementRefusalCodeV1,
        message: str,
        *,
        details: object | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = {} if details is None else details
        self.retryable = retryable


class SettlementLostCas(ProcedureSettlementRefused):
    """Activation lost its exact-head race and must be prepared again."""

    code: Literal["settlement_lost_cas"] = "settlement_lost_cas"

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(self.code, message, details=details, retryable=True)


class _StrictTerminalServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SettlementTargetV1(_StrictTerminalServiceModel):
    """The exact approved candidate a settlement occurrence is allowed to drive."""

    tag: Literal["playbill-settlement-target-v1"] = "playbill-settlement-target-v1"
    proposal_id: str
    candidate_digest: str
    base_semantic_root: str

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id(cls, value: str) -> str:
        ProposalDigest.from_tagged(value)
        return value

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @field_validator("base_semantic_root")
    @classmethod
    def _base_semantic_root(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class SettlementDoorResultV1(_StrictTerminalServiceModel):
    tag: Literal["playbill-settlement-door-result-v1"] = "playbill-settlement-door-result-v1"
    status: Literal["accepted", "lost_cas"]
    proposal_id: str
    candidate_digest: str
    observed_head: AcceptedCoordinate | None = None

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id(cls, value: str) -> str:
        ProposalDigest.from_tagged(value)
        return value

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _lost_cas_head(self) -> "SettlementDoorResultV1":
        if (self.observed_head is not None) != (self.status == "lost_cas"):
            raise ValueError("only a lost-CAS settlement result names the observed head")
        return self


class SettlementDoorProtocol(Protocol):
    """Adapter boundary whose implementation must call the sole activation service."""

    def inspect_exact_candidate(
        self,
        *,
        target: SettlementTargetV1,
    ) -> SettlementCandidateInspection: ...

    def activate_exact_candidate(
        self,
        *,
        target: SettlementTargetV1,
        actor_id: str,
    ) -> SettlementDoorResultV1: ...


class SettlementCandidateInspection(_StrictTerminalServiceModel):
    """Read-only candidate facts derived by the sole settlement door."""

    proposal_id: str
    candidate_digest: str
    base_semantic_root: str
    target_paths: tuple[str, ...]


class PlaybillSettlementDoor:
    """Concrete exact-candidate door over the existing proposal/activation services."""

    def __init__(
        self,
        *,
        instance: PlaybillInstance,
        admitted_coordinate: AcceptedCoordinate,
    ) -> None:
        self.instance = instance
        self.admitted_coordinate = admitted_coordinate

    def inspect_exact_candidate(
        self,
        *,
        target: SettlementTargetV1,
    ) -> SettlementCandidateInspection:
        evidence = self.instance.proposal_evidence()
        proposal_id = evidence.resolve_proposal_id(target.proposal_id)
        if proposal_id != target.proposal_id:
            raise ProcedureSettlementRefused(
                "settlement_proposal_id_mismatch",
                "Target must name the full proposal id.",
            )
        admission = evidence.read_admission(proposal_id)
        evaluation = evidence.read_evaluation(proposal_id)
        if (
            admission.proposal_id != proposal_id
            or evaluation.proposal_id != proposal_id
            or evaluation.candidate_digest != target.candidate_digest
            or evaluation.evaluated_tree_oid is None
        ):
            raise ProcedureSettlementRefused(
                "settlement_candidate_mismatch",
                "Proposal evidence names another candidate.",
            )
        candidate = evidence.read_candidate(target.candidate_digest)
        if candidate.candidate_digest != target.candidate_digest:
            raise ProcedureSettlementRefused(
                "settlement_candidate_mismatch",
                "Candidate bytes do not reproduce.",
            )
        base = self.instance.coordinate_for_oid(evaluation.evaluated_base_oid)
        if (
            AcceptedCoordinate.from_internal(base) != self.admitted_coordinate
            or base.semantic_root != target.base_semantic_root
            or candidate.candidate.parent_semantic_root != target.base_semantic_root
        ):
            raise ProcedureSettlementRefused(
                "settlement_base_semantic_root_mismatch",
                "Candidate is not based at admission.",
            )
        base_tree = self.instance.tree_at(evaluation.evaluated_base_oid)
        candidate_tree = self.instance.proposal_tree(evaluation.evaluated_tree_oid)
        return SettlementCandidateInspection(
            proposal_id=proposal_id,
            candidate_digest=candidate.candidate_digest,
            base_semantic_root=base.semantic_root,
            target_paths=_changed_paths(base_tree, candidate_tree),
        )

    def activate_exact_candidate(
        self,
        *,
        target: SettlementTargetV1,
        actor_id: str,
    ) -> SettlementDoorResultV1:
        self.inspect_exact_candidate(target=target)
        current = self.instance.accepted_coordinate()
        if AcceptedCoordinate.from_internal(current) != self.admitted_coordinate:
            raise ProcedureSettlementRefused(
                "settlement_activation_coordinate_changed",
                "Accepted state advanced before activation.",
            )
        principals = principal_registry_from_tree(
            self.instance.tree_at(self.admitted_coordinate.git_oid),
            semantic_root=self.admitted_coordinate.semantic_root,
        )
        try:
            principals.require_active(actor_id)
        except PrincipalIntegrityError as exc:
            raise ProcedureSettlementRefused(
                "settlement_actor_principal_invalid",
                "Actor is not active at admission.",
            ) from exc
        activated = service_activate_playbill_proposal(
            self.instance,
            proposal_id=target.proposal_id,
            activated_by=actor_id,
        )
        return SettlementDoorResultV1(
            status=activated.status,
            proposal_id=target.proposal_id,
            candidate_digest=target.candidate_digest,
            observed_head=(
                AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())
                if activated.status == "lost_cas"
                else None
            ),
        )


def _changed_paths(base: Mapping[str, bytes], candidate: Mapping[str, bytes]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {path for path in set(base) | set(candidate) if base.get(path) != candidate.get(path)},
            key=lambda item: item.encode("utf-8"),
        )
    )


def _candidate_member_digest(
    member: CandidateMemberEvidence | CandidateMemberLawEvidenceV2,
) -> str | None:
    if isinstance(member, CandidateMemberEvidence):
        return member.artifact_digest
    return member.candidate_artifact_digest


class ProposalTerminalAdapter:
    """Lower one complete tree, authorize it, then call ProposalService exactly once."""

    def __init__(self, *, service: ProposalService) -> None:
        self.service = service

    def deliver(
        self,
        *,
        request: TerminalEgressRequestV2,
        admission: ProcedureRunAdmissionV1,
        candidate_tree: Mapping[str, bytes],
        accepted_mandates: Mapping[str, ProcedureMandateV1],
        item_paths: Mapping[str, str] | None = None,
        rationale: str | None = None,
        base_tree: Mapping[str, bytes] | None = None,
        changed_paths: tuple[str, ...] | None = None,
    ) -> TerminalEgressReceiptV2:
        if request.kind != "propose_change_set":
            raise EffectfulTerminalError("proposal adapter serves propose_change_set only")
        if changed_paths is None:
            # A caller that lowered the tree already knows exactly which paths
            # moved; only a caller handing over a bare tree pays for the diff.
            if base_tree is None:
                base_tree = self.service.transport.read_tree(request.accepted_coordinate.git_oid)
            changed_paths = _changed_paths(base_tree, candidate_tree)
        changed = changed_paths
        if changed != request.target_paths:
            raise ProposalDeliveryRefused(
                "proposal_target_paths_mismatch",
                "The lowered candidate differs from the terminal's declared targets.",
                details={
                    "declared_target_paths": list(request.target_paths),
                    "lowered_target_paths": list(changed),
                },
            )
        # Authority is the last pure check and occurs before ProposalService can
        # create a ref, write admission evidence, or commit proposal bytes.
        require_procedure_mandate(
            request,
            admission=admission,
            accepted_mandates=accepted_mandates,
        )
        actor_id = request.actor_context.actor_id
        assert request.operation_key is not None  # request shape

        def _authorize_at_head(
            _current: AcceptedProjectionCoordinate,
            head_tree: Mapping[str, bytes],
        ) -> None:
            # The mandate admission bound is re-established against the exact
            # head tree the door evaluates at, inside the door's own read, so a
            # mandate retired since admission cannot author a new proposal and
            # no second read can disagree with the first.
            require_procedure_mandate_at_head(request, admission=admission, head_tree=head_tree)

        result = self.service.submit(
            actor=AuthenticatedActor(actor_id=actor_id),
            request=ProposalAdmissionRequest(
                target_ref=proposal_terminal_ref(actor_id, request.operation_key),
                proposed_base_oid=request.accepted_coordinate.git_oid,
                rationale=rationale,
            ),
            candidate_tree=candidate_tree,
            timestamp=canonical_candidate_timestamp(request.evaluation_time),
            authorize=_authorize_at_head,
        )
        return proposal_terminal_receipt(
            request,
            result=result,
            item_paths=item_paths,
        )


def proposal_terminal_ref(actor_id: str, operation_key: str) -> str:
    """The one proposal ref an admitted operation may create, keyed on its identity."""

    return f"refs/proposals/{actor_id}/procedure-{operation_key.removeprefix('sha256:')[:64]}"


def proposal_terminal_receipt(
    request: TerminalEgressRequestV2,
    *,
    result: ProposalResult,
    item_paths: Mapping[str, str] | None,
) -> TerminalEgressReceiptV2:
    """Bind every terminal item to the exact candidate member it lowered into."""

    candidate = result.candidate
    if candidate is None:
        codes = tuple(item.code for item in result.evaluation.diagnostics)
        raise ProposalDeliveryRefused(
            "proposal_candidate_refused",
            "Proposal receive refused the lowered candidate.",
            details={
                "proposal_id": result.admission.proposal_id,
                "diagnostics": [
                    item.model_dump(mode="json") for item in result.evaluation.diagnostics
                ],
                "codes": list(codes),
            },
        )
    by_path = {member.path: member for member in candidate.members}
    children: list[TerminalEgressChildReceiptV2] = []
    for item in request.items:
        path = item.item_key if item_paths is None else item_paths.get(item.item_key)
        member = None if path is None else by_path.get(path)
        candidate_artifact_digest = None if member is None else _candidate_member_digest(member)
        if path is None or candidate_artifact_digest is None:
            raise ProposalDeliveryRefused(
                "proposal_receipt_incomplete",
                "A terminal item does not name a candidate member.",
                details={"item_key": item.item_key, "path": path},
            )
        children.append(
            TerminalEgressChildReceiptV2(
                child_index=item.child_index,
                item_key=item.item_key,
                egress_digest=candidate_artifact_digest,
                path=path,
            )
        )
    assert request.operation_key is not None  # request shape
    return TerminalEgressReceiptV3(
        kind=request.kind,
        run_id=request.run_id,
        node_id=request.node_id,
        disposition="received",
        children=tuple(children),
        operation_key=request.operation_key,
        proposal_id=result.admission.proposal_id,
        candidate_digest=candidate.candidate_digest,
        target_paths=request.target_paths,
    )


class SettlementTerminalAdapter:
    """Authorize one exact target, then delegate to the sole settlement door."""

    def __init__(self, *, door: SettlementDoorProtocol) -> None:
        self.door = door

    def deliver(
        self,
        *,
        request: TerminalEgressRequestV2,
        admission: ProcedureRunAdmissionV1,
        accepted_mandates: Mapping[str, ProcedureMandateV1],
    ) -> TerminalEgressReceiptV2:
        if request.kind != "mandate_settlement" or len(request.items) != 1:
            raise EffectfulTerminalError(
                "settlement adapter serves one mandate_settlement target only"
            )
        target = SettlementTargetV1.model_validate(request.items[0].value)
        if target.base_semantic_root != request.accepted_coordinate.semantic_root:
            raise ProcedureSettlementRefused(
                "settlement_base_semantic_root_mismatch",
                "Target names another accepted base.",
            )
        inspection = self.door.inspect_exact_candidate(target=target)
        if (
            inspection.proposal_id != target.proposal_id
            or inspection.candidate_digest != target.candidate_digest
            or inspection.base_semantic_root != target.base_semantic_root
            or inspection.target_paths != request.target_paths
        ):
            raise ProcedureSettlementRefused(
                "settlement_candidate_scope_mismatch",
                "Target paths are not the exact candidate delta.",
            )
        require_procedure_mandate(
            request,
            admission=admission,
            accepted_mandates=accepted_mandates,
        )
        result = self.door.activate_exact_candidate(
            target=target,
            actor_id=request.actor_context.actor_id,
        )
        if (
            result.proposal_id != target.proposal_id
            or result.candidate_digest != target.candidate_digest
        ):
            raise ProcedureSettlementRefused(
                "settlement_receipt_mismatch",
                "Settlement receipt does not reproduce the exact approved candidate.",
            )
        if result.status == "lost_cas":
            assert result.observed_head is not None
            raise SettlementLostCas(
                f"{SettlementLostCas.code}: accepted head advanced to "
                f"{result.observed_head.git_oid}; re-prepare at the new head"
            )
        assert request.bound_artifact_pin is not None  # request shape
        assert request.operation_key is not None  # request shape
        return TerminalEgressReceiptV2(
            kind=request.kind,
            run_id=request.run_id,
            node_id=request.node_id,
            disposition="settled",
            bound_artifact_digest=request.bound_artifact_pin.artifact_digest,
            children=(
                TerminalEgressChildReceiptV1(
                    child_index=0,
                    item_key=request.items[0].item_key,
                    egress_digest=result.candidate_digest,
                ),
            ),
            operation_key=request.operation_key,
        )


__all__ = [
    "EffectfulTerminalError",
    "ProposalDeliveryRefused",
    "ProposalTerminalAdapter",
    "proposal_terminal_receipt",
    "proposal_terminal_ref",
    "PlaybillSettlementDoor",
    "ProcedureSettlementRefused",
    "SettlementCandidateInspection",
    "SettlementDoorProtocol",
    "SettlementDoorResultV1",
    "SettlementLostCas",
    "SettlementTargetV1",
    "SettlementTerminalAdapter",
]
