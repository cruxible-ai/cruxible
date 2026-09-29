"""Retire accepted Claims through the change-set retirement member, as the write verbs do.

A retirement is one ``ClaimRetirementMemberV1`` naming the Claim it ``retires``
and its exact live dependent closure. These helpers build that member, submit it
through the ordinary authoring coordinator, and approve and activate the
proposal, so a fixture retires a Claim the only way the product does.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from cruxible_client.contracts.authoring.models import (
    AuthoringSubmitResultV1,
    ChangeSetAuthoringPayloadV1,
    ClaimRetirementMemberV1,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.claims import (
    ClaimRetireDependentV1,
    ClaimRetirementReason,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.claims.claim_retirement import (
    ClaimRetireInventoryItemV1,
    claim_retirement_inventory,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.core_support._support import client_material
from tests.test_ledger.test_activation import _sign


def retirement_inventory(
    instance: PlaybillInstance, claim_id: str
) -> tuple[ClaimRetireInventoryItemV1, ...]:
    """The live dependent closure a retirement of ``claim_id`` must carry, at the head."""

    head = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(head.git_oid)
    path = claim_path(claim_id)
    return claim_retirement_inventory(
        instance,
        tree=tree,
        coordinate=AcceptedCoordinate.from_internal(head),
        claim=parse_claim(tree[path], path=path),
    )


def retirement_member(
    instance: PlaybillInstance,
    claim_id: str,
    *,
    reason: ClaimRetirementReason = "was-rescinded",
    effective_until: datetime | None = None,
    dependent_reasons: Mapping[str, ClaimRetirementReason] | None = None,
    dependents: tuple[ClaimRetireDependentV1, ...] | None = None,
) -> ClaimRetirementMemberV1:
    """One retirement member; its dependents default to the exact closure at the head."""

    if dependents is None:
        dependents = tuple(
            sorted(
                (
                    ClaimRetireDependentV1(
                        artifact_identity=item.artifact_identity,
                        predecessor_digest=item.predecessor_digest,
                        reason=(dependent_reasons or {}).get(item.artifact_identity.name, reason),
                    )
                    for item in retirement_inventory(instance, claim_id)
                ),
                key=lambda item: item.artifact_identity.qualified.encode("utf-8"),
            )
        )
    return ClaimRetirementMemberV1(
        retires=claim_id,
        reason=reason,
        effective_until=effective_until,
        dependents=dependents,
    )


def submit_retirement(
    instance: PlaybillInstance,
    member: ClaimRetirementMemberV1,
    *,
    actor_id: str = "owner",
    timestamp: str | None = None,
) -> AuthoringSubmitResultV1:
    """Create and submit one change set carrying ``member``; a refusal is not raised."""

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id=actor_id)
    created = coordinator.create(
        actor=actor,
        payload=ChangeSetAuthoringPayloadV1(members=(member,)),
        canonical_timestamp=timestamp or canonical_candidate_timestamp(utc_now()),
    )
    return coordinator.submit(created.intent.intent_id, actor=actor)


def refusal_codes(submitted: AuthoringSubmitResultV1) -> set[str]:
    """The preflight diagnostic codes of a submit that did not propose."""

    preflight = submitted.intent.last_preflight
    assert preflight is not None
    return {item.code for item in preflight.frontier.diagnostics}


def refusal_messages(submitted: AuthoringSubmitResultV1) -> str:
    preflight = submitted.intent.last_preflight
    assert preflight is not None
    return " ".join(item.message for item in preflight.frontier.diagnostics)


def candidate_tree(instance: PlaybillInstance, submitted: AuthoringSubmitResultV1) -> Any:
    """The evaluated candidate tree of a submitted retirement."""

    proposal_id = submitted.status.proposal_id
    assert proposal_id is not None, submitted.intent.last_preflight
    evaluation = instance.proposal_evidence().read_evaluation(proposal_id)
    assert evaluation.evaluated_tree_oid is not None
    return instance.proposal_tree(evaluation.evaluated_tree_oid)


def activate_submitted(
    instance: PlaybillInstance, owner: Any, submitted: AuthoringSubmitResultV1
) -> None:
    """Approve (when the policy requires it) and activate one submitted change set."""

    proposal_id = submitted.status.proposal_id
    candidate_digest = submitted.status.candidate_digest
    assert proposal_id is not None and candidate_digest is not None, submitted.intent.last_preflight
    candidate = instance.proposal_evidence().read_candidate(candidate_digest)
    if candidate.approval_requirements:
        approval = _sign(
            client_material(instance.root.parent, instance),
            candidate_digest,
            instance.accepted_coordinate().semantic_root,
        )
        service_submit_playbill_approval(
            instance,
            proposal_id=proposal_id,
            attestation=approval.attestation,
            authenticated_submitter=owner.principal.principal_id,
        )
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"


def retire_claim(
    instance: PlaybillInstance,
    owner: Any,
    claim_id: str,
    *,
    reason: ClaimRetirementReason = "was-rescinded",
    effective_until: datetime | None = None,
) -> AuthoringSubmitResultV1:
    """Retire one accepted Claim, with its closure, and accept the change set."""

    submitted = submit_retirement(
        instance,
        retirement_member(instance, claim_id, reason=reason, effective_until=effective_until),
    )
    activate_submitted(instance, owner, submitted)
    return submitted


__all__ = [
    "activate_submitted",
    "candidate_tree",
    "refusal_codes",
    "refusal_messages",
    "retire_claim",
    "retirement_inventory",
    "retirement_member",
    "submit_retirement",
]
