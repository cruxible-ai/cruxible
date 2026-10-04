"""Move accepted ClaimTypes to identity evidence rules, one reviewed change set.

Compiler revision 31 admits ClaimType v6, whose evidence rules name
CaptureContracts by identity. This builds the ordinary change set that moves
every live ClaimType whose evidence rules name contracts by exact digest (the
v1 through v5 formats) to v6 and carries its Claims, and proposes it; nothing
is activated here.

A converted rule admits evidence captured under every accepted version of the
contracts it names. That must never widen admission silently, so each rule is
converted only when it provably means the same thing, and anything else is left
out and reported for an explicit decision:

- every version of a named contract must be compatible with its predecessor
  (a lineage with a breaking step cannot be admitted as one identity);
- versions the rule did not name before are listed as the disclosed widening;
- two rules that did not overlap before must not start matching the same
  evidence, because admission refuses evidence that two rules match.
"""

from __future__ import annotations

from cruxible_client.contracts.artifacts import ArtifactLifecycle, ArtifactRef
from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    capture_contract_successor_break,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.evidence_rule_upgrade import (
    EvidenceRuleConversion,
    EvidenceRuleRefusal,
    EvidenceRuleUpgradeRequest,
    EvidenceRuleUpgradeResult,
)
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimEvidenceAdmissionRuleV1,
    ClaimEvidenceAdmissionRuleV2,
)
from cruxible_core.claims.claim_type_inputs import identity_rules_supported
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.errors import DataValidationError
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import ChangeMode, admit_change_set, change_scope


class _Refused(Exception):
    pass


class _Lineages:
    """Every accepted version of each contract identity, read once from history."""

    def __init__(self, instance: PlaybillInstance, at: AcceptedCoordinate) -> None:
        self._instance = instance
        self._at = at
        self._by_digest: dict[str, AcceptedCaptureContract] = {}
        self._lineage: dict[str, tuple[AcceptedCaptureContract, ...]] = {}

    def version(self, digest: str) -> AcceptedCaptureContract:
        found = self._by_digest.get(digest)
        if found is None:
            found = self._instance.accepted_capture_contract_version(self._at, digest)
            if found is None:
                raise _Refused(f"names {digest}, which is not an accepted CaptureContract")
            self._by_digest[digest] = found
        return found

    def lineage(self, identity: str) -> tuple[AcceptedCaptureContract, ...]:
        cached = self._lineage.get(identity)
        if cached is not None:
            return cached
        with self._instance.accepted_history_reader(at=self._at) as history:
            occurrences = history.occurrences(identity)
        versions: list[AcceptedCaptureContract] = []
        for location in occurrences:
            if any(item.artifact_digest == location.artifact_digest for item in versions):
                continue
            versions.append(self.version(location.artifact_digest))
        by_digest = {item.artifact_digest: item for item in versions}
        for item in versions:
            predecessor_digest = item.contract.lifecycle.predecessor_digest
            if predecessor_digest is None:
                continue
            previous = by_digest.get(predecessor_digest)
            if previous is None:
                raise _Refused(f"{identity} has a version whose predecessor is not accepted")
            if item.contract.lifecycle.state == "retired":
                raise _Refused(f"{identity} has been retired; it cannot be named by identity")
            if previous.contract.lifecycle.state == "retired":
                raise _Refused(f"{identity} was revived after retirement")
            broken = capture_contract_successor_break(previous.contract, item.contract)
            if broken is not None:
                raise _Refused(
                    f"{identity} changed {broken!r} between versions; evidence under its "
                    "versions is not interchangeable, so a rule cannot name it by identity"
                )
        self._lineage[identity] = tuple(versions)
        return self._lineage[identity]


def _overlaps(first: ClaimEvidenceAdmissionRule, second: ClaimEvidenceAdmissionRule) -> bool:
    return (
        bool(set(first.claim_roles) & set(second.claim_roles))
        and bool(set(first.evidence_kinds) & set(second.evidence_kinds))
        and bool(
            {item.target.qualified for item in first.capture_contracts}
            & {item.target.qualified for item in second.capture_contracts}
        )
    )


def _convert(
    claim_type: ClaimType, lineages: _Lineages
) -> tuple[ClaimType, EvidenceRuleConversion]:
    if any(pin.target.kind == "CaptureContract" for pin in claim_type.pins):
        raise _Refused("pins a CaptureContract exactly; remove the pin first")
    rules: list[ClaimEvidenceAdmissionRule] = []
    before: list[ClaimEvidenceAdmissionRuleV1 | ClaimEvidenceAdmissionRuleV2] = []
    widened: set[str] = set()
    for rule in claim_type.evidence_admission_policy.rules:
        if not isinstance(rule, ClaimEvidenceAdmissionRuleV1 | ClaimEvidenceAdmissionRuleV2):
            raise _Refused("already names contracts by identity")
        if getattr(rule, "allowed_reducer_digests", ()):
            raise _Refused(
                f"rule {rule.rule_id!r} authorizes producer reducers, which identity rules "
                "cannot express; producer authorization belongs to Procedure mandates"
            )
        before.append(rule)
        named: dict[str, set[str]] = {}
        for digest in rule.capture_contract_digests:
            version = lineages.version(digest)
            named.setdefault(version.contract.identity.qualified, set()).add(digest)
        for identity, digests in named.items():
            admitted = {item.artifact_digest for item in lineages.lineage(identity)}
            widened.update(f"{identity}@{digest}" for digest in admitted - digests)
        rules.append(
            ClaimEvidenceAdmissionRule(
                rule_id=rule.rule_id,
                claim_roles=rule.claim_roles,
                evidence_kinds=rule.evidence_kinds,
                admission=rule.admission,
                subject_binding=rule.subject_binding,
                attestation_requirement=rule.attestation_requirement,
                capture_contracts=tuple(
                    ArtifactRef(
                        role=CAPTURE_CONTRACT_REF_ROLE,
                        target=lineages.lineage(identity)[0].contract.identity,
                    )
                    for identity in sorted(named, key=lambda item: item.encode("utf-8"))
                ),
            )
        )
    for index, first in enumerate(rules):
        for offset, second in enumerate(rules[index + 1 :], start=index + 1):
            if not _overlaps(first, second):
                continue
            # Evidence two rules both match is refused, so conversion may only keep
            # ambiguity that already existed: every version of a shared contract
            # must already have been named by both exact rules.
            already = set(before[index].capture_contract_digests) & set(
                before[offset].capture_contract_digests
            )
            shared = {item.target.qualified for item in first.capture_contracts} & {
                item.target.qualified for item in second.capture_contracts
            }
            newly = sorted(
                f"{identity}@{version.artifact_digest}"
                for identity in shared
                for version in lineages.lineage(identity)
                if version.artifact_digest not in already
            )
            if newly:
                raise _Refused(
                    f"rules {first.rule_id!r} and {second.rule_id!r} would both match the same "
                    f"evidence once they name contracts by identity ({', '.join(newly)}); "
                    "merge them first"
                )
    payload = claim_type.model_dump(mode="python")
    payload.update(
        artifact_format="playbill-claim-type-v6",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=tuple(rules)),
        lifecycle=ArtifactLifecycle(predecessor_digest=claim_type_digest(claim_type).tagged),
    )
    return ClaimType.model_validate(payload), EvidenceRuleConversion(
        claim_type=claim_type.identity.qualified,
        widened_versions=tuple(sorted(widened, key=lambda item: item.encode("utf-8"))),
    )


def service_upgrade_evidence_rules(
    instance: PlaybillInstance,
    *,
    request: EvidenceRuleUpgradeRequest,
    actor_id: str,
    timestamp: str,
) -> EvidenceRuleUpgradeResult:
    """Propose (or preview) the change set moving every convertible exact-rule ClaimType to v6.

    The change set carries every dependent Claim, so it previews unless
    ``dry_run`` is false; the preview evaluates it on the proposal service's own
    admission path, under the same receive limits a submission meets, and
    answers with counts and per-ClaimType entries rather than the Claims.
    """

    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="playbill.claim-type.upgrade-evidence-rules",
        describe="the evidence-rule upgrade",
    ) as mode:
        return _upgrade(instance, mode, actor_id=actor_id, timestamp=timestamp)


def _upgrade(
    instance: PlaybillInstance, mode: ChangeMode, *, actor_id: str, timestamp: str
) -> EvidenceRuleUpgradeResult:
    assert mode.head is not None
    base = mode.head
    if not identity_rules_supported(base.compiler):
        raise DataValidationError(
            "identity evidence rules need compiler revision 31 or later; upgrade the compiler first"
        )
    tree = instance.immutable_tree_at(base.git_oid)
    lineages = _Lineages(instance, AcceptedCoordinate.from_internal(base))
    changed: dict[str, bytes] = {}
    converted: list[EvidenceRuleConversion] = []
    refused: list[EvidenceRuleRefusal] = []
    for path in sorted(item for item in tree if item.startswith("claim-types/")):
        claim_type = parse_claim_type(tree[path], path=path)
        if claim_type.artifact_format in {"playbill-claim-type-v6", "playbill-claim-type-v7"}:
            continue
        if claim_type.lifecycle.state != "live":
            continue
        try:
            successor, conversion = _convert(claim_type, lineages)
        except (_Refused, FormatError, ValueError) as error:
            refused.append(
                EvidenceRuleRefusal(claim_type=claim_type.identity.qualified, reason=str(error))
            )
            continue
        changed[path] = render_claim_type(successor)
        converted.append(conversion)
    if not changed:
        return EvidenceRuleUpgradeResult(
            status="unchanged",
            refused=tuple(refused),
            detail="No ClaimType to convert.",
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
        return EvidenceRuleUpgradeResult(
            status="would_block" if mode.previewing else "blocked",
            converted=tuple(converted),
            refused=tuple(refused),
            detail=str(error),
            coordinate=mode.coordinate,
        )
    candidate = tree.fork()
    for path, content in {**settled, **changed}.items():
        candidate[path] = content
    suffix = canonical_digest(
        "playbill-evidence-rule-upgrade-target-v1",
        {"base": base.semantic_root, "members": sorted(changed)},
    )
    admitted = admit_change_set(
        instance,
        mode,
        actor_id=actor_id,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/evidence-rules-{suffix[:32]}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=candidate,
        timestamp=timestamp,
    )
    if not admitted.admitted:
        return EvidenceRuleUpgradeResult(
            status=admitted.status,
            proposal_id=admitted.proposal_id,
            converted=tuple(converted),
            refused=tuple(refused),
            detail=admitted.refusal_detail(),
            coordinate=mode.coordinate,
        )
    return EvidenceRuleUpgradeResult(
        status=admitted.status,
        proposal_id=admitted.proposal_id,
        converted=tuple(converted),
        refused=tuple(refused),
        carried_claims=sum(1 for item in inventory if item.path.startswith("claims/")),
        detail=(
            "Each converted rule now admits evidence under every compatible version of the "
            "contracts it names, including future successors; widened_versions lists the "
            "accepted versions it admits that it did not before."
        ),
        coordinate=mode.coordinate,
    )


__all__ = ["service_upgrade_evidence_rules"]
