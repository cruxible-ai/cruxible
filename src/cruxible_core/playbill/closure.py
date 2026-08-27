"""Deterministic multi-artifact dependency closure and exact refusal evidence.

Closure is two separable jobs, and this module keeps them separate: *maintaining
the indexes* one evaluation reads about a tree, and *judging* a scope against a
pair of them. The judgement is a pure function of the two indexes and the scope,
so it cannot tell how they were obtained; the indexes can therefore be built
from scratch over a whole tree, or carried forward from the parent generation
and updated for only the members that changed, and the verdict, both refusal
shapes, the per-member proofs, and the committed edge root are identical either
way. The from-scratch build stays the differential oracle the incremental
maintenance is tested against, and stays the only path a cold start can take.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.acquisition_policies import (
    SourceAcquisitionPolicyError,
    acquisition_policy_digest,
    parse_acquisition_policy,
)
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
    parse_artifact_identity,
)
from cruxible_client.contracts.candidates import DependencyProofReferenceV1
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CanonicalValue,
    DependencyEdgeRoot,
    Sha256Value,
    canonical_bytes,
    canonical_digest,
    normalize_manifest_paths,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    CaptureFormatError,
    capture_contract_digest,
    parse_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimFormatError,
    claim_artifact_digest,
    parse_claim,
)
from cruxible_client.contracts.documents import (
    DocumentArtifactAdapter,
    document_digest,
    parse_document,
)
from cruxible_client.contracts.errors import DocumentFormatError, SubjectFormatError
from cruxible_client.contracts.merkle import (
    DEPENDENCY_EDGE_DOMAINS,
    MerkleTree,
    build_merkle_tree,
    update_merkle_tree,
    verify_merkle_tree,
)
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureFormatError,
    parse_procedure,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.line_specs import (
    LineSpecFormatError,
    line_spec_digest,
    parse_line_spec,
)
from cruxible_client.contracts.providers import ProviderFormatError, parse_provider, provider_digest
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionFormatError,
    parse_query_definition,
    query_definition_digest,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.standing_mandates import (
    StandingMandateError,
    parse_standing_mandate,
    standing_mandate_digest,
)
from cruxible_client.contracts.subjects import (
    parse_subject,
    subject_digest,
)
from cruxible_core.playbill.exhaust.promotions import (
    ExhaustPromotionError,
    exhaust_promotion_digest,
    parse_exhaust_promotion,
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


DEFERRED_PIN_TARGET_KINDS: Final = frozenset(
    {
        "Contract",
        "EffectPolicy",
        "EnvironmentManifest",
        "ExhaustReducer",
        "LandingFilter",
        "Policy",
        "QueryDefinition",
        "ReceiptSetManifest",
        "Reducer",
    }
)
"""Pin target kinds whose referent has no ledger artifact envelope yet.

These component families are exact compiler/policy-registry pins, or
content-addressed receipt manifests, until a later batch gives them ledger
artifact envelopes. Their owning law verifies the role-named digest; this
exception is never a name lookup and never permits a missing Playbill artifact
kind such as Procedure, ClaimType, or Provider.

A QueryDefinition ledger envelope exists from PC-F slice 1, so a
QueryDefinition's own ClaimType pins already close exactly here. Referent-side
resolution of a Procedure/LineSpec role='query' pin stays deferred to the PC-F
engine slice that re-authors those Procedures against real accepted
QueryDefinitions; resolving it earlier would only refuse placeholder pins that
no accepted query yet backs.
"""


def _edge_order(edge: DependencyProofReferenceV1) -> bytes:
    return canonical_bytes(edge.model_dump(mode="json"))


def _sorted_edges(
    edges: Iterable[DependencyProofReferenceV1],
) -> tuple[DependencyProofReferenceV1, ...]:
    return tuple(sorted(edges, key=_edge_order))


def _outgoing_edges(
    source: ArtifactDependencyStateV1,
    *,
    states: Mapping[str, ArtifactDependencyStateV1],
    paths_by_identity: Mapping[str, str],
) -> tuple[DependencyProofReferenceV1, ...]:
    """Resolve one member's outgoing edges against the tree it belongs to."""

    edges: list[DependencyProofReferenceV1] = []
    for pin in source.pins:
        target_path = paths_by_identity.get(pin.target.qualified)
        target = None if target_path is None else states[target_path]
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
    return _sorted_edges(edges)


def _edges(
    artifacts: tuple[ArtifactDependencyStateV1, ...],
) -> tuple[DependencyProofReferenceV1, ...]:
    """Resolve one whole tree's edge set from scratch: the differential oracle."""

    states = {item.path: item for item in artifacts}
    paths_by_identity = {item.identity.qualified: item.path for item in artifacts}
    return _sorted_edges(
        edge
        for source in artifacts
        for edge in _outgoing_edges(
            source,
            states=states,
            paths_by_identity=paths_by_identity,
        )
    )


def _unresolved_pins_for(
    source: ArtifactDependencyStateV1,
    *,
    states: Mapping[str, ArtifactDependencyStateV1],
    paths_by_identity: Mapping[str, str],
) -> tuple[UnresolvedArtifactPinV1, ...]:
    """Classify one scoped member's own pins; reads nothing outside its pin list."""

    missing: list[UnresolvedArtifactPinV1] = []
    for pin in source.pins:
        if pin.target.kind in DEFERRED_PIN_TARGET_KINDS:
            continue
        target_path = paths_by_identity.get(pin.target.qualified)
        target = None if target_path is None else states[target_path]
        reason: Literal["missing_or_digest_mismatch", "live_source_targets_retired"] | None = None
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
    return tuple(missing)


_DUPLICATE_IDENTITY = "candidate tree contains a duplicate semantic artifact identity"


@dataclass(frozen=True)
class DependencyIndexV1:
    """Everything closure judging reads about one tree, in maintainable form.

    Nothing here is a commitment and nothing here is believed: every field is
    derived from the exact member bytes of the tree it describes, either by the
    from-scratch build or by applying one change set to an index that was. The
    reverse maps exist so that a judgement whose scope names a handful of members
    reads a handful of entries instead of walking the whole edge set.
    """

    states: Mapping[str, ArtifactDependencyStateV1]
    paths_by_identity: Mapping[str, str]
    sources_by_pinned_identity: Mapping[str, frozenset[str]]
    edges_by_source: Mapping[str, tuple[DependencyProofReferenceV1, ...]]
    edges_by_target: Mapping[str, tuple[DependencyProofReferenceV1, ...]]
    edge_tree: MerkleTree[DependencyEdgeRoot]

    @property
    def edge_root(self) -> DependencyEdgeRoot:
        return self.edge_tree.root

    def edges(self) -> tuple[DependencyProofReferenceV1, ...]:
        """Return the whole edge set, which only the flat v2 digest still needs."""

        return _sorted_edges(edge for edges in self.edges_by_source.values() for edge in edges)

    def touching(self, path: str) -> Iterable[DependencyProofReferenceV1]:
        return (*self.edges_by_source.get(path, ()), *self.edges_by_target.get(path, ()))


@dataclass(frozen=True)
class ReversePinClosureItem:
    """One member reached by a complete deterministic reverse-pin walk."""

    state: ArtifactDependencyStateV1
    triggering_identity: ArtifactIdentity
    dependency_edge_roles: tuple[str, ...]


def reverse_pin_closure(
    tree: Mapping[str, bytes],
    *,
    root: ArtifactIdentity,
    include: Callable[[ArtifactDependencyStateV1], bool],
) -> tuple[ReversePinClosureItem, ...]:
    """Return the complete included reverse-pin closure of ``root``.

    Inclusion controls both membership and traversal. That is deliberate: a
    mutation operation may only walk through artifact families it can
    disposition, and must refuse an included unsupported family rather than
    silently discover dependencies beyond an excluded member.
    """

    index = build_dependency_index(tree)
    pending = [root.qualified]
    seen_identities = {root.qualified}
    inventory: dict[str, ReversePinClosureItem] = {}
    while pending:
        triggering = pending.pop(0)
        for path in sorted(
            index.sources_by_pinned_identity.get(triggering, frozenset()),
            key=lambda item: item.encode("utf-8"),
        ):
            state = index.states[path]
            if state.identity.qualified in seen_identities or not include(state):
                continue
            roles = tuple(
                sorted(
                    {pin.role for pin in state.pins if pin.target.qualified == triggering},
                    key=lambda item: item.encode("utf-8"),
                )
            )
            if not roles:
                raise ValueError("reverse-pin index lacks an exact dependency edge")
            inventory[state.identity.qualified] = ReversePinClosureItem(
                state=state,
                triggering_identity=parse_artifact_identity(triggering),
                dependency_edge_roles=roles,
            )
            seen_identities.add(state.identity.qualified)
            pending.append(state.identity.qualified)
    return tuple(
        inventory[identity] for identity in sorted(inventory, key=lambda item: item.encode("utf-8"))
    )


def _pin_sources(
    artifacts: Iterable[ArtifactDependencyStateV1],
) -> dict[str, set[str]]:
    sources: dict[str, set[str]] = {}
    for item in artifacts:
        for pin in item.pins:
            sources.setdefault(pin.target.qualified, set()).add(item.path)
    return sources


def _grouped_edges(
    edges: Iterable[DependencyProofReferenceV1],
    *,
    key: str,
) -> dict[str, tuple[DependencyProofReferenceV1, ...]]:
    grouped: dict[str, list[DependencyProofReferenceV1]] = {}
    for edge in edges:
        grouped.setdefault(getattr(edge, key), []).append(edge)
    return {path: _sorted_edges(items) for path, items in grouped.items()}


def build_dependency_index(tree: Mapping[str, bytes]) -> DependencyIndexV1:
    """Build one tree's complete dependency index by parsing every member.

    This is the cold path, and the oracle every incremental result is compared
    against. A cold start -- genesis, an ad-hoc replay window, a checkpoint seed,
    or settlement, which holds no cross-call state at all -- takes it.
    """

    artifacts = dependency_artifacts(tree)
    edges = _edges(artifacts)
    return DependencyIndexV1(
        states={item.path: item for item in artifacts},
        paths_by_identity={item.identity.qualified: item.path for item in artifacts},
        sources_by_pinned_identity={
            identity: frozenset(paths) for identity, paths in _pin_sources(artifacts).items()
        },
        edges_by_source=_grouped_edges(edges, key="source_path"),
        edges_by_target=_grouped_edges(edges, key="target_path"),
        edge_tree=build_dependency_edge_tree(edges),
    )


def update_dependency_index(
    index: DependencyIndexV1,
    *,
    tree: Mapping[str, bytes],
    changed: Iterable[str],
) -> DependencyIndexV1:
    """Apply one change set, parsing and re-resolving only what actually moved.

    `changed` names every member whose exact bytes differ from the tree `index`
    describes, in either direction. Only those members are parsed. An edge is
    re-resolved when its source's pins changed *or* when the resolution of an
    identity it reads changed, which the carried reverse map finds without
    touching an unrelated member. The result is what `build_dependency_index`
    would have returned for `tree`.
    """

    touched = sorted(set(changed))
    states = dict(index.states)
    paths_by_identity = dict(index.paths_by_identity)
    pin_sources = {
        identity: set(paths) for identity, paths in index.sources_by_pinned_identity.items()
    }
    touched_identities: set[str] = set()

    for path in touched:
        previous = states.pop(path, None)
        if previous is not None:
            touched_identities.add(previous.identity.qualified)
            if paths_by_identity.get(previous.identity.qualified) == path:
                del paths_by_identity[previous.identity.qualified]
            for pin in previous.pins:
                holders = pin_sources.get(pin.target.qualified)
                if holders is not None:
                    holders.discard(path)
                    if not holders:
                        del pin_sources[pin.target.qualified]

    for path in touched:
        content = tree.get(path)
        if content is None:
            continue
        parsed = parse_dependency_artifact(path, content)
        if parsed is None:
            continue
        identity = parsed.identity.qualified
        if paths_by_identity.get(identity, path) != path:
            raise ValueError(_DUPLICATE_IDENTITY)
        states[path] = parsed
        paths_by_identity[identity] = path
        touched_identities.add(identity)
        for pin in parsed.pins:
            pin_sources.setdefault(pin.target.qualified, set()).add(path)

    # Every touched member is re-resolved, including one that left the tree: its
    # own outgoing edges leave with it, and nothing else in the change set is
    # obliged to mention them.
    affected = set(touched)
    for identity in touched_identities:
        affected.update(index.sources_by_pinned_identity.get(identity, frozenset()))
        affected.update(pin_sources.get(identity, set()))
    affected &= set(states) | set(touched)

    edges_by_source = dict(index.edges_by_source)
    affected_targets: set[str] = set()
    updates: dict[str, tuple[DependencyProofReferenceV1, ...]] = {}
    for path in sorted(affected):
        for edge in edges_by_source.get(path, ()):
            affected_targets.add(edge.target_path)
        state = states.get(path)
        resolved = (
            ()
            if state is None
            else _outgoing_edges(state, states=states, paths_by_identity=paths_by_identity)
        )
        for edge in resolved:
            affected_targets.add(edge.target_path)
        updates[path] = resolved
        if resolved:
            edges_by_source[path] = resolved
        else:
            edges_by_source.pop(path, None)

    edges_by_target = dict(index.edges_by_target)
    for target in sorted(affected_targets):
        incoming = _sorted_edges(
            edge
            for source in pin_sources.get(
                states[target].identity.qualified if target in states else "",
                set(),
            )
            for edge in edges_by_source.get(source, ())
            if edge.target_path == target
        )
        if incoming:
            edges_by_target[target] = incoming
        else:
            edges_by_target.pop(target, None)

    return DependencyIndexV1(
        states=states,
        paths_by_identity=paths_by_identity,
        sources_by_pinned_identity={
            identity: frozenset(paths) for identity, paths in pin_sources.items()
        },
        edges_by_source=edges_by_source,
        edges_by_target=edges_by_target,
        edge_tree=update_dependency_edge_tree(index.edge_tree, updated=updates),
    )


@dataclass(frozen=True)
class _ClosureFacts:
    """Everything one closure evaluation determines, before any version speaks.

    The reverse-dependency walk, the pin resolution, and the per-member proof
    assembly are one algorithm; v2 and v3 differ only in how they commit to the
    edges it read. Keeping the facts in one place is what makes the succession a
    change of commitment rather than a second closure implementation.
    """

    scope: tuple[str, ...]
    parent: DependencyIndexV1
    candidate: DependencyIndexV1
    missing_dependents: tuple[IncompleteClosureItemV1, ...]
    unresolved_pins: tuple[UnresolvedArtifactPinV1, ...]
    member_dependency_proofs: tuple[MemberDependencyProofsV1, ...]

    @property
    def verdict(self) -> Literal["complete", "refused"]:
        return "refused" if self.missing_dependents or self.unresolved_pins else "complete"


def judge_dependency_closure(
    *,
    parent: DependencyIndexV1,
    candidate: DependencyIndexV1,
    scope: tuple[str, ...],
) -> _ClosureFacts:
    """Judge one scope against two indexes; reads only the scope's own neighbourhood.

    This is the whole closure law. It is a pure function of its arguments, so an
    index carried forward from the parent generation and an index built from
    scratch over the same tree are indistinguishable to it -- which is exactly
    what makes the incremental maintenance safe to believe.
    """

    if tuple(normalize_manifest_paths(scope)) != scope or not scope:
        raise ValueError("closure scope must be nonempty, sorted, and unique")
    scope_set = set(scope)
    missing: list[IncompleteClosureItemV1] = []
    for changed_path in scope:
        changed = parent.states.get(changed_path)
        if changed is None:
            continue
        for edge in parent.edges_by_target.get(changed_path, ()):
            if edge.source_path in scope_set:
                continue
            dependent = parent.states[edge.source_path]
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
    unresolved: list[UnresolvedArtifactPinV1] = []
    proofs: list[MemberDependencyProofsV1] = []
    for path in scope:
        source = candidate.states.get(path)
        if source is not None:
            unresolved.extend(
                _unresolved_pins_for(
                    source,
                    states=candidate.states,
                    paths_by_identity=candidate.paths_by_identity,
                )
            )
        proofs.append(
            MemberDependencyProofsV1(
                path=path,
                proof_refs=_sorted_edges({*parent.touching(path), *candidate.touching(path)}),
            )
        )
    return _ClosureFacts(
        scope=scope,
        parent=parent,
        candidate=candidate,
        missing_dependents=tuple(
            sorted(missing, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
        ),
        unresolved_pins=tuple(
            sorted(unresolved, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
        ),
        member_dependency_proofs=tuple(proofs),
    )


def _closure_facts(
    *,
    parent_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> _ClosureFacts:
    """Judge one scope over two indexes built from scratch: the cold path."""

    return judge_dependency_closure(
        parent=build_dependency_index(parent_tree),
        candidate=build_dependency_index(candidate_tree),
        scope=scope,
    )


def evaluate_dependency_closure(
    *,
    parent_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> ClosureEvaluationV2:
    """Evaluate closure and commit to both trees' full edge lists in one digest."""

    return closure_evaluation_v2(
        _closure_facts(
            parent_tree=parent_tree,
            candidate_tree=candidate_tree,
            scope=scope,
        )
    )


def closure_evaluation_v2(facts: _ClosureFacts) -> ClosureEvaluationV2:
    """Speak one judgement as the v2 evaluation, whole-edge-list digest and all.

    The digest covers both trees' complete edge lists, so this version is
    inherently O(total) to state however cheaply the judgement itself was
    reached. That is the defect the succession retires; until every accepted
    receipt is v3, replaying a v1 or v2 generation still has to pay it, and must
    pay it byte-identically.
    """

    graph_digest = typed_digest(
        Sha256Value,
        "playbill-dependency-graph-v2",
        {
            "parent_edges": [item.model_dump(mode="json") for item in facts.parent.edges()],
            "candidate_edges": [item.model_dump(mode="json") for item in facts.candidate.edges()],
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


def closure_evaluation_v3(facts: _ClosureFacts) -> ClosureEvaluationV3:
    """Speak one judgement as the v3 evaluation, committing to the edge root.

    The root is read off the candidate index's own trie, which the incremental
    maintenance updated for exactly the members that moved, so stating the
    commitment costs what reaching the judgement cost.
    """

    return ClosureEvaluationV3(
        verdict=facts.verdict,
        paths=facts.scope,
        dependency_edge_root=facts.candidate.edge_root.tagged,
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
    """Evaluate the same closure from scratch and commit to the edge root."""

    return closure_evaluation_v3(
        _closure_facts(
            parent_tree=parent_tree,
            candidate_tree=candidate_tree,
            scope=scope,
        )
    )


__all__ = [
    "ArtifactDependencyStateV1",
    "ClosureEvaluationV2",
    "ClosureEvaluationV3",
    "DEFERRED_PIN_TARGET_KINDS",
    "DEPENDENCY_EDGE_SET_DOMAIN",
    "DependencyIndexV1",
    "IncompleteClosureItemV1",
    "MemberDependencyProofsV1",
    "ReversePinClosureItem",
    "UnresolvedArtifactPinV1",
    "build_dependency_edge_tree",
    "build_dependency_index",
    "closure_evaluation_v2",
    "closure_evaluation_v3",
    "dependency_artifacts",
    "dependency_edge_members",
    "dependency_edge_root",
    "evaluate_dependency_closure",
    "evaluate_dependency_closure_v3",
    "judge_dependency_closure",
    "parse_dependency_artifact",
    "reverse_pin_closure",
    "update_dependency_edge_tree",
    "update_dependency_index",
    "verify_dependency_edge_root",
]
