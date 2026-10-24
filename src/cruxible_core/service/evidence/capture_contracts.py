"""CaptureContract input proposals through the shared governed proposal path."""

from __future__ import annotations

from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    lower_capture_contract_input,
)
from cruxible_client.contracts.captures import (
    capture_contract_path,
    parse_capture_contract,
    render_capture_contract,
)
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    AcceptedCoordinate,
    ProposalInspection,
    admit_proposal,
)
from cruxible_core.service.proposals.proposal_names import canonical_playbill_proposal_name


def service_propose_capture_contract(
    instance: PlaybillInstance,
    *,
    input: CaptureContractInputRecord,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: AcceptedCoordinate | None = None,
    dry_run: bool | None = None,
    at: str | None = None,
) -> ProposalInspection:
    coordinate = (
        instance.accepted_coordinate()
        if base is None
        else instance.resolve_accepted_coordinate(
            git_oid=base.git_oid,
            semantic_root=base.semantic_root,
            generation_root=base.generation_root,
            compiler_digest=base.compiler_digest,
        )
    )
    tree = instance.immutable_tree_at(coordinate.git_oid)
    path = capture_contract_path(input.name)
    previous_bytes = tree.get(path)
    previous = None if previous_bytes is None else parse_capture_contract(previous_bytes, path=path)
    contract = lower_capture_contract_input(input, predecessor=previous)
    candidate = tree.fork()
    candidate[path] = render_capture_contract(contract)
    name = canonical_playbill_proposal_name(proposal_name, family="capture contract")
    return admit_proposal(
        instance,
        dry_run=dry_run,
        at=at,
        operation="cruxible.capture-contract.propose",
        describe=f"proposing CaptureContract {input.name}",
        actor_id=actor_id,
        proposed_base=coordinate,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{name}", proposed_base_oid=coordinate.git_oid
        ),
        candidate_tree=candidate,
        timestamp=timestamp,
        capabilities=("propose",),
    )
