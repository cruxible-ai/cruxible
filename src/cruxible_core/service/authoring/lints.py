"""Advisory vocabulary lint for the artifacts an authoring payload changes."""

from __future__ import annotations

import json

from cruxible_client.contracts.authoring.models import (
    AuthoringPayload,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimAuthoringPayloadV2,
    ClaimTypeAuthoringPayload,
    ClaimTypeSuccessionMember,
    SubjectKindAuthoringPayload,
    WorkingSelectionObservation,
)
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeLintWarningV1,
    ClaimTypeProposalLintV1,
    lint_claim_type_input,
    lint_description,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance


def lint_authoring_payload(
    instance: PlaybillInstance,
    payload: AuthoringPayload,
    *,
    coordinate: AcceptedProjectionCoordinate,
) -> ClaimTypeProposalLintV1 | None:
    """Read only the authored members and their accepted vocabulary dependencies."""

    members = payload.members if isinstance(payload, ChangeSetAuthoringPayload) else (payload,)
    declared_kinds = frozenset(
        member.name
        for member in members
        if isinstance(member, SubjectKindAuthoringPayload) and not member.retire
    )
    warnings: list[ClaimTypeLintWarningV1] = []
    for member in members:
        claim_type = None
        source_ids: tuple[str, ...] = ()
        if isinstance(member, ClaimTypeAuthoringPayload):
            # Whole definitions already state their kinds. Description lint is
            # local to this member and needs no evidence-contract inventory.
            warnings.extend(lint_description(member.claim_type.description))
        elif isinstance(member, ClaimTypeSuccessionMember):
            claim_type = member.successor
        elif isinstance(member, ClaimAuthoringPayloadV2 | ClaimAuthoringPayload):
            claim_type = member.dependency_drafts.claim_type
            if isinstance(member.source, WorkingSelectionObservation):
                source_ids = (member.source.source_id,)
        elif isinstance(member, SubjectKindAuthoringPayload):
            warnings.extend(lint_description(member.description))
        if claim_type is not None:
            warnings.extend(
                lint_claim_type_input(
                    instance,
                    claim_type,
                    coordinate=coordinate,
                    anticipated_source_ids=source_ids,
                    declared_subject_kinds=declared_kinds,
                ).warnings
            )
    seen = {
        json.dumps(warning.model_dump(mode="json"), sort_keys=True): warning for warning in warnings
    }
    return (
        ClaimTypeProposalLintV1(warnings=tuple(seen[key] for key in sorted(seen))) if seen else None
    )
