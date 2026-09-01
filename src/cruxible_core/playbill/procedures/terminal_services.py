"""Dark P2-C adapters onto the existing proposal and settlement doors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    canonical_candidate_timestamp,
)
from cruxible_client.contracts.canonical import CandidateDigest, ProposalDigest, Sha256Value
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedure_mandates import ProcedureMandateV1
from cruxible_core.playbill.procedures.egress import (
    TerminalEgressChildReceiptV1,
    TerminalEgressReceiptV2,
    TerminalEgressRequestV2,
    require_procedure_mandate,
)
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalService,
)

if TYPE_CHECKING:
    from cruxible_core.playbill.procedures.execution import ProcedureRunAdmissionV1


class EffectfulTerminalError(PlaybillFormatError):
    """An effectful terminal cannot traverse the governed service door."""


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


@dataclass(frozen=True)
class SettlementCandidateInspection:
    """Read-only candidate facts derived by the sole settlement door."""

    proposal_id: str
    candidate_digest: str
    base_semantic_root: str
    target_paths: tuple[str, ...]


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
    ) -> TerminalEgressReceiptV2:
        if request.kind != "propose_change_set":
            raise EffectfulTerminalError("proposal adapter serves propose_change_set only")
        base_tree = self.service.transport.read_tree(request.accepted_coordinate.git_oid)
        if _changed_paths(base_tree, candidate_tree) != request.target_paths:
            raise EffectfulTerminalError(
                "effectful_target_paths_mismatch: lowered candidate differs from declared targets"
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
        result = self.service.submit(
            actor=AuthenticatedActor(actor_id=actor_id),
            request=ProposalAdmissionRequest(
                target_ref=(
                    f"refs/proposals/{actor_id}/procedure-{request.operation_key.removeprefix('sha256:')[:64]}"
                ),
                proposed_base_oid=request.accepted_coordinate.git_oid,
            ),
            candidate_tree=candidate_tree,
            timestamp=canonical_candidate_timestamp(request.evaluation_time),
        )
        candidate = result.candidate
        if candidate is None:
            codes = ", ".join(item.code for item in result.evaluation.diagnostics)
            raise EffectfulTerminalError(
                f"effectful_candidate_refused: proposal receive refused the candidate ({codes})"
            )
        by_path = {member.path: member for member in candidate.members}
        children: list[TerminalEgressChildReceiptV1] = []
        for item in request.items:
            member = by_path.get(item.item_key)
            candidate_artifact_digest = None if member is None else _candidate_member_digest(member)
            if candidate_artifact_digest is None:
                raise EffectfulTerminalError(
                    "effectful_candidate_receipt_incomplete: item does not name a candidate member"
                )
            children.append(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index,
                    item_key=item.item_key,
                    egress_digest=candidate_artifact_digest,
                )
            )
        return TerminalEgressReceiptV2(
            kind=request.kind,
            run_id=request.run_id,
            node_id=request.node_id,
            disposition="received",
            children=tuple(children),
            operation_key=request.operation_key,
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
            raise EffectfulTerminalError(
                "settlement_base_semantic_root_mismatch: target names another accepted base"
            )
        inspection = self.door.inspect_exact_candidate(target=target)
        if (
            inspection.proposal_id != target.proposal_id
            or inspection.candidate_digest != target.candidate_digest
            or inspection.base_semantic_root != target.base_semantic_root
            or inspection.target_paths != request.target_paths
        ):
            raise EffectfulTerminalError(
                "settlement_candidate_scope_mismatch: target paths are not the exact "
                "candidate delta"
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
            raise EffectfulTerminalError(
                "settlement receipt does not reproduce the exact approved candidate"
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
    "ProposalTerminalAdapter",
    "SettlementCandidateInspection",
    "SettlementDoorProtocol",
    "SettlementDoorResultV1",
    "SettlementTargetV1",
    "SettlementTerminalAdapter",
]
