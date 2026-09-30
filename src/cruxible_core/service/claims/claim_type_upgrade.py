"""Move accepted ClaimTypes to ClaimType v7, one reviewed change set.

ClaimTypes before v7 never switch on their own: one accepted ClaimType digest
keeps one revision rule. This builds the ordinary change set that moves the
chosen live ClaimTypes to v7 and carries their Claims, and proposes it; nothing
is activated here.

The move is mechanical and keeps meaning, with one disclosed exception:

- a v6 ClaimType becomes v7 with ``evidence_requirement="self"`` (its meaning),
  no default role and no descriptions;
- a v1-v5 ClaimType first takes the identity-rule conversion of
  ``upgrade-evidence-rules`` (and its disclosed ``widened_versions``);
- ``revision_evidence`` is the one meaning that may change: every ClaimType
  before v7 accumulates, and the upgrade states ``replace`` unless asked for
  ``accumulate``. The result lists that flip per ClaimType.

Every dependent Claim is carried with byte-identical backing, so no Claim loses
evidence by the move: only its later statement-changing revisions follow the new
rule.
"""

from __future__ import annotations

from typing import Literal, cast

from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRefusalV1,
    ClaimTypeUpgradeRequestV1,
    ClaimTypeUpgradeResultV1,
    ClaimTypeUpgradeV1,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDispositionV3,
    ClaimTypeMigrationError,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.compiler.compiler import AUTHORITY_VERBS_COMPILER
from cruxible_core.errors import DataValidationError
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    evaluate_proposal_tree,
    validate_proposal_tree,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.claims.evidence_rule_upgrade import _convert, _Lineages, _Refused

_PreV7Format = Literal[
    "playbill-claim-type-v1",
    "playbill-claim-type-v3",
    "playbill-claim-type-v4",
    "playbill-claim-type-v5",
    "playbill-claim-type-v6",
]


def _to_v7(
    claim_type: ClaimType, lineages: _Lineages, request: ClaimTypeUpgradeRequestV1
) -> tuple[ClaimType, ClaimTypeUpgradeV1]:
    widened: tuple[str, ...] = ()
    identity_ruled = claim_type
    if claim_type.artifact_format != "playbill-claim-type-v6":
        identity_ruled, conversion = _convert(claim_type, lineages)
        widened = conversion.widened_versions
    payload = identity_ruled.model_dump(mode="python")
    payload.update(
        artifact_format="playbill-claim-type-v7",
        evidence_requirement="self",
        revision_evidence=request.revision_evidence,
        lifecycle=identity_ruled.lifecycle.model_copy(
            update={"predecessor_digest": claim_type_digest(claim_type).tagged}
        ),
    )
    successor = ClaimType.model_validate(payload)
    return successor, ClaimTypeUpgradeV1(
        claim_type=claim_type.identity.qualified,
        # The caller only moves ClaimTypes before v7.
        from_format=cast(_PreV7Format, claim_type.artifact_format),
        revision_evidence_after=request.revision_evidence,
        widened_versions=widened,
    )


def service_upgrade_claim_types(
    instance: PlaybillInstance,
    *,
    request: ClaimTypeUpgradeRequestV1,
    actor_id: str,
    timestamp: str,
) -> ClaimTypeUpgradeResultV1:
    """Propose (or, dry, evaluate) the change set moving live ClaimTypes to v7."""

    base = instance.accepted_coordinate()
    if base.compiler != AUTHORITY_VERBS_COMPILER:
        raise DataValidationError("ClaimType v7 needs compiler revision 31; upgrade it first")
    tree = instance.immutable_tree_at(base.git_oid)
    lineages = _Lineages(instance, AcceptedCoordinate.from_internal(base))
    wanted = set(request.claim_types)
    changed: dict[str, bytes] = {}
    upgraded: list[ClaimTypeUpgradeV1] = []
    unchanged: list[str] = []
    refused: list[ClaimTypeUpgradeRefusalV1] = []
    for predicate in sorted(wanted, key=lambda item: item.encode("utf-8")):
        try:
            present = claim_type_path(predicate) in tree
        except PlaybillFormatError:
            present = False
        if not present:
            refused.append(
                ClaimTypeUpgradeRefusalV1(
                    claim_type=f"ClaimType:{predicate}", reason="no accepted ClaimType"
                )
            )
    for path in sorted(item for item in tree if item.startswith("claim-types/")):
        claim_type = parse_claim_type(tree[path], path=path)
        if wanted and claim_type.predicate not in wanted:
            continue
        if claim_type.lifecycle.state != "live":
            if wanted:
                refused.append(
                    ClaimTypeUpgradeRefusalV1(
                        claim_type=claim_type.identity.qualified, reason="retired"
                    )
                )
            continue
        if claim_type.artifact_format == "playbill-claim-type-v7":
            unchanged.append(claim_type.identity.qualified)
            continue
        try:
            successor, entry = _to_v7(claim_type, lineages, request)
        except (_Refused, PlaybillFormatError, ValueError) as error:
            refused.append(
                ClaimTypeUpgradeRefusalV1(
                    claim_type=claim_type.identity.qualified, reason=str(error)
                )
            )
            continue
        changed[path] = render_claim_type(successor)
        upgraded.append(entry)
    refused.sort(key=lambda item: item.claim_type.encode("utf-8"))
    if not changed:
        return ClaimTypeUpgradeResultV1(
            status="unchanged",
            unchanged=tuple(unchanged),
            refused=tuple(refused),
            detail="No ClaimType to upgrade.",
        )
    roots = tuple(parse_claim_type(tree[path], path=path).identity for path in sorted(changed))
    try:
        inventory = dependent_closure_inventory(tree, roots=roots, fixed_paths=frozenset(changed))
        settled, _normalized = build_dependent_closure_candidate(
            tree=tree,
            changed=changed,
            inventory=inventory,
            dispositions=tuple(
                ClaimTypeDependentDispositionV3(identity=item.identity, disposition="successor")
                for item in inventory
            ),
        )
    except ClaimTypeMigrationError as error:
        return ClaimTypeUpgradeResultV1(
            status="blocked",
            upgraded=tuple(upgraded),
            unchanged=tuple(unchanged),
            refused=tuple(refused),
            detail=str(error),
        )
    candidate = tree.fork()
    for path, content in {**settled, **changed}.items():
        candidate[path] = content
    carried = sum(1 for item in inventory if item.path.startswith("claims/"))
    flips = [item for item in upgraded if item.revision_evidence_after == "replace"]
    detail = (
        f"{len(flips)} ClaimType(s) move from accumulate to replace: a revision that changes "
        "its statement then carries exactly the evidence it cites. Carried Claims keep all "
        "of their backing."
        if flips
        else "Every upgraded ClaimType keeps accumulating evidence across revisions."
    )
    if request.dry_run:
        service = instance.proposal_service()
        evaluation = evaluate_proposal_tree(
            base_tree=tree,
            current_tree=tree,
            proposed_tree=validate_proposal_tree(
                candidate, limits=service.receive_limits, base_tree=tree
            ),
            current=base,
            bodies=instance.body_store(),
            timestamp=timestamp,
            rebased=False,
            actor_id=actor_id,
            promotion_verifier=service.promotion_verifier,
            query_facts_provider=service.query_facts_provider,
            principal_registry_provider=instance.accepted_principal_registry,
            accepted_referents_provider=instance.accepted_referent_coordinates,
            historical_artifact_provider=instance.accepted_artifact_version,
        )
        blocked = evaluation.candidate is None
        return ClaimTypeUpgradeResultV1(
            status="would_block" if blocked else "would_propose",
            upgraded=tuple(upgraded),
            unchanged=tuple(unchanged),
            refused=tuple(refused),
            carried_claims=carried,
            detail=(
                "Refused: " + "; ".join(item.code for item in evaluation.diagnostics)
                if blocked
                else detail
            ),
        )
    suffix = canonical_digest(
        "playbill-claim-type-upgrade-target-v1",
        {
            "base": base.semantic_root,
            "members": sorted(changed),
            "revision_evidence": request.revision_evidence,
        },
    )
    submitted = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/claim-type-v7-{suffix[:32]}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=candidate,
        timestamp=timestamp,
    )
    if submitted.evaluation.verdict != "candidate":
        return ClaimTypeUpgradeResultV1(
            status="blocked",
            proposal_id=submitted.admission.proposal_id,
            upgraded=tuple(upgraded),
            unchanged=tuple(unchanged),
            refused=tuple(refused),
            detail="Refused: " + "; ".join(item.code for item in submitted.evaluation.diagnostics),
        )
    return ClaimTypeUpgradeResultV1(
        status="proposed",
        proposal_id=submitted.admission.proposal_id,
        upgraded=tuple(upgraded),
        unchanged=tuple(unchanged),
        refused=tuple(refused),
        carried_claims=carried,
        detail=detail,
    )


__all__ = ["service_upgrade_claim_types"]
