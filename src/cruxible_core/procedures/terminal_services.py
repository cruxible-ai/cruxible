"""Proposal terminal delivery through the governed proposal door."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    CandidateRecordAnyVersion,
    canonical_candidate_timestamp,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedure_mandates import ProcedureMandateAny
from cruxible_client.contracts.proposal_models import ProposalSettleSubmissionV1
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.procedures.egress import (
    TerminalEgressChildReceiptV2,
    TerminalEgressError,
    TerminalEgressReceiptV2,
    TerminalEgressReceiptV3,
    TerminalEgressReceiptV4,
    TerminalEgressRequestV2,
    require_procedure_mandate,
    require_procedure_mandate_at_head,
)
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalHeadMovedError,
    ProposalResult,
    ProposalService,
)

if TYPE_CHECKING:
    from cruxible_core.indexes.sqlite import ProjectionHandle
    from cruxible_core.procedures.execution import ProcedureRunAdmissionV1
    from cruxible_core.procedures.nested import ProcedureDelegation


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


def _changed_paths(base: Mapping[str, bytes], candidate: Mapping[str, bytes]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {path for path in set(base) | set(candidate) if base.get(path) != candidate.get(path)},
            key=lambda item: item.encode("utf-8"),
        )
    )


def proposal_terminal_payload_digest(
    tree: Mapping[str, bytes],
    paths: tuple[str, ...],
    *,
    settle: ProposalSettleSubmissionV1 | None = None,
) -> str:
    """Retain the exact authored payload binding after candidate Git objects expire.

    A settle terminal's submission mode is bound too, so the admission cannot
    be read back as the other mode than the one it was submitted under.
    """
    settle_binding: dict[str, object] = (
        {} if settle is None else {"settle_submission": settle.model_dump(mode="json")}
    )
    return typed_digest(
        Sha256Value,
        "playbill-procedure-proposal-payload-v1",
        {
            **settle_binding,
            "members": [
                {
                    "path": path,
                    "digest": None
                    if path not in tree
                    else "sha256:" + hashlib.sha256(tree[path]).hexdigest(),
                }
                for path in sorted(paths, key=str.encode)
            ],
        },
    ).tagged


def _candidate_member_digest(
    member: CandidateMemberEvidence | CandidateMemberLawEvidenceV2,
) -> str | None:
    if isinstance(member, CandidateMemberEvidence):
        return member.artifact_digest
    return member.candidate_artifact_digest


HEAD_CONTENTION_ATTEMPTS = 3


class ProposalTerminalAdapter:
    """Lower one complete tree, authorize it, then call ProposalService exactly once."""

    def __init__(
        self,
        *,
        service: ProposalService,
        bind_projection: Callable[
            [AcceptedProjectionCoordinate], AbstractContextManager[ProjectionHandle]
        ],
    ) -> None:
        self.service = service
        self._bind_projection = bind_projection

    def deliver(
        self,
        *,
        request: TerminalEgressRequestV2,
        admission: ProcedureRunAdmissionV1,
        candidate_tree: Mapping[str, bytes],
        accepted_mandates: Mapping[str, ProcedureMandateAny],
        item_paths: Mapping[str, str] | None = None,
        rationale: str | None = None,
        base_tree: Mapping[str, bytes] | None = None,
        changed_paths: tuple[str, ...] | None = None,
        delegation: ProcedureDelegation | None = None,
    ) -> TerminalEgressReceiptV2:
        result = self.submit(
            request=request,
            admission=admission,
            candidate_tree=candidate_tree,
            accepted_mandates=accepted_mandates,
            rationale=rationale,
            base_tree=base_tree,
            changed_paths=changed_paths,
            delegation=delegation,
        )
        return proposal_terminal_receipt(request, result=result, item_paths=item_paths)

    def submit(
        self,
        *,
        request: TerminalEgressRequestV2,
        admission: ProcedureRunAdmissionV1,
        candidate_tree: Mapping[str, bytes],
        accepted_mandates: Mapping[str, ProcedureMandateAny],
        rationale: str | None = None,
        base_tree: Mapping[str, bytes] | None = None,
        changed_paths: tuple[str, ...] | None = None,
        delegation: ProcedureDelegation | None = None,
        settle_submission: ProposalSettleSubmissionV1 | None = None,
    ) -> ProposalResult:
        """Submit the lowered candidate once, under the exact mandate, and return the result.

        A settle terminal passes how it submits -- under its bound mandate's
        delegated authority, or as that mandate's declared fallback -- and the
        admission retains it; a proposal terminal passes none.
        """
        if (settle_submission is not None) != (request.kind == "settle_change_set"):
            raise EffectfulTerminalError("exactly a settle terminal names its submission mode")

        if request.kind not in {"propose_change_set", "settle_change_set"}:
            raise EffectfulTerminalError("proposal adapter serves proposal and settle only")
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
            delegation=delegation,
        )
        actor_id = request.actor_context.actor_id
        assert request.operation_key is not None  # request shape
        # Bind permission to this execution's exact lowered outputs, not a flag
        # that would authorize other Claims in the same candidate or on retry.
        claim_outputs = {
            path: candidate_tree[path]
            for path in changed
            if path.startswith("claims/") and path in candidate_tree
        }

        def _authorize_at_head(
            current: AcceptedProjectionCoordinate,
            _head_tree: Mapping[str, bytes],
        ) -> Mapping[str, bytes]:
            # The mandate admission bound is re-established against the exact
            # coordinate the door evaluates at, inside the door's own read, so a
            # mandate retired since admission cannot author a new proposal and
            # no second read can disagree with the first.
            with self._bind_projection(current) as projection:
                require_procedure_mandate_at_head(
                    request, admission=admission, projection=projection, delegation=delegation
                )
            return claim_outputs

        for attempt in range(HEAD_CONTENTION_ATTEMPTS):
            try:
                result = self.service.submit(
                    actor=AuthenticatedActor(actor_id=actor_id),
                    request=ProposalAdmissionRequest(
                        target_ref=proposal_terminal_ref(actor_id, request.operation_key),
                        proposed_base_oid=request.accepted_coordinate.git_oid,
                        source_compilation_digest=proposal_terminal_payload_digest(
                            candidate_tree, changed, settle=settle_submission
                        ),
                        rationale=rationale,
                    ),
                    candidate_tree=candidate_tree,
                    timestamp=canonical_candidate_timestamp(request.evaluation_time),
                    authorize=_authorize_at_head,
                    settle_submission=settle_submission,
                )
                break
            except ProposalHeadMovedError:
                # Nothing was written. The next attempt reads the new head and
                # re-establishes the mandate there before evaluating again.
                if attempt == HEAD_CONTENTION_ATTEMPTS - 1:
                    raise
        return result


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

    children, candidate = _receipt_children(request, result=result, item_paths=item_paths)
    assert request.operation_key is not None  # request shape
    return TerminalEgressReceiptV3(
        kind=request.kind,
        run_id=request.run_id,
        node_id=request.node_id,
        disposition="received",
        children=children,
        operation_key=request.operation_key,
        proposal_id=result.admission.proposal_id,
        candidate_digest=candidate.candidate_digest,
        target_paths=request.target_paths,
    )


def settle_terminal_receipt(
    request: TerminalEgressRequestV2,
    *,
    result: ProposalResult,
    item_paths: Mapping[str, str] | None,
    accepted_git_oid: str | None,
    fallback_reason: str | None = None,
) -> TerminalEgressReceiptV4:
    """A settle outcome: the accepted generation, or the ordinary proposal it fell back to."""

    children, candidate = _receipt_children(request, result=result, item_paths=item_paths)
    assert request.operation_key is not None and request.procedure_mandate_digest is not None
    settled = accepted_git_oid is not None
    return TerminalEgressReceiptV4(
        kind=request.kind,
        run_id=request.run_id,
        node_id=request.node_id,
        disposition="settled" if settled else "received",
        children=children,
        operation_key=request.operation_key,
        proposal_id=result.admission.proposal_id,
        candidate_digest=candidate.candidate_digest,
        target_paths=request.target_paths,
        outcome="settled" if settled else "proposed",
        procedure_mandate_digest=request.procedure_mandate_digest,
        accepted_git_oid=accepted_git_oid,
        fallback_reason=fallback_reason,
    )


def _receipt_children(
    request: TerminalEgressRequestV2,
    *,
    result: ProposalResult,
    item_paths: Mapping[str, str] | None,
) -> tuple[tuple[TerminalEgressChildReceiptV2, ...], CandidateRecordAnyVersion]:

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
    return tuple(children), candidate


__all__ = [
    "EffectfulTerminalError",
    "ProposalDeliveryRefused",
    "ProposalTerminalAdapter",
    "proposal_terminal_receipt",
    "proposal_terminal_ref",
]
