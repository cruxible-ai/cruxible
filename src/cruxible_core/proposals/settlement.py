"""Closed candidate/change-set/generation correspondence before main settlement."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.attestations import (
    ApprovalSubmission,
    VerifiedApproval,
    verify_candidate_approvals,
)
from cruxible_client.contracts.candidates import (
    CandidateMemberLawEvidence,
    CandidateRecord,
    ClosureProof,
    MemberLawEvaluation,
    SemanticCandidate,
    candidate_digest,
)
from cruxible_client.contracts.canonical import (
    CandidateDigest,
    ChangeSetDigest,
    DependencyEdgeRoot,
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    canonical_list_bytes,
    composed_canonical_digest,
    model_canonical_bytes,
)
from cruxible_client.contracts.captures import ProducerReceiptResolverProtocol
from cruxible_client.contracts.content_memo import ParseMemo, seal, sealed_memo
from cruxible_client.contracts.documents import BodyVerifierProtocol
from cruxible_client.contracts.errors import (
    SettlementEvidenceUnproven,
    SettlementIntegrityError,
)
from cruxible_client.contracts.governance import (
    ActivationPolicy,
    ApprovalRequirement,
    PermissionTier,
    governance_identifier,
)
from cruxible_client.contracts.laws import ACCEPTANCE_LAWS, AcceptanceLawRegistry
from cruxible_client.contracts.merkle import MerkleManifest, MerkleTree
from cruxible_client.contracts.principals import (
    PrincipalRegistrySnapshot,
    principal_registry_from_tree,
)
from cruxible_client.contracts.types import GenerationDescriptor
from cruxible_core.compiler.upgrades import compiler_after_record
from cruxible_core.derived.derived_runtime import BoundedCache
from cruxible_core.derived.derived_state import CandidateTree, SnapshotTree
from cruxible_core.indexes.projection import (
    AcceptedProjectionCoordinate,
    CandidateGenerationProjectionCoordinate,
)
from cruxible_core.indexes.subject_identity import SubjectAssignmentAuthority
from cruxible_core.ledger.bootstrap import generation_root
from cruxible_core.ledger.generation_facts import AcceptingAdmission
from cruxible_core.ledger.git import GitLedger
from cruxible_core.ledger.semantic_root import compute_semantic_root
from cruxible_core.proposals.prepared_evaluation import AdmittedEvaluation
from cruxible_core.proposals.proposal_message import generation_commit_message
from cruxible_core.proposals.proposals import (
    AcceptedReferentsProvider,
    AttestationPrincipalProvider,
    ClaimLawEvidenceProvider,
    ClaimQueryFactsProvider,
    ExhaustPromotionVerifierProtocol,
    HistoricalArtifactProvider,
    TreeStateProvider,
    claim_admission_accounts_from_candidate,
    claim_type_expansions_from_candidate,
    evaluate_proposal_tree,
)
from cruxible_core.runtime.phase_timing import timed_phase


class _StrictSettlementModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


GENERATION_CONSTRUCTION: Final = "generation.construction"


class SettlementCrashHook(Protocol):
    def __call__(self, checkpoint: str) -> None: ...


def _checkpoint(phase: str, hook: SettlementCrashHook | None) -> None:
    if phase not in {"before", "after"}:
        raise SettlementIntegrityError("unknown generation-construction crash phase")
    if hook is not None:
        hook(f"{phase}:{GENERATION_CONSTRUCTION}")


class SettlementBinding(_StrictSettlementModel):
    """Locator-bearing `C_g`; deliberately separate from the semantic candidate."""

    tag: Literal["playbill-settlement-v1"] = "playbill-settlement-v1"
    c_s_digest: str
    base_oid: str

    @field_validator("c_s_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @field_validator("base_oid")
    @classmethod
    def _base_oid(cls, value: str) -> str:
        malformed_hex = any(character not in "0123456789abcdef" for character in value)
        if len(value) not in {40, 64} or malformed_hex:
            raise ValueError("settlement base OID is malformed")
        return value


class ChangeActorBinding(_StrictSettlementModel):
    """Format-neutral identity established at authenticated proposal admission."""

    actor_id: str
    source_compilation_digest: str | None = None

    @field_validator("actor_id")
    @classmethod
    def _actor_id(cls, value: str) -> str:
        return governance_identifier(value, label="change-set actor")

    @field_validator("source_compilation_digest")
    @classmethod
    def _source_compilation_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value


class ChangeSetRecord(_StrictSettlementModel):
    """The one governed-change receipt stored in every accepted generation's commit.

    The candidate signs a merkle manifest root, and the closure proof commits to
    an incrementally maintainable edge root. Every accepted generation settles in
    this receipt; no other receipt version is parsed or replayed.
    """

    tag: Literal["playbill-changeset-v3"] = "playbill-changeset-v3"
    sequence: int = Field(ge=1)
    members: tuple[CandidateMemberLawEvidence, ...]
    closure_proof: ClosureProof
    law_evidence: tuple[MemberLawEvaluation, ...]
    required_tier: PermissionTier
    approval_requirements: tuple[ApprovalRequirement, ...]
    activation_policy: ActivationPolicy
    candidate: SemanticCandidate
    candidate_digest: str
    law_digests: dict[str, str]
    compiler_digest: str
    approvals: tuple[ApprovalSubmission, ...]
    actor_binding: ChangeActorBinding
    mandate_digest: str | None = None
    changeset_digest: str

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @field_validator("compiler_digest", "mandate_digest")
    @classmethod
    def _generic_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("law_digests")
    @classmethod
    def _law_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return _validated_change_set_law_digests(value, label="v3 change-set")

    @field_validator("changeset_digest")
    @classmethod
    def _changeset_digest(cls, value: str) -> str:
        ChangeSetDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _complete_correspondence(self) -> "ChangeSetRecord":
        CandidateRecord(
            candidate=self.candidate,
            candidate_digest=self.candidate_digest,
            required_tier=self.required_tier,
            approval_requirements=self.approval_requirements,
            activation_policy=self.activation_policy,
            closure_proof=self.closure_proof,
            members=self.members,
            law_evidence=self.law_evidence,
            law_digests=self.law_digests,
            compiler_digest=self.compiler_digest,
        )
        _verify_multi_member_change_set(self, label="v3 change-set")
        return self


def _validated_change_set_law_digests(value: dict[str, str], *, label: str) -> dict[str, str]:
    if not value or list(value) != sorted(value, key=lambda item: item.encode("utf-8")):
        raise ValueError(f"{label} acceptance-law mapping must be nonempty and sorted")
    for identifier, digest in value.items():
        governance_identifier(identifier, label=f"{label} acceptance-law identifier")
        Sha256Value.from_tagged(digest)
    return value


def _verify_multi_member_change_set(
    record: "ChangeSetRecord",
    *,
    label: str,
) -> None:
    """Close the receipt-side correspondence of one change-set receipt."""

    if candidate_digest(record.candidate).tagged != record.candidate_digest:
        raise ValueError(f"{label} candidate digest does not reproduce")
    paths = tuple(member.path for member in record.members)
    if paths != record.candidate.scope or record.closure_proof.paths != record.candidate.scope:
        raise ValueError(f"{label} member/closure paths differ from C_s.scope")
    if tuple(item.path for item in record.law_evidence) != paths:
        raise ValueError(f"{label} law evidence differs from members")
    if {item.law_identifier for item in record.members} != set(record.law_digests):
        raise ValueError(f"{label} members and acceptance-law mapping differ")
    if change_set_digest(record).tagged != record.changeset_digest:
        raise ValueError(f"{label} self digest does not reproduce")


#: The receipt fields that hold one entry per member: composed from each entry's
#: own canonical bytes, which a sealed entry keeps (``_receipt_digest``).
_MEMBER_LISTS: Final = ("members", "law_evidence")


def _receipt_digest(tag: str, values: Mapping[str, object]) -> ChangeSetDigest:
    """The change-set digest of ``values`` (models, or tuples of models, or JSON values).

    The per-member lists are composed from their entries' canonical bytes, so a
    receipt built from a sealed candidate encodes none of its members again; the
    preimage is byte for byte the one the whole record's JSON dump hashes to.
    """

    lists = {
        name: canonical_list_bytes([model_canonical_bytes(item) for item in value])
        for name, value in values.items()
        if name in _MEMBER_LISTS and isinstance(value, tuple)
    }
    rest = _json_values({name: value for name, value in values.items() if name not in lists})
    return ChangeSetDigest(composed_canonical_digest(tag, rest, lists))


@sealed_memo
def change_set_digest(record: ChangeSetRecord) -> ChangeSetDigest:
    values = {name: getattr(record, name) for name in type(record).model_fields}
    values.pop("tag")
    values.pop("changeset_digest")
    return _receipt_digest(record.tag, values)


def build_change_set_record(
    candidate: CandidateRecord,
    *,
    sequence: int,
    approvals: tuple[ApprovalSubmission, ...],
    actor_binding: ChangeActorBinding,
    mandate_digest: str | None = None,
) -> ChangeSetRecord:
    """Wrap one validated candidate in the change-set receipt it settles in."""

    values = {
        "sequence": sequence,
        "members": candidate.members,
        "closure_proof": candidate.closure_proof,
        "law_evidence": candidate.law_evidence,
        "required_tier": candidate.required_tier,
        "approval_requirements": candidate.approval_requirements,
        "activation_policy": candidate.activation_policy,
        "candidate": candidate.candidate,
        "candidate_digest": candidate.candidate_digest,
        "law_digests": candidate.law_digests,
        "compiler_digest": candidate.compiler_digest,
        "approvals": approvals,
        "actor_binding": actor_binding,
        "mandate_digest": mandate_digest,
    }
    digest = _receipt_digest("playbill-changeset-v3", values)
    receipt = ChangeSetRecord.model_validate({**values, "changeset_digest": digest.tagged})
    # Built here and final: sealed, it is rendered and digested once.
    seal(receipt)
    return receipt


def _json_values(values: dict[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in values.items():
        if isinstance(value, BaseModel):
            normalized[key] = value.model_dump(mode="json")
        elif isinstance(value, tuple):
            normalized[key] = [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                for item in value
            ]
        else:
            normalized[key] = value
    return normalized


def change_set_path(record: ChangeSetRecord) -> str:
    return f"changesets/cs-{record.sequence:020d}.json"


def render_change_set(record: ChangeSetRecord) -> bytes:
    return model_canonical_bytes(record) + b"\n"


#: Accepted change-set receipts, parsed once per exact (path, bytes) and shared
#: sealed (``content_memo``): one settle reads its parent's receipt and its own
#: (handoff, history) more than once, and each parse re-verifies every member's
#: law evidence. Few and large: a handful are kept, each up to a few MB of JSON.
_CHANGE_SET_PARSES: ParseMemo[ChangeSetRecord] = ParseMemo(
    capacity=8, max_bytes=96 * 1024 * 1024, max_entry_bytes=48 * 1024 * 1024
)


def parse_change_set_record(
    content: bytes, *, path: str, canonical_cache: BoundedCache[bool] | None = None
) -> ChangeSetRecord:
    """Parse one accepted change-set receipt and verify exact canonical bytes.

    This is the one seam through which accepted change-set bytes enter replay,
    checkpoint re-derivation, and accepted projection.

    A parse runs once per exact (path, bytes) and every caller gets the one
    sealed record; a refusal is never remembered.
    """

    return _CHANGE_SET_PARSES.parse(
        content,
        path,
        None,
        lambda: _parse_change_set_record(content, path=path, canonical_cache=canonical_cache),
    )


def _parse_change_set_record(
    content: bytes, *, path: str, canonical_cache: BoundedCache[bool] | None
) -> ChangeSetRecord:
    try:
        record = ChangeSetRecord.model_validate_json(content)
    except (ValueError, ValidationError) as exc:
        raise SettlementIntegrityError(f"generation change-set record is invalid: {path}") from exc
    # A record too large for the parse memo is parsed afresh, and the expensive
    # canonical round trip stays reusable for identical content; this marker
    # neither authenticates a locator nor proves source availability.
    key = hashlib.sha256(content).digest() if canonical_cache is not None else None
    if canonical_cache is None or canonical_cache.get(key) is None:
        if render_change_set(record) != content:
            raise SettlementIntegrityError(f"generation change-set record is not canonical: {path}")
        if canonical_cache is not None:
            canonical_cache.put(key, True, weight=64)
    return record


def render_generation_descriptor(descriptor: GenerationDescriptor) -> bytes:
    return canonical_bytes(descriptor.model_dump(mode="json")) + b"\n"


def semantic_root_for_record(
    record: ChangeSetRecord,
    *,
    approval_digests: tuple[str, ...],
    parent_semantic_root: str,
) -> SemanticRoot:
    """Derive one accepted generation's semantic root.

    Settlement, replay, and checkpoint prefix re-derivation all ask this one
    question, so they ask it in one place. Every accepted generation's parent
    is a semantic root under the one derivation (generation zero's included),
    so a successor needs nothing of its parent but that root. The manifest root
    enters from the receipt the caller has already reproduced from member
    bytes, so nothing here is believed that was not first recomputed.
    """

    return compute_semantic_root(
        manifest_root=record.candidate.candidate_manifest_root,
        changeset_digest=record.changeset_digest,
        approval_digests=approval_digests,
        parent=cast(SemanticRoot, SemanticRoot.from_tagged(parent_semantic_root)),
    )


@dataclass(frozen=True)
class VerifiedGenerationBundle:
    settlement: SettlementBinding
    record: ChangeSetRecord
    record_path: str
    tree: Mapping[str, bytes]
    members: Mapping[str, str]
    oid: str
    semantic_root: SemanticRoot
    descriptor: GenerationDescriptor
    generation_root: GenerationRoot
    principals: PrincipalRegistrySnapshot
    approvals: tuple[VerifiedApproval, ...]
    #: ``members``' manifest-domain trie, as the candidate's evaluation advanced
    #: it: a replay checkpoint reads its root instead of rebuilding the trie.
    merkle: MerkleManifest | None = None
    #: The evaluator already advanced the candidate's dependency proof trie.
    #: Acceptance carries it to the exact accepted OID; the following write
    #: must not reprocess this generation's changed dependency edges.
    dependency_edges: MerkleTree[DependencyEdgeRoot] | None = None

    def projection_coordinate(
        self,
        *,
        base: AcceptedProjectionCoordinate,
    ) -> CandidateGenerationProjectionCoordinate:
        if self.settlement.base_oid != base.git_oid:
            raise SettlementIntegrityError("generation bundle and projection base differ")
        return CandidateGenerationProjectionCoordinate(
            instance_id=base.instance_id,
            repository_path=base.repository_path,
            git_object_format=base.git_object_format,
            git_oid=self.oid,
            semantic_root=self.semantic_root.tagged,
            generation_root=self.generation_root.tagged,
            compiler=compiler_after_record(self.record),
            base_git_oid=base.git_oid,
        )


#: The refusals a first citation of an unproduced Capture earns, Claim and attestation.
_UNPROVEN_CAPTURE_CODES = frozenset(
    {
        "cruxible.claim.capture_production_unproven",
        "cruxible.claim_attestation.capture_production_unproven",
    }
)


@timed_phase("settlement")
def prepare_generation(
    ledger: GitLedger,
    *,
    base: AcceptedProjectionCoordinate,
    candidate_tree: Mapping[str, bytes],
    candidate: CandidateRecord,
    approval_submissions: tuple[ApprovalSubmission, ...],
    candidate_tree_oid: str | None = None,
    bodies: BodyVerifierProtocol,
    actor_binding: ChangeActorBinding,
    proposal_actor_id: str,
    sequence: int,
    laws: AcceptanceLawRegistry = ACCEPTANCE_LAWS,
    mandate_digest: str | None = None,
    crash_hook: SettlementCrashHook | None = None,
    promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    query_facts_provider: ClaimQueryFactsProvider | None = None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None = None,
    tree_state_provider: TreeStateProvider | None = None,
    accepted_tree_provider: Callable[[str], Mapping[str, bytes]] | None = None,
    claim_law_provider: ClaimLawEvidenceProvider | None = None,
    attestation_principal_provider: AttestationPrincipalProvider | None = None,
    accepted_referents_provider: AcceptedReferentsProvider | None = None,
    principal_registry_provider: Callable[[AcceptedProjectionCoordinate], PrincipalRegistrySnapshot]
    | None = None,
    historical_artifact_provider: HistoricalArtifactProvider | None = None,
    subject_assignment_authority: SubjectAssignmentAuthority | None = None,
    admitted: AdmittedEvaluation | None = None,
    accepting_admission: AcceptingAdmission | None = None,
) -> VerifiedGenerationBundle:
    """Build and verify a generation bundle without mutating main or serving state.

    ``admitted`` is the admission evaluation of the same request's own
    submission. It stands in for re-deriving the candidate only when it is
    exactly reusable (``AdmittedEvaluation.reusable``: same base, unrebased,
    the same candidate and tree, an unchanged derived-state epoch, every
    observed body read and fact build answering the same) and the laws are the
    build's own; every check below runs on it as on a fresh evaluation.

    The new semantic root needs only the base's own root, never its receipt,
    so a settle's cost follows its own change, not its parent's.
    """

    if actor_binding.actor_id != proposal_actor_id:
        raise SettlementIntegrityError(
            "settlement actor binding differs from the proposal admission actor"
        )
    if ledger.object_format() != base.git_object_format:
        raise SettlementIntegrityError("settlement base object format differs from ledger")
    if ledger.read_main() != base.git_oid:
        raise SettlementIntegrityError("settlement base is not the current main ref")
    base_tree = (accepted_tree_provider or ledger.read_tree)(base.git_oid)
    expansions = claim_type_expansions_from_candidate(candidate)
    reused = (
        None
        if admitted is None or laws is not ACCEPTANCE_LAWS
        else admitted.reusable(
            base=base,
            candidate=candidate,
            candidate_tree=candidate_tree,
            actor_id=actor_binding.actor_id,
            mandate_digest=mandate_digest,
            claim_type_expansions=expansions,
            bodies=bodies,
            query_facts_provider=query_facts_provider,
        )
    )
    reevaluated = (
        reused
        if reused is not None
        else evaluate_proposal_tree(
            base_tree=base_tree,
            current_tree=base_tree,
            proposed_tree=candidate_tree,
            current=base,
            bodies=bodies,
            timestamp=candidate.candidate.timestamp,
            rebased=False,
            actor_id=actor_binding.actor_id,
            claim_type_expansions=expansions,
            promotion_verifier=promotion_verifier,
            query_facts_provider=query_facts_provider,
            producer_receipt_resolver=producer_receipt_resolver,
            replay_claim_admission_accounts=claim_admission_accounts_from_candidate(candidate),
            acceptance_laws=laws,
            tree_state_provider=tree_state_provider,
            retained_tree=accepted_tree_provider or ledger.read_tree,
            claim_law_provider=claim_law_provider,
            attestation_principal_provider=attestation_principal_provider,
            accepted_referents_provider=accepted_referents_provider,
            principal_registry_provider=principal_registry_provider,
            delegated_mandate_digest=mandate_digest,
            historical_artifact_provider=historical_artifact_provider,
            subject_assignment_authority=subject_assignment_authority,
            historical_law_coordinates={
                member.path: (
                    member.law_identifier,
                    candidate.law_digests[member.law_identifier],
                )
                for member in candidate.members
            },
        )
    )
    unproven = tuple(
        sorted(
            {
                item.subject.artifact_path if item.subject is not None else "candidate"
                for item in reevaluated.diagnostics
                if item.code in _UNPROVEN_CAPTURE_CODES
            }
        )
    )
    if unproven:
        raise SettlementEvidenceUnproven(unproven)
    if reevaluated.candidate is None or reevaluated.diagnostics or reevaluated.state is None:
        raise SettlementIntegrityError("candidate no longer passes its accepted laws")
    if reevaluated.tree != candidate_tree:
        raise SettlementIntegrityError("candidate derivative cards do not reproduce exactly")
    reproduced = reevaluated.candidate
    # The re-evaluation already hashed every member of the candidate tree exactly
    # once, in the structure this candidate's own version signs, so the manifest
    # and diff checks read what it derived instead of deriving them a second time.
    if reproduced.candidate.candidate_manifest_root != candidate.candidate.candidate_manifest_root:
        raise SettlementIntegrityError("generation semantic projection differs from C_s")
    if (
        reproduced.candidate.semantic_diff_digest != candidate.candidate.semantic_diff_digest
        or reproduced.candidate.scope != candidate.candidate.scope
    ):
        raise SettlementIntegrityError("generation semantic diff differs from C_s")
    if reproduced != candidate:
        raise SettlementIntegrityError("candidate law/closure evidence cannot be reproduced")
    for identifier, digest in candidate.law_digests.items():
        laws.require_historical(identifier=identifier, digest=digest)

    principals = (
        principal_registry_from_tree(base_tree, semantic_root=base.semantic_root)
        if principal_registry_provider is None
        else principal_registry_provider(base)
    )
    if principals.semantic_root != base.semantic_root:
        raise SettlementIntegrityError("principal registry differs from settlement coordinate")
    principal_lifecycle = all(
        member.artifact_kind == "principal-lifecycle" for member in candidate.members
    )
    verified_approvals = verify_candidate_approvals(
        candidate,
        approval_submissions,
        principals=principals,
        creator_principal_id=actor_binding.actor_id,
        purpose="principal-lifecycle" if principal_lifecycle else "ordinary-artifact",
    )
    if (
        any(member.artifact_kind == "compiler-upgrade" for member in candidate.members)
        and not verified_approvals
    ):
        raise SettlementIntegrityError("compiler upgrade requires a signed client approval")

    if principal_lifecycle and actor_binding.actor_id not in {
        approval.signer_id for approval in verified_approvals
    }:
        raise SettlementIntegrityError(
            "principal lifecycle actor must cryptographically approve the transition"
        )
    binding = SettlementBinding(
        c_s_digest=candidate.candidate_digest,
        base_oid=base.git_oid,
    )
    record = build_change_set_record(
        candidate,
        sequence=sequence,
        approvals=approval_submissions,
        actor_binding=actor_binding,
        mandate_digest=mandate_digest,
    )
    record_path = change_set_path(record)
    if record_path in candidate_tree:
        raise SettlementIntegrityError("candidate tree collides with daemon change-set path")
    generation_tree: Mapping[str, bytes]
    if isinstance(candidate_tree, (SnapshotTree, CandidateTree)):
        # Stay a snapshot: add the record without reading every member's bytes.
        builder = candidate_tree.fork()
        builder[record_path] = render_change_set(record)
        generation_tree = builder.snapshot()
    else:
        generation_tree = {**candidate_tree, record_path: render_change_set(record)}

    _checkpoint("before", crash_hook)
    oid = ledger.create_signed_generation(
        generation_tree,
        parent_oid=binding.base_oid,
        sequence=sequence,
        timestamp=candidate.candidate.timestamp,
        accepting_admission=accepting_admission,
        # The settled generation carries the proposal's own member summary, so
        # accepted history reads as the same change set a reviewer approved
        # rather than as an anonymous sequence number.
        message=generation_commit_message(candidate.members, sequence=sequence),
        # The candidate tree was read from this stored proposal tree; the
        # generation adds only its change-set record. The readback below still
        # compares every member of what was stored.
        extends_tree=candidate_tree_oid,
        extends_rows=candidate_tree if candidate_tree_oid is not None else None,
    )
    if ledger.parent_of(oid) != binding.base_oid or not ledger.verify_commit(oid):
        raise SettlementIntegrityError("generation parent or daemon signature failed")
    stored_tree = ledger.read_tree_delta(base.git_oid, oid, parent_tree=base_tree)
    if stored_tree != generation_tree:
        raise SettlementIntegrityError("stored generation tree differs from verified payload")

    approval_digests = tuple(sorted(item.digest.tagged for item in verified_approvals))
    semantic_root = semantic_root_for_record(
        record,
        approval_digests=approval_digests,
        parent_semantic_root=base.semantic_root,
    )
    parent_generation = GenerationRoot.from_tagged(base.generation_root)
    descriptor = GenerationDescriptor(
        semantic_root=semantic_root.value,
        git_oid=oid,
        parent_generation_root=parent_generation.value,
    )
    bundle = VerifiedGenerationBundle(
        settlement=binding,
        record=record,
        record_path=record_path,
        tree=generation_tree,
        # Generation records and review cards are outside the semantic manifest.
        members=reevaluated.state.members,
        oid=oid,
        semantic_root=semantic_root,
        descriptor=descriptor,
        generation_root=generation_root(descriptor),
        principals=principals,
        approvals=verified_approvals,
        merkle=reevaluated.state.merkle,
        dependency_edges=reevaluated.state.dependencies.edge_tree,
    )
    _checkpoint("after", crash_hook)
    return bundle


__all__ = [
    "ChangeActorBinding",
    "ChangeSetRecord",
    "GENERATION_CONSTRUCTION",
    "SettlementBinding",
    "SettlementCrashHook",
    "VerifiedGenerationBundle",
    "build_change_set_record",
    "change_set_digest",
    "change_set_path",
    "prepare_generation",
    "parse_change_set_record",
    "render_change_set",
    "render_generation_descriptor",
    "semantic_root_for_record",
]
