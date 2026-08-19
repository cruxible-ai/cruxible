"""Deterministic multi-artifact dependency closure and exact refusal evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.acquisition_policies import (
    SourceAcquisitionPolicyError,
    acquisition_policy_digest,
    parse_acquisition_policy,
)
from cruxible_core.playbill.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.candidates import DependencyProofReferenceV1
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    CanonicalValue,
    DependencyEdgeRoot,
    Sha256Value,
    canonical_bytes,
    canonical_digest,
    normalize_manifest_paths,
    typed_digest,
)
from cruxible_core.playbill.captures import (
    CaptureFormatError,
    capture_contract_digest,
    parse_capture_contract,
)
from cruxible_core.playbill.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    parse_claim_type,
)
from cruxible_core.playbill.claims import (
    ClaimFormatError,
    claim_artifact_digest,
    parse_claim,
)
from cruxible_core.playbill.documents import (
    DocumentArtifactAdapter,
    document_digest,
    parse_document,
)
from cruxible_core.playbill.errors import DocumentFormatError, SubjectFormatError
from cruxible_core.playbill.exhaust.promotions import (
    ExhaustPromotionError,
    exhaust_promotion_digest,
    parse_exhaust_promotion,
)
from cruxible_core.playbill.merkle import (
    DEPENDENCY_EDGE_DOMAINS,
    MerkleTree,
    build_merkle_tree,
    update_merkle_tree,
    verify_merkle_tree,
)
from cruxible_core.playbill.procedures.artifacts import (
    ProcedureFormatError,
    parse_procedure,
    procedure_artifact_digest,
)
from cruxible_core.playbill.procedures.line_specs import (
    LineSpecFormatError,
    line_spec_digest,
    parse_line_spec,
)
from cruxible_core.playbill.providers import ProviderFormatError, parse_provider, provider_digest
from cruxible_core.playbill.query.definitions import (
    QueryDefinitionFormatError,
    parse_query_definition,
    query_definition_digest,
)
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.standing_mandates import (
    StandingMandateError,
    parse_standing_mandate,
    standing_mandate_digest,
)
from cruxible_core.playbill.subjects import (
    parse_subject,
    subject_digest,
)

# Bounded so the memo can never grow with history. One accepted tree at the
# pre-PC-G file-count posture fits several times over, which is what keeps the
# steady-state hit rate high, and every entry is dropped when the process exits.
_PARSE_MEMO_ENTRIES = 16_384


class _StrictClosureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactDependencyStateV1(_StrictClosureModel):
    path: str
    artifact_kind: Literal[
        "document",
        "subject",
        "claim-type",
        "capture-contract",
        "provider",
        "source-acquisition-policy",
        "standing-mandate",
        "claim",
        "procedure",
        "line",
        "query-definition",
        "exhaust-promotion",
    ]
    artifact_tag: str
    identity: ArtifactIdentity
    artifact_digest: str
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if tuple(normalize_manifest_paths((value,))) != (value,):
            raise ValueError("artifact dependency path must be canonical")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @property
    def address(self) -> SemanticAddress:
        return SemanticAddress.whole_artifact(self.path)


@lru_cache(maxsize=_PARSE_MEMO_ENTRIES)
def parse_dependency_artifact(path: str, content: bytes) -> ArtifactDependencyStateV1 | None:
    """Parse only artifact kinds participating in PC-A2 dependency closure.

    The result is a pure function of the exact path and the exact bytes -- every
    parser below reads nothing else -- and the state it returns is frozen, so a
    bounded content-addressed memo is observationally identical to reparsing and
    is safe to share between callers.

    The memo is load bearing rather than decorative. One proposal evaluation
    parses the parent tree and the candidate tree, then evaluates closure over
    both again, so an unmemoized evaluation re-derives every member of the whole
    tree four times for a change that touched a handful of them. Replay
    compounds that by the length of history while handing the identical `bytes`
    objects forward from generation to generation. Format failures are raised
    rather than memoized, so a malformed member is re-derived and re-refused on
    every look.
    """

    try:
        if path.startswith("documents/"):
            document = parse_document(content, path=path)
            adapter = DocumentArtifactAdapter(document)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="document",
                artifact_tag=document.tag,
                identity=adapter.identity,
                artifact_digest=document_digest(document).tagged,
                pins=adapter.pins,
                lifecycle=adapter.lifecycle,
            )
        if path.startswith("subjects/"):
            subject = parse_subject(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="subject",
                artifact_tag=subject.artifact_format,
                identity=subject.identity,
                artifact_digest=subject_digest(subject).tagged,
                pins=subject.pins,
                lifecycle=subject.lifecycle,
            )
        if path.startswith("claim-types/"):
            claim_type = parse_claim_type(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="claim-type",
                artifact_tag=claim_type.artifact_format,
                identity=claim_type.identity,
                artifact_digest=claim_type_digest(claim_type).tagged,
                pins=claim_type.pins,
                lifecycle=claim_type.lifecycle,
            )
        if path.startswith("capture-contracts/"):
            contract = parse_capture_contract(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="capture-contract",
                artifact_tag=contract.artifact_format,
                identity=contract.identity,
                artifact_digest=capture_contract_digest(contract).tagged,
                pins=contract.pins,
                lifecycle=contract.lifecycle,
            )
        if path.startswith("providers/"):
            provider = parse_provider(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="provider",
                artifact_tag=provider.artifact_format,
                identity=provider.identity,
                artifact_digest=provider_digest(provider).tagged,
                pins=provider.pins,
                lifecycle=provider.lifecycle,
            )
        if path.startswith("source-acquisition-policies/"):
            policy = parse_acquisition_policy(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="source-acquisition-policy",
                artifact_tag=policy.artifact_format,
                identity=policy.identity,
                artifact_digest=acquisition_policy_digest(policy).tagged,
                pins=policy.pins,
                lifecycle=policy.lifecycle,
            )
        if path.startswith("standing-mandates/"):
            mandate = parse_standing_mandate(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="standing-mandate",
                artifact_tag=mandate.artifact_format,
                identity=mandate.identity,
                artifact_digest=standing_mandate_digest(mandate).tagged,
                pins=mandate.pins,
                lifecycle=mandate.lifecycle,
            )
        if path.startswith("claims/"):
            claim = parse_claim(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="claim",
                artifact_tag=claim.artifact_format,
                identity=claim.identity,
                artifact_digest=claim_artifact_digest(claim).tagged,
                pins=claim.pins,
                lifecycle=claim.lifecycle,
            )
        if path.startswith("procedures/"):
            procedure = parse_procedure(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="procedure",
                artifact_tag=procedure.artifact_format,
                identity=procedure.identity,
                artifact_digest=procedure_artifact_digest(procedure).tagged,
                pins=procedure.pins,
                lifecycle=procedure.lifecycle,
            )
        if path.startswith("lines/"):
            line = parse_line_spec(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="line",
                artifact_tag=line.artifact_format,
                identity=line.identity,
                artifact_digest=line_spec_digest(line).tagged,
                pins=line.pins,
                lifecycle=line.lifecycle,
            )
        if path.startswith("query-definitions/"):
            query = parse_query_definition(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="query-definition",
                artifact_tag=query.artifact_format,
                identity=query.identity,
                artifact_digest=query_definition_digest(query).tagged,
                pins=query.pins,
                lifecycle=query.lifecycle,
            )
        if path.startswith("exhaust-promotions/"):
            promotion = parse_exhaust_promotion(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="exhaust-promotion",
                artifact_tag=promotion.artifact_format,
                identity=promotion.identity,
                artifact_digest=exhaust_promotion_digest(promotion),
                pins=promotion.pins,
                lifecycle=promotion.lifecycle,
            )
    except (
        CaptureFormatError,
        ClaimFormatError,
        DocumentFormatError,
        ProviderFormatError,
        SourceAcquisitionPolicyError,
        StandingMandateError,
        SubjectFormatError,
        ClaimTypeFormatError,
        ProcedureFormatError,
        LineSpecFormatError,
        QueryDefinitionFormatError,
        ExhaustPromotionError,
    ):
        raise
    return None


def dependency_artifacts(
    tree: Mapping[str, bytes],
) -> tuple[ArtifactDependencyStateV1, ...]:
    items = tuple(
        parsed
        for path in sorted(tree, key=lambda item: item.encode("utf-8"))
        if (parsed := parse_dependency_artifact(path, tree[path])) is not None
    )
    identities = tuple(item.identity.qualified for item in items)
    if len(identities) != len(set(identities)):
        raise ValueError("candidate tree contains a duplicate semantic artifact identity")
    return items


class UnresolvedArtifactPinV1(_StrictClosureModel):
    source_path: str
    source_artifact_digest: str
    target_identity: ArtifactIdentity
    expected_target_digest: str
    pin_role: str
    reason: Literal["missing_or_digest_mismatch", "live_source_targets_retired"]


class IncompleteClosureItemV1(_StrictClosureModel):
    path: str
    address: SemanticAddress
    current_artifact_digest: str
    triggering_dependency_path: str
    triggering_dependency_digest: str
    dependency_edge_role: str
    permitted_dispositions: tuple[Literal["successor", "retire", "invalidation"], ...]

    @field_validator("current_artifact_digest", "triggering_dependency_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class MemberDependencyProofsV1(_StrictClosureModel):
    path: str
    proof_refs: tuple[DependencyProofReferenceV1, ...]


DEPENDENCY_EDGE_SET_DOMAIN: Final = "playbill-depgraph-edges-v1"


def _edge_sort_key(edge: DependencyProofReferenceV1) -> bytes:
    return canonical_bytes(edge.model_dump(mode="json"))


def dependency_edge_members(
    edges: Sequence[DependencyProofReferenceV1],
) -> dict[str, str]:
    """Group edges by source member and digest each member's outgoing edge set.

    Only members that actually have outgoing edges become leaves, which is what
    keeps the tree proportional to the dependency structure rather than to the
    instance. Ordering inside a member is canonical and duplicates are preserved
    exactly as the flat `playbill-dependency-graph-v2` edge list preserved them,
    so the two commitments read the same edge set and only its shape differs.

    The source path is not repeated inside the member digest: every edge already
    carries `source_path`, and the trie leaf binds the path itself, exactly as
    the manifest merkle binds a member's path at its leaf rather than inside its
    content digest.
    """

    grouped: dict[str, list[CanonicalValue]] = {}
    for edge in sorted(edges, key=_edge_sort_key):
        grouped.setdefault(edge.source_path, []).append(edge.model_dump(mode="json"))
    return {
        path: canonical_digest(DEPENDENCY_EDGE_SET_DOMAIN, {"edges": entries})
        for path, entries in grouped.items()
    }


def build_dependency_edge_tree(
    edges: Sequence[DependencyProofReferenceV1],
) -> MerkleTree[DependencyEdgeRoot]:
    """Build the `playbill-dependency-graph-v3` trie over one tree's edge set."""

    return build_merkle_tree(dependency_edge_members(edges), domains=DEPENDENCY_EDGE_DOMAINS)


def update_dependency_edge_tree(
    tree: MerkleTree[DependencyEdgeRoot],
    *,
    updated: Mapping[str, Sequence[DependencyProofReferenceV1]] | None = None,
    removed: Sequence[str] | None = None,
) -> MerkleTree[DependencyEdgeRoot]:
    """Re-digest only the named members' edge sets and the nodes above them.

    `updated` maps a member path to its complete new outgoing edge set, and
    `removed` names members that left the tree entirely. A member with no
    outgoing edges has no leaf, so updating one to an empty edge set drops its
    leaf, and doing so to a member that never had one is the no-op it describes
    -- unlike `removed`, which still refuses to name a member that is not there.
    The result is the tree `build_dependency_edge_tree` would have built from the
    whole post-change edge set.
    """

    members: dict[str, str] = {}
    dropped = list(removed or ())
    for path, member_edges in (updated or {}).items():
        digests = dependency_edge_members(member_edges)
        if not digests:
            existing = tree.nodes.get(path)
            if existing is not None and existing.is_leaf:
                dropped.append(path)
            continue
        if set(digests) != {path}:
            raise ValueError(f"dependency edge update names edges outside {path!r}")
        members[path] = digests[path]
    return update_merkle_tree(tree, updated=members, removed=dropped)


def dependency_edge_root(
    edges: Sequence[DependencyProofReferenceV1],
) -> DependencyEdgeRoot:
    """Return the tagged v3 edge-set root, defined even for an edgeless tree."""

    return build_dependency_edge_tree(edges).root


def verify_dependency_edge_root(
    edges: Sequence[DependencyProofReferenceV1],
    *,
    claimed_root: str,
) -> MerkleTree[DependencyEdgeRoot]:
    """Rebuild the edge trie and refuse unless the claimed root reproduces."""

    return verify_merkle_tree(
        dependency_edge_members(edges),
        claimed_root=claimed_root,
        domains=DEPENDENCY_EDGE_DOMAINS,
    )


def _verify_closure_shape(evaluation: "ClosureEvaluationV2 | ClosureEvaluationV3") -> None:
    """Check the verdict/evidence/scope agreement both closure versions share."""

    refused = bool(evaluation.missing_dependents or evaluation.unresolved_pins)
    if (evaluation.verdict == "refused") != refused:
        raise ValueError("closure verdict must agree with exact refusal evidence")
    if tuple(item.path for item in evaluation.member_dependency_proofs) != evaluation.paths:
        raise ValueError("closure member proof paths differ from complete scope")


def _proofs_for(
    member_dependency_proofs: tuple[MemberDependencyProofsV1, ...],
    path: str,
) -> tuple[DependencyProofReferenceV1, ...]:
    for item in member_dependency_proofs:
        if item.path == path:
            return item.proof_refs
    raise KeyError(path)


class ClosureEvaluationV2(_StrictClosureModel):
    tag: Literal["playbill-closure-evaluation-v2"] = "playbill-closure-evaluation-v2"
    verdict: Literal["complete", "refused"]
    paths: tuple[str, ...]
    dependency_graph_digest: str
    member_dependency_proofs: tuple[MemberDependencyProofsV1, ...]
    missing_dependents: tuple[IncompleteClosureItemV1, ...] = ()
    unresolved_pins: tuple[UnresolvedArtifactPinV1, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "ClosureEvaluationV2":
        _verify_closure_shape(self)
        return self

    def proofs_for(self, path: str) -> tuple[DependencyProofReferenceV1, ...]:
        return _proofs_for(self.member_dependency_proofs, path)


class ClosureEvaluationV3(_StrictClosureModel):
    """The v2 closure evaluation with the incrementally maintainable edge root.

    Verdict, scope, per-member proofs, and both refusal shapes are unchanged:
    only the graph commitment moves, from a digest over both trees' full edge
    lists to a merkle root over the candidate tree's edge set.
    """

    tag: Literal["playbill-closure-evaluation-v3"] = "playbill-closure-evaluation-v3"
    verdict: Literal["complete", "refused"]
    paths: tuple[str, ...]
    dependency_edge_root: str
    member_dependency_proofs: tuple[MemberDependencyProofsV1, ...]
    missing_dependents: tuple[IncompleteClosureItemV1, ...] = ()
    unresolved_pins: tuple[UnresolvedArtifactPinV1, ...] = ()

    @field_validator("dependency_edge_root")
    @classmethod
    def _edge_root(cls, value: str) -> str:
        DependencyEdgeRoot.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ClosureEvaluationV3":
        _verify_closure_shape(self)
        return self

    def proofs_for(self, path: str) -> tuple[DependencyProofReferenceV1, ...]:
        return _proofs_for(self.member_dependency_proofs, path)


def _edges(
    artifacts: tuple[ArtifactDependencyStateV1, ...],
) -> tuple[DependencyProofReferenceV1, ...]:
    by_identity = {item.identity.qualified: item for item in artifacts}
    edges: list[DependencyProofReferenceV1] = []
    for source in artifacts:
        for pin in source.pins:
            target = by_identity.get(pin.target.qualified)
            if target is None or target.artifact_digest != pin.artifact_digest:
                continue
            edges.append(
                DependencyProofReferenceV1(
                    source_path=source.path,
                    source_artifact_digest=source.artifact_digest,
                    target_path=target.path,
                    target_artifact_digest=target.artifact_digest,
                    pin_role=pin.role,
                )
            )
    return tuple(sorted(edges, key=lambda item: canonical_bytes(item.model_dump(mode="json"))))


def _unresolved_pins(
    artifacts: tuple[ArtifactDependencyStateV1, ...],
    *,
    source_paths: set[str],
) -> tuple[UnresolvedArtifactPinV1, ...]:
    by_identity = {item.identity.qualified: item for item in artifacts}
    missing: list[UnresolvedArtifactPinV1] = []
    for source in artifacts:
        if source.path not in source_paths:
            continue
        for pin in source.pins:
            # These component families are exact compiler/policy-registry pins,
            # or content-addressed receipt manifests, until a later batch gives
            # them ledger artifact envelopes. Their owning law verifies the
            # role-named digest; this exception is never a name lookup and never
            # permits a missing Playbill artifact kind such as Procedure,
            # ClaimType, or Provider.
            #
            # A QueryDefinition ledger envelope exists from PC-F slice 1, so a
            # QueryDefinition's own ClaimType pins already close exactly here.
            # Referent-side resolution of a Procedure/LineSpec role='query' pin
            # stays deferred to the PC-F engine slice that re-authors those
            # Procedures against real accepted QueryDefinitions; resolving it
            # earlier would only refuse placeholder pins that no accepted query
            # yet backs.
            if pin.target.kind in {
                "Contract",
                "EffectPolicy",
                "EnvironmentManifest",
                "ExhaustReducer",
                "LandingFilter",
                "Policy",
                "QueryDefinition",
                "ReceiptSetManifest",
                "Reducer",
            }:
                continue
            target = by_identity.get(pin.target.qualified)
            reason: Literal["missing_or_digest_mismatch", "live_source_targets_retired"] | None = (
                None
            )
            if target is None or target.artifact_digest != pin.artifact_digest:
                reason = "missing_or_digest_mismatch"
            elif source.lifecycle.state == "live" and target.lifecycle.state == "retired":
                reason = "live_source_targets_retired"
            if reason is not None:
                missing.append(
                    UnresolvedArtifactPinV1(
                        source_path=source.path,
                        source_artifact_digest=source.artifact_digest,
                        target_identity=pin.target,
                        expected_target_digest=pin.artifact_digest,
                        pin_role=pin.role,
                        reason=reason,
                    )
                )
    return tuple(sorted(missing, key=lambda item: canonical_bytes(item.model_dump(mode="json"))))


@dataclass(frozen=True)
class _ClosureFacts:
    """Everything one closure evaluation determines, before any version speaks.

    The reverse-dependency walk, the pin resolution, and the per-member proof
    assembly are one algorithm; v2 and v3 differ only in how they commit to the
    edges it read. Keeping the facts in one place is what makes the succession a
    change of commitment rather than a second closure implementation.
    """

    scope: tuple[str, ...]
    parent_edges: tuple[DependencyProofReferenceV1, ...]
    candidate_edges: tuple[DependencyProofReferenceV1, ...]
    missing_dependents: tuple[IncompleteClosureItemV1, ...]
    unresolved_pins: tuple[UnresolvedArtifactPinV1, ...]
    member_dependency_proofs: tuple[MemberDependencyProofsV1, ...]

    @property
    def verdict(self) -> Literal["complete", "refused"]:
        return "refused" if self.missing_dependents or self.unresolved_pins else "complete"


def _closure_facts(
    *,
    parent_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> _ClosureFacts:
    """Compute exact reverse dependencies and every omitted live dependent."""

    if tuple(normalize_manifest_paths(scope)) != scope or not scope:
        raise ValueError("closure scope must be nonempty, sorted, and unique")
    parent = dependency_artifacts(parent_tree)
    candidate = dependency_artifacts(candidate_tree)
    parent_by_path = {item.path: item for item in parent}
    parent_edges = _edges(parent)
    candidate_edges = _edges(candidate)
    scope_set = set(scope)
    missing: list[IncompleteClosureItemV1] = []
    for changed_path in scope:
        changed = parent_by_path.get(changed_path)
        if changed is None:
            continue
        for edge in parent_edges:
            if edge.target_path != changed_path or edge.source_path in scope_set:
                continue
            dependent = parent_by_path[edge.source_path]
            if dependent.lifecycle.state != "live":
                continue
            missing.append(
                IncompleteClosureItemV1(
                    path=dependent.path,
                    address=dependent.address,
                    current_artifact_digest=dependent.artifact_digest,
                    triggering_dependency_path=changed.path,
                    triggering_dependency_digest=changed.artifact_digest,
                    dependency_edge_role=edge.pin_role,
                    permitted_dispositions=("invalidation", "retire", "successor"),
                )
            )
    missing_tuple = tuple(
        sorted(missing, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
    )
    unresolved = _unresolved_pins(candidate, source_paths=scope_set)
    proofs: list[MemberDependencyProofsV1] = []
    all_edges = tuple(
        sorted(
            {*parent_edges, *candidate_edges},
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    for path in scope:
        related = tuple(
            edge for edge in all_edges if edge.source_path == path or edge.target_path == path
        )
        proofs.append(MemberDependencyProofsV1(path=path, proof_refs=related))
    return _ClosureFacts(
        scope=scope,
        parent_edges=parent_edges,
        candidate_edges=candidate_edges,
        missing_dependents=missing_tuple,
        unresolved_pins=unresolved,
        member_dependency_proofs=tuple(proofs),
    )


def evaluate_dependency_closure(
    *,
    parent_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> ClosureEvaluationV2:
    """Evaluate closure and commit to both trees' full edge lists in one digest."""

    facts = _closure_facts(
        parent_tree=parent_tree,
        candidate_tree=candidate_tree,
        scope=scope,
    )
    graph_digest = typed_digest(
        Sha256Value,
        "playbill-dependency-graph-v2",
        {
            "parent_edges": [item.model_dump(mode="json") for item in facts.parent_edges],
            "candidate_edges": [item.model_dump(mode="json") for item in facts.candidate_edges],
        },
    ).tagged
    return ClosureEvaluationV2(
        verdict=facts.verdict,
        paths=facts.scope,
        dependency_graph_digest=graph_digest,
        member_dependency_proofs=facts.member_dependency_proofs,
        missing_dependents=facts.missing_dependents,
        unresolved_pins=facts.unresolved_pins,
    )


def evaluate_dependency_closure_v3(
    *,
    parent_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> ClosureEvaluationV3:
    """Evaluate the same closure and commit to the candidate tree's edge root.

    Nothing calls this yet: the v3 format and its verifier land before any
    producer adopts them.
    """

    facts = _closure_facts(
        parent_tree=parent_tree,
        candidate_tree=candidate_tree,
        scope=scope,
    )
    return ClosureEvaluationV3(
        verdict=facts.verdict,
        paths=facts.scope,
        dependency_edge_root=dependency_edge_root(facts.candidate_edges).tagged,
        member_dependency_proofs=facts.member_dependency_proofs,
        missing_dependents=facts.missing_dependents,
        unresolved_pins=facts.unresolved_pins,
    )


__all__ = [
    "ArtifactDependencyStateV1",
    "ClosureEvaluationV2",
    "ClosureEvaluationV3",
    "DEPENDENCY_EDGE_SET_DOMAIN",
    "IncompleteClosureItemV1",
    "MemberDependencyProofsV1",
    "UnresolvedArtifactPinV1",
    "build_dependency_edge_tree",
    "dependency_artifacts",
    "dependency_edge_members",
    "dependency_edge_root",
    "evaluate_dependency_closure",
    "evaluate_dependency_closure_v3",
    "parse_dependency_artifact",
    "update_dependency_edge_tree",
    "verify_dependency_edge_root",
]
