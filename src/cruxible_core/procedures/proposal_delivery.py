"""The production bridge from a `propose_change_set` terminal to a durable proposal.

A terminal item is a typed Claim proposal, never a candidate tree. This module
turns the items one terminal reached into one change set, hands that set to the
SAME lowering every authoring surface uses, evaluates the exact live
ProcedureMandate against the paths lowering actually changed, and calls the
sole proposal door exactly once per admitted operation.

Three properties are load-bearing:

* **Evidence is the run's own.** Each item cites the produced Capture in its
  own dependency closure as `ExistingCaptureCitationSourceV1`. Nothing an
  author writes into a template can name, borrow, or omit evidence, and the
  Claim's grade is whatever its ClaimType's evidence admission policy says
  about that Capture.
* **Lowering is a pure function of the admitted operation.** Claim IDs are
  minted from the admission binding and the item key, the intent is built in
  memory at the admitted base, and the changed member bytes are digested. A
  retry reproduces the same tree; recovery re-derives it from the journal.
* **One operation, one proposal.** The retained admission binds the operation
  key to the exact authored payload. Recovery returns the same receipt or
  refuses a changed payload even after the candidate Git objects are collected.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.authoring.models import (
    AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
    AuthoringIntentV1,
    CandidateStatusV1,
    ChangeSetAuthoringPayloadV1,
    ChangeSetClaimIdentityV1,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
    SelfSourceBodyV1,
    authoring_change_set_membership,
    authoring_create_fingerprint,
    authoring_member_identity,
    authoring_payload_digest,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateAny,
    ProcedureMandateInvocationV1,
    ProcedureMandateV2,
    evaluate_procedure_mandate,
)
from cruxible_client.contracts.procedures.models import TERMINAL_REQUIRED_RUNGS
from cruxible_client.contracts.procedures.proposal_items import (
    ProcedureClaimProposalItemV1,
    ProcedureClaimProposalItemV2,
)
from cruxible_client.contracts.proposal_models import (
    ProposalResult,
    ProposalSettleSubmissionV1,
)
from cruxible_core.authoring import lowering as authoring_lowering
from cruxible_core.authoring.lowering import AuthoringLoweringError, LoweredAuthoring
from cruxible_core.procedures.egress import (
    PreparedTerminalEgressV1,
    TerminalEgressReceiptV1,
    TerminalEgressRequestV1,
    TerminalEgressRequestV2,
)
from cruxible_core.procedures.nested import ProcedureDelegation, authority_procedure
from cruxible_core.procedures.terminal_dependencies import (
    TerminalItemDependencyManifestV1,
)
from cruxible_core.procedures.terminal_services import (
    ProposalDeliveryRefused,
    ProposalTerminalAdapter,
    proposal_terminal_payload_digest,
    proposal_terminal_receipt,
    proposal_terminal_ref,
    settle_terminal_receipt,
)
from cruxible_core.proposals.delegated_authority import (
    delegated_authority_issues,
    mandate_coverage,
)
from cruxible_core.proposals.proposals import ProposalService

if TYPE_CHECKING:
    from cruxible_core.procedures.execution import ProcedureRunAdmissionV1
    from cruxible_core.runtime.instance import PlaybillInstance

PROPOSAL_CLAIM_ID_DOMAIN = "playbill-procedure-proposal-claim-id-v1"
PROPOSAL_INTENT_ID_DOMAIN = "playbill-procedure-proposal-intent-id-v1"
PROPOSAL_LOWERING_DOMAIN = "playbill-procedure-proposal-lowering-v1"


def proposal_claim_id(*, admission_binding_digest: str, node_id: str, item_key: str) -> str:
    """Mint the Claim ID one terminal item lowers into, from the admitted operation alone."""

    digest = typed_digest(
        Sha256Value,
        PROPOSAL_CLAIM_ID_DOMAIN,
        {
            "admission_binding_digest": admission_binding_digest,
            "item_key": item_key,
            "node_id": node_id,
        },
    ).tagged.removeprefix("sha256:")
    return f"CLM-{digest[:32]}"


def proposal_lowering_digest(changed_members: tuple[tuple[str, bytes], ...]) -> str:
    """Commit to exactly the member bytes one lowering changed."""

    return typed_digest(
        Sha256Value,
        PROPOSAL_LOWERING_DOMAIN,
        {
            "changed_members": [
                {"path": path, "digest": "sha256:" + hashlib.sha256(content).hexdigest()}
                for path, content in changed_members
            ]
        },
    ).tagged


@dataclass(frozen=True)
class PreparedProposal:
    """Everything delivery needs, resolved once from the admitted operation."""

    prepared: PreparedTerminalEgressV1
    candidate_tree: dict[str, bytes]
    changed_members: tuple[tuple[str, bytes], ...]
    item_paths: dict[str, str]
    rationale: str


def proposal_items(
    request: TerminalEgressRequestV1,
) -> tuple[ProcedureClaimProposalItemV1, ...]:
    """Validate every resolved template as one Claim proposal item, typed to the item."""

    items: list[ProcedureClaimProposalItemV1] = []
    for item in request.items:
        try:
            model = (
                ProcedureClaimProposalItemV2
                if isinstance(item.value, dict)
                and item.value.get("tag") == "playbill-procedure-claim-proposal-item-v2"
                else ProcedureClaimProposalItemV1
            )
            items.append(model.model_validate(item.value))
        except ValidationError as exc:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "A resolved candidate template is not one Claim proposal item.",
                details={
                    "item_key": item.item_key,
                    "child_index": item.child_index,
                    "errors": [
                        {"location": list(map(str, error["loc"])), "message": error["msg"]}
                        for error in exc.errors()
                    ],
                },
            ) from exc
    return tuple(items)


def evidence_by_item(
    request: TerminalEgressRequestV1,
    manifests: Mapping[str, TerminalItemDependencyManifestV1],
) -> dict[str, str]:
    """Pick the one produced Capture each item's own closure reached."""

    evidence: dict[str, str] = {}
    for item in request.items:
        manifest = manifests.get(item.item_key)
        produced = () if manifest is None else manifest.produced_capture_digests
        parsed = None
        if (
            isinstance(item.value, dict)
            and item.value.get("tag") == "playbill-procedure-claim-proposal-item-v2"
        ):
            parsed = ProcedureClaimProposalItemV2.model_validate(item.value)
            if isinstance(parsed.source, SelfSourceBodyV1):
                continue
            selected = parsed.source.capture_digest
            if selected not in produced:
                raise ProposalDeliveryRefused(
                    "proposal_item_evidence_missing",
                    "Selected evidence is outside the item's verified closure.",
                )
            evidence[item.item_key] = selected
            continue
        if len(produced) == 0:
            raise ProposalDeliveryRefused(
                "proposal_item_evidence_missing",
                "The item's dependency closure reached no produced Capture to cite.",
                details={"item_key": item.item_key, "child_index": item.child_index},
            )
        if len(produced) > 1:
            raise ProposalDeliveryRefused(
                "proposal_item_evidence_ambiguous",
                "The item's dependency closure reached more than one produced Capture.",
                details={
                    "item_key": item.item_key,
                    "child_index": item.child_index,
                    "produced_capture_digests": list(produced),
                },
            )
        evidence[item.item_key] = produced[0]
    return evidence


def _claim_members(
    request: TerminalEgressRequestV1,
    *,
    items: tuple[ProcedureClaimProposalItemV1, ...],
    evidence: Mapping[str, str],
) -> tuple[tuple[ClaimAuthoringPayloadV3, ...], dict[str, str], dict[str, str]]:
    """Build one Claim member per item; return members, item->claim id, identity->item."""

    members: list[ClaimAuthoringPayloadV3] = []
    claim_ids: dict[str, str] = {}
    identity_to_item: dict[str, str] = {}
    for egress_item, item in zip(request.items, items, strict=True):
        claim_id = item.revises or proposal_claim_id(
            admission_binding_digest=request.admission_binding_digest,
            node_id=request.node_id,
            item_key=egress_item.item_key,
        )
        member = ClaimAuthoringPayloadV3(
            statement=item.statement,
            rationale=item.rationale,
            source=(
                item.source
                if isinstance(item, ProcedureClaimProposalItemV2)
                else ExistingCaptureCitationSourceV1(capture_digest=evidence[egress_item.item_key])
            ),
            citation_role=item.citation_role
            if isinstance(item, ProcedureClaimProposalItemV2)
            else "evidence",
            derivation=item.derivation if isinstance(item, ProcedureClaimProposalItemV2) else None,
            existing_claim_dispositions=(
                item.existing_claim_dispositions
                if isinstance(item, ProcedureClaimProposalItemV2)
                else ()
            ),
            claim_ref=item.revises,
            dependency_drafts=ClaimDependencyDraftsV1(),
        )
        identity = authoring_member_identity(member)
        if identity in identity_to_item:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "Two terminal items author the same Claim statement.",
                details={
                    "item_key": egress_item.item_key,
                    "duplicate_of": identity_to_item[identity],
                },
            )
        identity_to_item[identity] = egress_item.item_key
        claim_ids[egress_item.item_key] = claim_id
        members.append(member)
    return tuple(members), claim_ids, identity_to_item


def _intent(
    request: TerminalEgressRequestV1,
    *,
    instance_id: str,
    payload: ChangeSetAuthoringPayloadV1,
    claim_identities: tuple[ChangeSetClaimIdentityV1, ...],
) -> AuthoringIntentV1:
    membership_digest = typed_digest(
        Sha256Value,
        AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
        {
            "members": [
                {"kind": kind, "identity": identity}
                for kind, identity in authoring_change_set_membership(payload.members)
            ]
        },
    ).tagged.removeprefix("sha256:")
    intent_digest = typed_digest(
        Sha256Value,
        PROPOSAL_INTENT_ID_DOMAIN,
        {
            "admission_binding_digest": request.admission_binding_digest,
            "node_id": request.node_id,
        },
    ).tagged.removeprefix("sha256:")
    evaluation_time = (
        request.evaluation_time
        if isinstance(request, TerminalEgressRequestV2)
        else request.prepared_at
    )
    return AuthoringIntentV1(
        intent_id=f"AIT-{intent_digest[:32]}",
        instance_id=instance_id,
        actor_id=request.actor_context.actor_id,
        canonical_timestamp=canonical_candidate_timestamp(evaluation_time),
        base_coordinate=request.accepted_coordinate,
        semantic_identity=f"ChangeSet:{membership_digest}",
        payload=payload,
        payload_digest=authoring_payload_digest(payload),
        create_fingerprint=authoring_create_fingerprint(
            instance_id=instance_id,
            actor_id=request.actor_context.actor_id,
            payload=payload,
        ),
        candidate_status=CandidateStatusV1(
            state="draft",
            current_accepted_coordinate=request.accepted_coordinate,
        ),
        change_set_claim_identities=claim_identities,
    )


def select_procedure_mandate(
    request: TerminalEgressRequestV1,
    *,
    admission: ProcedureRunAdmissionV1,
    accepted_mandates: Mapping[str, ProcedureMandateAny],
    target_paths: tuple[str, ...],
    delegation: ProcedureDelegation | None = None,
) -> str | None:
    """Bind the one live mandate that covers this request, else the closest.

    Every accepted mandate pinned to this Procedure artifact is evaluated with
    the same law `require_procedure_mandate` applies later. A permitted one is
    bound (lowest digest first, so the choice is stable). If none permits, the
    one with the fewest refusals is bound instead, so the refusal the door
    raises names the real limiting term rather than "no mandate".
    """

    evaluation_time = (
        request.evaluation_time
        if isinstance(request, TerminalEgressRequestV2)
        else request.prepared_at
    )
    authority = authority_procedure(admission, delegation)
    ranked: list[tuple[int, str]] = []
    for digest, mandate in sorted(accepted_mandates.items(), key=lambda item: item[0]):
        evaluation = evaluate_procedure_mandate(
            mandate,
            ProcedureMandateInvocationV1(
                procedure_identity=authority.target,
                procedure_artifact_digest=authority.artifact_digest,
                requested_rung=TERMINAL_REQUIRED_RUNGS[request.kind],  # type: ignore[arg-type]
                requested_authority=admission.hard_caps,
                target_paths=target_paths,
                evaluation_time=evaluation_time,
                accepted_mandate_digest=digest,
            ),
        )
        ranked.append((len(evaluation.refusal_codes), digest))
    if not ranked:
        return None
    return min(ranked)[1]


def select_settle_mandate(
    request: TerminalEgressRequestV1,
    *,
    admission: ProcedureRunAdmissionV1,
    accepted_mandates: Mapping[str, ProcedureMandateAny],
    target_paths: tuple[str, ...],
    base_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    delegation: ProcedureDelegation | None = None,
) -> str:
    """Bind the one settle grant that covers every change; none or several refuse.

    Coverage is the mandate's own law (Procedure, window, resources, namespace,
    ClaimType and change-kind scope); its condition is decided at delivery.
    """

    authority = authority_procedure(admission, delegation)
    covering: list[str] = []
    for digest, mandate in sorted(accepted_mandates.items(), key=lambda item: item[0]):
        if not isinstance(mandate, ProcedureMandateV2) or mandate.grants != "settle":
            continue
        evaluation = evaluate_procedure_mandate(
            mandate,
            ProcedureMandateInvocationV1(
                procedure_identity=authority.target,
                procedure_artifact_digest=authority.artifact_digest,
                requested_rung=3,
                requested_authority=admission.hard_caps,
                target_paths=target_paths,
                evaluation_time=request.prepared_at,
                accepted_mandate_digest=digest,
            ),
        )
        if evaluation.verdict != "permitted":
            continue
        _targets, issues = mandate_coverage(
            mandate,
            scope=target_paths,
            current_tree=base_tree,
            candidate_tree=candidate_tree,
            evaluated_at=request.prepared_at,
        )
        if not issues:
            covering.append(digest)
    if not covering:
        raise ProposalDeliveryRefused(
            "settle_mandate_missing",
            "No live settle ProcedureMandate for this Procedure covers every changed Claim.",
            details={"target_paths": list(target_paths)},
        )
    if len(covering) > 1:
        raise ProposalDeliveryRefused(
            "settle_mandate_ambiguous",
            "More than one settle ProcedureMandate covers this change; exactly one must.",
            details={"mandates": covering},
        )
    return covering[0]


class ProposalTerminalEgressSink:
    """The production `propose_change_set` sink: prepare once, deliver exactly once."""

    def __init__(
        self,
        *,
        instance: PlaybillInstance,
        accepted_mandates: Mapping[str, ProcedureMandateAny],
        proposal_service: Callable[[], ProposalService] | None = None,
        delegation: ProcedureDelegation | None = None,
    ) -> None:
        self.instance = instance
        self.delegation = delegation
        self.accepted_mandates = dict(accepted_mandates)
        self._proposal_service = proposal_service or instance.proposal_service
        self._prepared: dict[tuple[str, str], PreparedProposal] = {}

    # -- preparation --------------------------------------------------------

    def prepare_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
        admission: ProcedureRunAdmissionV1,
        manifests: Mapping[str, TerminalItemDependencyManifestV1] | None = None,
        evidence: Mapping[str, str] | None = None,
    ) -> PreparedTerminalEgressV1:
        if request.kind not in {"propose_change_set", "settle_change_set"}:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "The proposal sink prepares proposal and settle terminals only.",
                details={"kind": request.kind},
            )
        items = proposal_items(request)
        if evidence is None:
            evidence = evidence_by_item(request, manifests or {})
        else:
            # Self-source items retain their body in the request, not a selected
            # Capture. Recovery must use the same rule as evidence_by_item.
            missing = [
                egress_item.item_key
                for egress_item, item in zip(request.items, items, strict=True)
                if egress_item.item_key not in evidence
                and not (
                    isinstance(item, ProcedureClaimProposalItemV2)
                    and isinstance(item.source, SelfSourceBodyV1)
                )
            ]
            if missing:
                raise ProposalDeliveryRefused(
                    "proposal_item_evidence_missing",
                    "The journaled preparation names no Capture for a terminal item.",
                    details={"item_keys": missing},
                )
        members, claim_ids, identity_to_item = _claim_members(
            request,
            items=items,
            evidence=evidence,
        )
        ordered = tuple(
            sorted(members, key=lambda member: authoring_member_identity(member).encode("utf-8"))
        )
        rationale = (
            f"Proposed by Procedure {request.procedure_identity.name} "
            f"run {request.run_id} terminal {request.node_id}"
        )
        payload = ChangeSetAuthoringPayloadV1(members=ordered, rationale=rationale)
        claim_identities = tuple(
            sorted(
                (
                    ChangeSetClaimIdentityV1(
                        member_identity=identity,
                        claim_id=claim_ids[item_key],
                    )
                    for identity, item_key in identity_to_item.items()
                ),
                key=lambda item: item.member_identity.encode("utf-8"),
            )
        )
        intent = _intent(
            request,
            instance_id=self.instance.descriptor.instance_id,
            payload=payload,
            claim_identities=claim_identities,
        )
        try:
            lowered: LoweredAuthoring = authoring_lowering.lower_authoring(
                self.instance,
                intent=intent,
                actor_id=request.actor_context.actor_id,
                derivation_procedure=ArtifactPin(
                    role="reducer",
                    target=request.procedure_identity,
                    artifact_digest=request.procedure_artifact_digest,
                ),
            )
        except AuthoringLoweringError as exc:
            raise ProposalDeliveryRefused(
                "proposal_lowering_refused",
                f"Shared lowering refused the proposed Claims: {exc.message}",
                details={
                    "code": exc.code,
                    "offending_element": exc.offending_element,
                    "repairs": [item.model_dump(mode="json") for item in exc.repairs],
                },
            ) from exc
        if not lowered.changed_members:
            raise ProposalDeliveryRefused(
                "proposal_lowering_refused",
                "The proposed Claims change nothing at the admitted base; there is no proposal.",
                details={"code": "playbill.authoring.no_change"},
            )
        target_paths = tuple(path for path, _content in lowered.changed_members)
        item_paths = {item_key: claim_path(claim_id) for item_key, claim_id in claim_ids.items()}
        for item_key, path in item_paths.items():
            if path not in target_paths:
                raise ProposalDeliveryRefused(
                    "proposal_receipt_incomplete",
                    "A terminal item lowered into no changed member.",
                    details={"item_key": item_key, "path": path},
                )
        mandate_digest = (
            select_settle_mandate(
                request,
                admission=admission,
                accepted_mandates=self.accepted_mandates,
                target_paths=target_paths,
                base_tree=self.instance.tree_at(request.accepted_coordinate.git_oid),
                candidate_tree=lowered.proposed_tree,
                delegation=self.delegation,
            )
            if request.kind == "settle_change_set"
            else select_procedure_mandate(
                request,
                admission=admission,
                accepted_mandates=self.accepted_mandates,
                target_paths=target_paths,
                delegation=self.delegation,
            )
        )
        prepared = PreparedTerminalEgressV1(
            target_paths=target_paths,
            procedure_mandate_digest=mandate_digest,
            lowering_digest=proposal_lowering_digest(lowered.changed_members),
            item_paths=tuple(sorted(item_paths.items(), key=lambda item: item[0].encode("utf-8"))),
        )
        # Lowering already read the base tree and reported exactly which member
        # paths moved; the door is handed those paths rather than a second
        # full-tree read whose only purpose would be to diff them out again.
        self._prepared[(request.admission_binding_digest, request.node_id)] = PreparedProposal(
            prepared=prepared,
            candidate_tree=dict(lowered.proposed_tree),
            changed_members=lowered.changed_members,
            item_paths=item_paths,
            rationale=rationale,
        )
        return prepared

    # -- delivery -----------------------------------------------------------

    def deliver_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
        admission: ProcedureRunAdmissionV1 | None = None,
    ) -> TerminalEgressReceiptV1:
        if not isinstance(request, TerminalEgressRequestV2) or request.kind not in {
            "propose_change_set",
            "settle_change_set",
        }:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "The proposal sink delivers prepared v2 proposal and settle egress only.",
                details={"kind": request.kind},
            )
        key = (request.admission_binding_digest, request.node_id)
        prepared = self._prepared.get(key)
        if prepared is None:
            raise ProposalDeliveryRefused(
                "proposal_receipt_incomplete",
                "The proposal sink was asked to deliver an egress it never prepared.",
                details={"node_id": request.node_id},
            )
        if prepared.prepared.target_paths != request.target_paths:
            raise ProposalDeliveryRefused(
                "proposal_target_paths_mismatch",
                "The egress request names other targets than its own preparation.",
                details={
                    "prepared_target_paths": list(prepared.prepared.target_paths),
                    "request_target_paths": list(request.target_paths),
                },
            )
        if admission is None:
            raise ProposalDeliveryRefused(
                "procedure_authority_admission_invalid",
                "Proposal delivery requires the exact admitted run.",
            )
        service = self._proposal_service()
        assert request.operation_key is not None  # v2 shape
        existing = self.recover_existing(
            request,
            service=service,
            lowering_digest=prepared.prepared.lowering_digest,
        )
        adapter = ProposalTerminalAdapter(
            service=service, bind_projection=self.instance.bind_accepted_projection
        )
        if request.kind == "settle_change_set":
            return self._deliver_settle(
                request,
                admission=admission,
                prepared=prepared,
                adapter=adapter,
                existing=existing,
            )
        if existing is not None:
            return proposal_terminal_receipt(
                request, result=existing, item_paths=prepared.item_paths
            )
        return adapter.deliver(
            request=request,
            admission=admission,
            candidate_tree=prepared.candidate_tree,
            accepted_mandates=self.accepted_mandates,
            item_paths=prepared.item_paths,
            rationale=prepared.rationale,
            changed_paths=prepared.prepared.target_paths,
            delegation=self.delegation,
        )

    def recover_existing(
        self,
        request: TerminalEgressRequestV2,
        *,
        service: ProposalService,
        lowering_digest: str,
    ) -> ProposalResult | None:
        """Recover by immutable operation admission; refs only protect unfinished writes."""
        assert request.operation_key is not None
        actor_id = request.actor_context.actor_id
        ref = proposal_terminal_ref(actor_id, request.operation_key)
        prepared = self._prepared.get((request.admission_binding_digest, request.node_id))
        if prepared is None:  # pragma: no cover - deliver() prepares before recovering
            return None
        evidence = self.instance.proposal_evidence()
        assert evidence.index is not None
        rows = evidence.index.rows(evidence, "target_ref=? AND admission_path IS NOT NULL", (ref,))
        if len(rows) > 1:
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key names more than one durable proposal.",
            )
        if not rows:
            ref_oid = service.transport.read_proposal_ref(ref)
            if ref_oid is not None:
                self._require_same_member_bytes(
                    prepared,
                    lowering_digest=lowering_digest,
                    tree=service.transport.read_tree(ref_oid),
                    details={"target_ref": ref, "ref_oid": ref_oid, "publication": "interrupted"},
                )
            return None
        admission_record = evidence.read_admission(rows[0]["proposal_id"])
        evaluation = evidence.read_evaluation(admission_record.proposal_id)
        candidate = (
            None
            if evaluation.candidate_digest is None
            else evidence.read_candidate(evaluation.candidate_digest)
        )
        if (
            admission_record.proposed_base_oid != request.accepted_coordinate.git_oid
            or evaluation.evaluated_tree_oid is None
            or candidate is None
        ):
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key already names a proposal at another base or with no candidate.",
                details={
                    "proposal_id": admission_record.proposal_id,
                    "proposed_base_oid": admission_record.proposed_base_oid,
                    "admitted_base_oid": request.accepted_coordinate.git_oid,
                },
            )
        if (
            proposal_lowering_digest(prepared.changed_members) != lowering_digest
            or admission_record.source_compilation_digest
            != proposal_terminal_payload_digest(
                prepared.candidate_tree,
                prepared.prepared.target_paths,
                settle=admission_record.settle_submission,
            )
            or (admission_record.settle_submission is not None)
            != (request.kind == "settle_change_set")
            or (
                admission_record.settle_submission is not None
                and admission_record.settle_submission.mandate_digest
                != request.procedure_mandate_digest
            )
        ):
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key already names a proposal carrying another authored payload.",
                details={"proposal_id": admission_record.proposal_id},
            )
        return ProposalResult(
            admission=admission_record,
            evaluation=evaluation,
            candidate=candidate,
        )

    # -- settlement ---------------------------------------------------------

    def _deliver_settle(
        self,
        request: TerminalEgressRequestV2,
        *,
        admission: ProcedureRunAdmissionV1,
        prepared: PreparedProposal,
        adapter: ProposalTerminalAdapter,
        existing: ProposalResult | None,
    ) -> TerminalEgressReceiptV1:
        """Settle under the one covering mandate, or fall back exactly as it declares.

        The condition is checked before anything is written, so a failing
        condition never leaves a refused proposal under the operation key. The
        submission mode is retained on the proposal's admission, so a repeated
        or recovered delivery reports what was actually submitted -- a fallback
        stays a fallback even after it is accepted through ordinary review.
        """

        digest = request.procedure_mandate_digest
        mandate = None if digest is None else self.accepted_mandates.get(digest)
        if not isinstance(mandate, ProcedureMandateV2) or mandate.condition is None:
            raise ProposalDeliveryRefused(
                "settle_mandate_missing",
                "The settle terminal's bound mandate is no longer an accepted settle grant.",
            )
        assert digest is not None
        result = existing
        if result is None:
            head = self.instance.accepted_coordinate()
            issues = delegated_authority_issues(
                mandate_digest=digest,
                scope=request.target_paths,
                current_tree=self.instance.tree_at(head.git_oid),
                candidate_tree=prepared.candidate_tree,
                current=head,
                timestamp=canonical_candidate_timestamp(request.evaluation_time),
                facts=self.instance._accepted_query_facts(self.instance, head),
            )
            codes = sorted({code for code, _message in issues})
            if issues and mandate.condition.fallback == "refuse":
                raise ProposalDeliveryRefused(
                    "settle_condition_refused",
                    "The settle mandate does not authorize this change and declares no "
                    "proposal fallback.",
                    details={"codes": codes, "messages": [m for _code, m in issues]},
                )
            submission = (
                ProposalSettleSubmissionV1(
                    mode="fallback", mandate_digest=digest, fallback_reason=", ".join(codes)
                )
                if issues
                else ProposalSettleSubmissionV1(mode="delegated", mandate_digest=digest)
            )
            result = adapter.submit(
                request=request,
                admission=admission,
                candidate_tree=prepared.candidate_tree,
                accepted_mandates=self.accepted_mandates,
                rationale=prepared.rationale,
                changed_paths=prepared.prepared.target_paths,
                delegation=self.delegation,
                settle_submission=submission,
            )
        settle = result.admission.settle_submission
        assert settle is not None  # submitted above, or verified by recover_existing
        if settle.mode == "fallback":
            return settle_terminal_receipt(
                request,
                result=result,
                item_paths=prepared.item_paths,
                accepted_git_oid=None,
                fallback_reason=settle.fallback_reason,
            )
        if result.candidate is None:
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "The delegated candidate was refused at submission.",
                details={
                    "proposal_id": result.admission.proposal_id,
                    "codes": [item.code for item in result.evaluation.diagnostics],
                },
            )
        accepted_oid = self._accepted_oid(result.candidate.candidate_digest, digest=digest)
        if accepted_oid is None:
            accepted_oid = self._activate(result, request=request, mandate=mandate, digest=digest)
        return settle_terminal_receipt(
            request,
            result=result,
            item_paths=prepared.item_paths,
            accepted_git_oid=accepted_oid,
        )

    def _accepted_oid(self, candidate_digest: str, *, digest: str) -> str | None:
        """The accepted generation that settled this exact candidate under this mandate.

        Found through the history index, never a history walk; its retained
        record must name the same mandate, or it is not this settlement.
        """

        with self.instance.accepted_history_reader() as history:
            location = history.generation_for_candidate(candidate_digest)
            if location is None:
                return None
            record = history.read_generation_record(location.sequence, self.instance.blob_at)
        if record.mandate_digest != digest:
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The delegated candidate was accepted, but not under this settle mandate.",
                details={"accepted_git_oid": location.git_oid},
            )
        return location.git_oid

    def _activate(
        self,
        result: ProposalResult,
        *,
        request: TerminalEgressRequestV2,
        mandate: ProcedureMandateV2,
        digest: str,
    ) -> str:
        from cruxible_client.contracts.errors import SettlementIntegrityError
        from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

        # No late acceptance: the mandate must still stand when the change lands.
        now = datetime.now(timezone.utc)
        if mandate.suspended or not (mandate.valid_from <= now < mandate.expires_at):
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "The settle mandate expired or was suspended before publication.",
            )
        try:
            receipt = service_activate_playbill_proposal(
                self.instance,
                proposal_id=result.admission.proposal_id,
                activated_by=request.actor_context.actor_id,
                mandate_digest=digest,
            )
        except SettlementIntegrityError:
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "The delegated candidate no longer reproduces under its mandate at publication.",
                details={"proposal_id": result.admission.proposal_id},
            ) from None
        if receipt.status != "accepted" or receipt.accepted_coordinate is None:
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "Accepted state moved before the settlement published.",
                details={"proposal_id": result.admission.proposal_id, "status": receipt.status},
            )
        return receipt.accepted_coordinate.git_oid

    @staticmethod
    def _require_same_member_bytes(
        prepared: PreparedProposal,
        *,
        lowering_digest: str,
        tree: Mapping[str, bytes],
        details: dict[str, object],
    ) -> None:
        """Refuse unless `tree` carries exactly the bytes THIS preparation lowered.

        The members themselves are compared, not the journaled digest alone: a
        journal can only say what an earlier attempt claimed to lower, while the
        tree says what the ref actually holds.
        """

        current_digest = proposal_lowering_digest(prepared.changed_members)
        existing_digest = proposal_lowering_digest(
            tuple((path, tree[path]) for path, _content in prepared.changed_members if path in tree)
        )
        if (
            existing_digest != current_digest
            or current_digest != lowering_digest
            or any(path not in tree for path, _content in prepared.changed_members)
        ):
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key already names a proposal carrying other member bytes.",
                details=details,
            )


__all__ = [
    "PROPOSAL_CLAIM_ID_DOMAIN",
    "PROPOSAL_LOWERING_DOMAIN",
    "PreparedProposal",
    "ProposalTerminalEgressSink",
    "evidence_by_item",
    "proposal_claim_id",
    "proposal_items",
    "proposal_lowering_digest",
    "select_procedure_mandate",
    "select_settle_mandate",
]
