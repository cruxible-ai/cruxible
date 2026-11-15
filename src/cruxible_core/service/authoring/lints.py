"""Advisory vocabulary lint for the artifacts an authoring payload changes."""

from __future__ import annotations

import json

from cruxible_client.contracts.authoring.models import (
    AuthoringPayload,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimTypeAuthoringPayload,
    ClaimTypeSuccessionMember,
    SubjectKindAuthoringPayload,
    WorkingSelectionObservation,
)
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeLintWarningV1,
    ClaimTypeProposalLintV1,
    accepted_capture_contracts,
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
    key_predicates = frozenset(
        predicate
        for member in members
        if isinstance(member, SubjectKindAuthoringPayload) and not member.retire
        for predicate in member.keys
    )
    warnings: list[ClaimTypeLintWarningV1] = []
    # One read of the contract inventory for the whole payload, not one per
    # member: a vocabulary of N fields lints in O(N + accepted contracts).
    contracts: dict[str, str] | None = None
    for member in members:
        claim_type = None
        source_ids: tuple[str, ...] = ()
        if isinstance(member, ClaimTypeAuthoringPayload):
            # A whole definition takes the lint `claim-type propose` gives it:
            # the same ClaimType warns the same on every road.
            claim_type = member.claim_type
        elif isinstance(member, ClaimTypeSuccessionMember):
            # A retiring successor admits no more writes: nothing to lint.
            if member.successor.lifecycle.state == "live":
                claim_type = member.successor
        elif isinstance(member, ClaimAuthoringPayload):
            claim_type = member.dependency_drafts.claim_type
            if isinstance(member.source, WorkingSelectionObservation):
                source_ids = (member.source.source_id,)
        elif isinstance(member, SubjectKindAuthoringPayload):
            warnings.extend(
                item.model_copy(update={"artifact": f"SubjectKind:{member.name}"})
                for item in lint_description(member.description)
            )
        if claim_type is not None:
            if contracts is None:
                contracts = accepted_capture_contracts(instance, coordinate=coordinate)
            artifact = f"ClaimType:{claim_type.predicate}"
            warnings.extend(
                item.model_copy(update={"artifact": artifact})
                for item in lint_claim_type_input(
                    instance,
                    claim_type,
                    coordinate=coordinate,
                    anticipated_source_ids=source_ids,
                    declared_subject_kinds=declared_kinds,
                    key_predicates=key_predicates,
                    accepted_contracts=contracts,
                ).warnings
            )
    seen = {
        json.dumps(warning.model_dump(mode="json"), sort_keys=True): warning for warning in warnings
    }
    return (
        ClaimTypeProposalLintV1(warnings=tuple(seen[key] for key in sorted(seen))) if seen else None
    )
