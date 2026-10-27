"""Move accepted ClaimTypes to ClaimType v7, one reviewed change set.

ClaimTypes before v7 never switch on their own: one accepted ClaimType digest
keeps one revision rule. This builds the ordinary change set that moves the
chosen live ClaimTypes to v7 and carries their Claims, and proposes it; nothing
is activated here.

The move is mechanical and keeps meaning, with one disclosed exception:

- a v6 ClaimType becomes v7 with ``evidence_requirement="self"`` (its meaning),
  no default role and no descriptions;
- a v1-v5 ClaimType first takes the identity-rule conversion (with its
  disclosed ``widened_versions``), in the same change set;
- ``revision_evidence`` is the one meaning that may change: every ClaimType
  before v7 accumulates, and the upgrade states ``replace`` unless asked for
  ``accumulate``. The result lists that flip per ClaimType.

On compiler revision 36, which writes only v4 identity evidence policies, the
same change set also restates each chosen live ClaimType's v3 policy as v4,
whatever its format (``restated``). The restatement admits exactly what the v3
policy admitted, or the ClaimType is refused and left as it is. Below revision
36, v3 is the format a write states, so nothing is restated.

Every dependent Claim is carried with byte-identical backing, so no Claim loses
evidence by the move: only its later statement-changing revisions follow the new
rule.
"""

from __future__ import annotations

from typing import Literal, cast

from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.captures import CaptureContractV1
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypePolicyRestatement,
    ClaimTypeUpgrade,
    ClaimTypeUpgradeRefusal,
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.claim_types import (
    V7_AND_LATER_FORMATS,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.policies import (
    CaptureContractAdmission,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV3,
    ClaimEvidenceAdmissionRule,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.compiler.compiler import (
    AUTHORITY_VERBS_COMPILER,
    CLAIM_REBIND_COMPILER,
    GOVERNED_TRIGGERS_COMPILER,
    LIGHT_ARTIFACTS_COMPILER,
    PROVIDER_BUILDS_COMPILER,
    SUBJECT_IDENTITY_COMPILER,
)
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


class _Inexact(Exception):
    """A v3 policy whose admission v4 cannot state exactly."""


def _restated_policy(
    policy: ClaimEvidenceAdmissionPolicyV3, lineages: _Lineages
) -> ClaimEvidenceAdmissionPolicy:
    """The v4 policy admitting exactly the evidence a v3 identity policy admits.

    Both rule versions name contracts by identity and share every requirement
    field, which carries over unchanged. They differ in how a source matches: a
    v3 rule matches the legacy evidence kinds a CaptureContract v1 version
    captures, a v4 rule matches logical sources and is stated here with none,
    which restricts nothing (a v3 rule restricts no source). So the two are the
    same rule exactly when the kind restriction never bites: every accepted v1
    version of each named contract captures only kinds the rule admits. A later
    version is a v3 contract (the one format revision 36 writes), which has no
    kind for a v3 rule to restrict.
    """

    rules: list[ClaimEvidenceAdmissionRule] = []
    for rule in policy.rules:
        admitted = set(rule.evidence_kinds)
        for ref in rule.capture_contracts:
            for version in lineages.versions(ref.target.qualified):
                contract = version.contract
                if not isinstance(contract, CaptureContractV1):
                    continue
                missing = sorted(set(contract.evidence_kinds) - admitted)
                if missing:
                    raise _Inexact(
                        f"rule {rule.rule_id!r} admits only evidence kinds "
                        f"{', '.join(sorted(admitted))} of {ref.target.qualified}, whose "
                        f"version {version.artifact_digest} also captures {', '.join(missing)}; "
                        "a v4 rule cannot restrict by evidence kind, so state the v4 "
                        "policy explicitly with `cruxible claim-type propose`"
                    )
        rules.append(
            ClaimEvidenceAdmissionRule(
                rule_id=rule.rule_id,
                claim_roles=rule.claim_roles,
                admission=rule.admission,
                subject_binding=rule.subject_binding,
                attestation_requirement=rule.attestation_requirement,
                capture_contracts=tuple(
                    CaptureContractAdmission(role=ref.role, target=ref.target)
                    for ref in rule.capture_contracts
                ),
            )
        )
    return ClaimEvidenceAdmissionPolicy(rules=tuple(rules))


def _restate(
    original: ClaimType, successor: ClaimType | None, lineages: _Lineages
) -> ClaimType | None:
    """``successor`` (or ``original``) with its v3 policy restated, or None if none."""

    current = successor or original
    policy = current.evidence_admission_policy
    if not isinstance(policy, ClaimEvidenceAdmissionPolicyV3):
        return None
    payload = current.model_dump(mode="python")
    payload.update(
        evidence_admission_policy=_restated_policy(policy, lineages),
        lifecycle=current.lifecycle.model_copy(
            update={"predecessor_digest": claim_type_digest(original).tagged}
        ),
    )
    return ClaimType.model_validate(payload)


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
        operation="cruxible.claim-type.upgrade",
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
    if base.compiler not in (
        AUTHORITY_VERBS_COMPILER,
        GOVERNED_TRIGGERS_COMPILER,
        SUBJECT_IDENTITY_COMPILER,
        PROVIDER_BUILDS_COMPILER,
        CLAIM_REBIND_COMPILER,
        LIGHT_ARTIFACTS_COMPILER,
    ):
        raise DataValidationError(
            "ClaimType v7 needs compiler revision 31 or later; upgrade it first"
        )
    tree = instance.immutable_tree_at(base.git_oid)
    lineages = _Lineages(instance, AcceptedCoordinate.from_internal(base))
    wanted = set(request.claim_types)
    # Revision 36 writes only v4 identity evidence policies; below it, v3.
    restating = base.compiler == LIGHT_ARTIFACTS_COMPILER
    changed: dict[str, bytes] = {}
    upgraded: list[ClaimTypeUpgrade] = []
    restated: list[ClaimTypePolicyRestatement] = []
    unchanged: list[str] = []
    refused: list[ClaimTypeUpgradeRefusal] = []
    for predicate in sorted(wanted, key=lambda item: item.encode("utf-8")):
        try:
            present = claim_type_path(predicate) in tree
        except FormatError:
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
        moved: tuple[ClaimType, ClaimTypeUpgrade] | None = None
        if claim_type.artifact_format not in V7_AND_LATER_FORMATS:
            try:
                moved = _to_v7(claim_type, lineages, request)
            except (_Refused, FormatError, ValueError) as error:
                refused.append(
                    ClaimTypeUpgradeRefusal(
                        claim_type=claim_type.identity.qualified, reason=str(error)
                    )
                )
                continue
        successor = None if moved is None else moved[0]
        if restating:
            try:
                restatement = _restate(claim_type, successor, lineages)
            except (_Inexact, _Refused) as error:
                refused.append(
                    ClaimTypeUpgradeRefusal(
                        claim_type=claim_type.identity.qualified,
                        reason=str(error),
                        code="cruxible.claim_type.policy_restatement_inexact",
                    )
                )
                continue
            if restatement is not None:
                successor = restatement
                restated.append(
                    ClaimTypePolicyRestatement(claim_type=claim_type.identity.qualified)
                )
        if successor is None:
            unchanged.append(claim_type.identity.qualified)
            continue
        changed[path] = render_claim_type(successor)
        if moved is not None:
            upgraded.append(moved[1])
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
        inventory = dependent_closure_inventory(
            tree,
            roots=roots,
            fixed_paths=frozenset(changed),
            by_name=base.compiler == LIGHT_ARTIFACTS_COMPILER,
        )
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
            restated=tuple(restated),
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
    sentences = []
    if flips:
        sentences.append(
            f"{len(flips)} ClaimType(s) move from accumulate to replace: a revision that "
            "changes its statement then carries exactly the evidence it cites."
        )
    elif upgraded:
        sentences.append("Every upgraded ClaimType keeps accumulating evidence across revisions.")
    if restated:
        sentences.append(
            f"{len(restated)} ClaimType(s) restate their v3 evidence policy as v4, which "
            "admits exactly the same evidence."
        )
    sentences.append("Carried Claims keep all of their backing.")
    detail = " ".join(sentences)
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
        restated=tuple(restated),
        unchanged=tuple(unchanged),
        refused=tuple(refused),
        carried_claims=carried if admitted.admitted else 0,
        detail=detail if admitted.admitted else admitted.refusal_detail(),
        coordinate=mode.coordinate,
    )


__all__ = ["service_upgrade_claim_types"]
