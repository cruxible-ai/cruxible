"""Proposal terminal delivery through the governed proposal door."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    canonical_candidate_timestamp,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedure_mandates import ProcedureMandateV1
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.procedures.egress import (
    TerminalEgressChildReceiptV2,
    TerminalEgressError,
    TerminalEgressReceiptV2,
    TerminalEgressReceiptV3,
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
            current: AcceptedProjectionCoordinate,
            _head_tree: Mapping[str, bytes],
        ) -> None:
            # The mandate admission bound is re-established against the exact
            # coordinate the door evaluates at, inside the door's own read, so a
            # mandate retired since admission cannot author a new proposal and
            # no second read can disagree with the first.
            with self._bind_projection(current) as projection:
                require_procedure_mandate_at_head(
                    request, admission=admission, projection=projection
                )

        for attempt in range(HEAD_CONTENTION_ATTEMPTS):
            try:
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
                break
            except ProposalHeadMovedError:
                # Nothing was written. The next attempt reads the new head and
                # re-establishes the mandate there before evaluating again.
                if attempt == HEAD_CONTENTION_ATTEMPTS - 1:
                    raise
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


__all__ = [
    "EffectfulTerminalError",
    "ProposalDeliveryRefused",
    "ProposalTerminalAdapter",
    "proposal_terminal_receipt",
    "proposal_terminal_ref",
]
