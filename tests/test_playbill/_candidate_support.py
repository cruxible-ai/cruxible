"""Test-only candidate construction for acceptance-law fixtures.

Production authoring must use the coordinator. These helpers keep lower-level
acceptance tests focused on their law by constructing candidate trees directly.
"""

from __future__ import annotations

from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.playbill.actor_context import TransportCapability
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)
from cruxible_core.playbill.service.proposal_names import canonical_playbill_proposal_name


def _submit_candidate(
    instance: PlaybillInstance,
    *,
    path: str,
    content: bytes,
    actor_id: str,
    proposal_name: str,
    proposal_family: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None,
    capabilities: tuple[TransportCapability, ...],
) -> PlaybillProposalInspection:
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
    candidate_tree[path] = content
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
    return PlaybillProposalInspection(
        proposal=result,
        accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ),
    )


def submit_subject_candidate(
    instance: PlaybillInstance,
    *,
    shell: SubjectShell,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None = None,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
) -> PlaybillProposalInspection:
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
    query: QueryDefinitionV1,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None = None,
    capabilities: tuple[TransportCapability, ...] = ("propose",),
) -> PlaybillProposalInspection:
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
