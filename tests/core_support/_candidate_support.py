"""Test-only candidate construction for acceptance-law fixtures.

Production authoring must use the coordinator. These helpers keep lower-level
acceptance tests focused on their law by constructing candidate trees directly.
"""

from __future__ import annotations

from collections.abc import Mapping

from cruxible_client.contracts.query.definitions import (
    QueryDefinition,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.governance.actor_context import TransportCapability
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    AcceptedCoordinate,
    ProposalInspection,
)
from cruxible_core.service.proposals.proposal_names import canonical_playbill_proposal_name
from tests.core_support._subject_kind_support import declare_created_kinds


def submit_member_candidate(
    instance: PlaybillInstance,
    *,
    members: Mapping[str, bytes],
    actor_id: str,
    proposal_name: str,
    proposal_family: str,
    timestamp: str,
    base: AcceptedCoordinate | None = None,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
) -> ProposalInspection:
    """Submit several artifact members as one candidate, closure included."""

    proposed_base = (
        instance.accepted_coordinate()
        if base is None
        else instance.resolve_accepted_coordinate(
            git_oid=base.git_oid,
            semantic_root=base.semantic_root,
            generation_root=base.generation_root,
            compiler_digest=base.compiler_digest,
        )
    )
    candidate_tree = instance.tree_at(proposed_base.git_oid)
    candidate_tree.update(declare_created_kinds(candidate_tree, dict(members), instance=instance))
    ref_name = canonical_playbill_proposal_name(proposal_name, family=proposal_family)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id, capabilities=capabilities),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{ref_name}",
            proposed_base_oid=proposed_base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return ProposalInspection(
        proposal=result,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )


def _submit_candidate(
    instance: PlaybillInstance,
    *,
    path: str,
    content: bytes,
    actor_id: str,
    proposal_name: str,
    proposal_family: str,
    timestamp: str,
    base: AcceptedCoordinate | None,
    capabilities: tuple[TransportCapability, ...],
) -> ProposalInspection:
    proposed_base = (
        instance.accepted_coordinate()
        if base is None
        else instance.resolve_accepted_coordinate(
            git_oid=base.git_oid,
            semantic_root=base.semantic_root,
            generation_root=base.generation_root,
            compiler_digest=base.compiler_digest,
        )
    )
    candidate_tree = instance.tree_at(proposed_base.git_oid)
    candidate_tree.update(declare_created_kinds(candidate_tree, {path: content}, instance=instance))
    ref_name = canonical_playbill_proposal_name(proposal_name, family=proposal_family)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id, capabilities=capabilities),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{ref_name}",
            proposed_base_oid=proposed_base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return ProposalInspection(
        proposal=result,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )


def submit_subject_candidate(
    instance: PlaybillInstance,
    *,
    shell: SubjectShell,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: AcceptedCoordinate | None = None,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
) -> ProposalInspection:
    return _submit_candidate(
        instance,
        path=subject_path(shell.subject_kind, shell.subject_id),
        content=render_subject(shell),
        actor_id=actor_id,
        proposal_name=proposal_name,
        proposal_family="subject",
        timestamp=timestamp,
        base=base,
        capabilities=capabilities,
    )


def submit_query_definition_candidate(
    instance: PlaybillInstance,
    *,
    query: QueryDefinition,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: AcceptedCoordinate | None = None,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
) -> ProposalInspection:
    from cruxible_core.compiler.compiler import LIGHT_ARTIFACTS_COMPILER

    if instance.accepted_coordinate().compiler == LIGHT_ARTIFACTS_COMPILER:
        query = query.model_copy(update={"artifact_format": "cruxible-query-definition-v3"})
    return _submit_candidate(
        instance,
        path=query_definition_path(query.identity.name),
        content=render_query_definition(query),
        actor_id=actor_id,
        proposal_name=proposal_name,
        proposal_family="query definition",
        timestamp=timestamp,
        base=base,
        capabilities=capabilities,
    )
