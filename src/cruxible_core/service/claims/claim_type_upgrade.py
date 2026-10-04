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
    ClaimTypeUpgrade,
    ClaimTypeUpgradeRefusal,
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
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
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.compiler.compiler import AUTHORITY_VERBS_COMPILER, GOVERNED_TRIGGERS_COMPILER
from cruxible_core.errors import DataValidationError
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import ChangeMode, admit_change_set, change_scope
from cruxible_core.service.claims.evidence_rule_upgrade import _convert, _Lineages, _Refused

_PreV7Format = Literal[
    "playbill-claim-type-v1",
    "playbill-claim-type-v3",
    "playbill-claim-type-v4",
    "playbill-claim-type-v5",
    "playbill-claim-type-v6",
]


def _to_v7(
    claim_type: ClaimType, lineages: _Lineages, request: ClaimTypeUpgradeRequest
) -> tuple[ClaimType, ClaimTypeUpgrade]:
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
    return successor, ClaimTypeUpgrade(
        claim_type=claim_type.identity.qualified,
        # The caller only moves ClaimTypes before v7.
        from_format=cast(_PreV7Format, claim_type.artifact_format),
        revision_evidence_after=request.revision_evidence,
        widened_versions=widened,
    )


def service_upgrade_claim_types(
    instance: PlaybillInstance,
    *,
    request: ClaimTypeUpgradeRequest,
    actor_id: str,
    timestamp: str,
) -> ClaimTypeUpgradeResult:
    """Propose (or preview) the change set moving live ClaimTypes to v7.

    The change set carries every dependent Claim, so it previews unless
    ``dry_run`` is false.
    """

    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="playbill.claim-type.upgrade",
        describe="the ClaimType v7 upgrade",
    ) as mode:
        return _upgrade(instance, mode, request=request, actor_id=actor_id, timestamp=timestamp)


def _upgrade(
    instance: PlaybillInstance,
    mode: ChangeMode,
    *,
    request: ClaimTypeUpgradeRequest,
    actor_id: str,
    timestamp: str,
) -> ClaimTypeUpgradeResult:
    assert mode.head is not None
    base = mode.head
    if base.compiler not in (AUTHORITY_VERBS_COMPILER, GOVERNED_TRIGGERS_COMPILER):
        raise DataValidationError(
            "ClaimType v7 needs compiler revision 31 or later; upgrade it first"
        )
    tree = instance.immutable_tree_at(base.git_oid)
    lineages = _Lineages(instance, AcceptedCoordinate.from_internal(base))
    wanted = set(request.claim_types)
    changed: dict[str, bytes] = {}
    upgraded: list[ClaimTypeUpgrade] = []
    unchanged: list[str] = []
    refused: list[ClaimTypeUpgradeRefusal] = []
    for predicate in sorted(wanted, key=lambda item: item.encode("utf-8")):
        try:
            present = claim_type_path(predicate) in tree
        except PlaybillFormatError:
            present = False
        if not present:
            refused.append(
                ClaimTypeUpgradeRefusal(
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
                    ClaimTypeUpgradeRefusal(
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
                ClaimTypeUpgradeRefusal(claim_type=claim_type.identity.qualified, reason=str(error))
            )
            continue
        changed[path] = render_claim_type(successor)
        upgraded.append(entry)
    refused.sort(key=lambda item: item.claim_type.encode("utf-8"))
    if not changed:
        return ClaimTypeUpgradeResult(
            status="unchanged",
            unchanged=tuple(unchanged),
            refused=tuple(refused),
            detail="No ClaimType to upgrade.",
            coordinate=mode.coordinate,
        )
    roots = tuple(parse_claim_type(tree[path], path=path).identity for path in sorted(changed))
    try:
        inventory = dependent_closure_inventory(tree, roots=roots, fixed_paths=frozenset(changed))
        settled, _normalized = build_dependent_closure_candidate(
            tree=tree,
            changed=changed,
            inventory=inventory,
            dispositions=tuple(
                ClaimTypeDependentDisposition(identity=item.identity, disposition="successor")
                for item in inventory
            ),
        )
    except ClaimTypeMigrationError as error:
        return ClaimTypeUpgradeResult(
            status="would_block" if mode.previewing else "blocked",
            upgraded=tuple(upgraded),
            unchanged=tuple(unchanged),
            refused=tuple(refused),
            detail=str(error),
            coordinate=mode.coordinate,
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
    suffix = canonical_digest(
        "playbill-claim-type-upgrade-target-v1",
        {
            "base": base.semantic_root,
            "members": sorted(changed),
            "revision_evidence": request.revision_evidence,
        },
    )
    admitted = admit_change_set(
        instance,
        mode,
        actor_id=actor_id,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/claim-type-v7-{suffix[:32]}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=candidate,
        timestamp=timestamp,
    )
    return ClaimTypeUpgradeResult(
        status=admitted.status,
        proposal_id=admitted.proposal_id,
        upgraded=tuple(upgraded),
        unchanged=tuple(unchanged),
        refused=tuple(refused),
        carried_claims=carried if admitted.admitted else 0,
        detail=detail if admitted.admitted else admitted.refusal_detail(),
        coordinate=mode.coordinate,
    )


__all__ = ["service_upgrade_claim_types"]
