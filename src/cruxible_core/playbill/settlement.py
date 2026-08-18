"""Closed candidate/change-set/generation correspondence before main settlement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from cruxible_core.playbill.attestations import (
    ApprovalSubmission,
    VerifiedApproval,
    verify_candidate_approvals,
)
from cruxible_core.playbill.bootstrap import generation_root
from cruxible_core.playbill.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    CandidateRecord,
    CandidateRecordV2,
    ClosureProofV2,
    MemberLawEvaluationV2,
    SemanticCandidate,
    candidate_digest,
)
from cruxible_core.playbill.canonical import (
    ApprovalDigest,
    CandidateDigest,
    ChangeSetDigest,
    GenerationRoot,
    SemanticManifestRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    manifest_root,
    semantic_diff,
    semantic_projection,
    typed_digest,
)
from cruxible_core.playbill.documents import BodyVerifierProtocol
from cruxible_core.playbill.errors import SettlementIntegrityError
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.governance import (
    ActivationPolicy,
    ApprovalRequirement,
    PermissionTier,
    governance_identifier,
)
from cruxible_core.playbill.laws import PLAYBILL_ACCEPTANCE_LAWS, AcceptanceLawRegistry
from cruxible_core.playbill.policies import (
    ClaimAdmissionCandidateResultV1,
    VerifiedPolicySignerV1,
    evaluate_claim_admission_settlement,
)
from cruxible_core.playbill.principals import (
    PrincipalRegistrySnapshot,
    principal_registry_from_tree,
)
from cruxible_core.playbill.projection import (
    AcceptedProjectionCoordinate,
    CandidateGenerationProjectionCoordinate,
)
from cruxible_core.playbill.proposals import (
    ExhaustPromotionVerifierProtocol,
    claim_type_expansions_from_candidate,
    evaluate_proposal_tree,
)
from cruxible_core.playbill.types import GenerationDescriptor


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


class ClosureProof(_StrictSettlementModel):
    """PB-D's complete singleton-Document closure proof."""

    tag: Literal["playbill-closure-proof-v1"] = "playbill-closure-proof-v1"
    strategy: Literal["singleton-document-v1"] = "singleton-document-v1"
    paths: tuple[str, ...]


class ChangeSetRecord(_StrictSettlementModel):
    """The complete format-neutral governed-change receipt stored in the commit."""

    tag: Literal["playbill-changeset-v1"] = "playbill-changeset-v1"
    sequence: int = Field(ge=1)
    members: tuple[CandidateMemberEvidence, ...]
    closure_proof: ClosureProof
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
        if not value or list(value) != sorted(value, key=lambda item: item.encode("utf-8")):
            raise ValueError("change-set acceptance-law mapping must be nonempty and sorted")
        for identifier, digest in value.items():
            governance_identifier(identifier, label="change-set acceptance-law identifier")
            Sha256Value.from_tagged(digest)
        return value

    @field_validator("changeset_digest")
    @classmethod
    def _changeset_digest(cls, value: str) -> str:
        ChangeSetDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _complete_correspondence(self) -> "ChangeSetRecord":
        if candidate_digest(self.candidate).tagged != self.candidate_digest:
            raise ValueError("change-set candidate digest does not reproduce")
        if self.closure_proof.paths != self.candidate.scope:
            raise ValueError("change-set closure differs from candidate scope")
        if tuple(member.path for member in self.members) != self.candidate.scope:
            raise ValueError("change-set members differ from candidate scope")
        if {member.law_identifier for member in self.members} != set(self.law_digests):
            raise ValueError("change-set members and acceptance-law mapping differ")
        if change_set_digest(self).tagged != self.changeset_digest:
            raise ValueError("change-set self digest does not reproduce")
        return self


class ChangeSetRecordV2(_StrictSettlementModel):
    """Dependency-closed multi-member receipt; C_s and approvals remain v1."""

    tag: Literal["playbill-changeset-v2"] = "playbill-changeset-v2"
    sequence: int = Field(ge=1)
    members: tuple[CandidateMemberLawEvidenceV2, ...]
    closure_proof: ClosureProofV2
    law_evidence: tuple[MemberLawEvaluationV2, ...]
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
        if not value or list(value) != sorted(value, key=lambda item: item.encode("utf-8")):
            raise ValueError("v2 change-set acceptance-law mapping must be nonempty and sorted")
        for identifier, digest in value.items():
            governance_identifier(identifier, label="v2 change-set acceptance-law identifier")
            Sha256Value.from_tagged(digest)
        return value

    @field_validator("changeset_digest")
    @classmethod
    def _changeset_digest(cls, value: str) -> str:
        ChangeSetDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _complete_correspondence(self) -> "ChangeSetRecordV2":
        CandidateRecordV2(
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
        if candidate_digest(self.candidate).tagged != self.candidate_digest:
            raise ValueError("v2 change-set candidate digest does not reproduce")
        paths = tuple(member.path for member in self.members)
        if paths != self.candidate.scope or self.closure_proof.paths != self.candidate.scope:
            raise ValueError("v2 change-set member/closure paths differ from C_s.scope")
        if tuple(item.path for item in self.law_evidence) != paths:
            raise ValueError("v2 change-set law evidence differs from members")
        if {item.law_identifier for item in self.members} != set(self.law_digests):
            raise ValueError("v2 change-set members and acceptance-law mapping differ")
        if change_set_digest(self).tagged != self.changeset_digest:
            raise ValueError("v2 change-set self digest does not reproduce")
        return self


def change_set_digest(record: ChangeSetRecord | ChangeSetRecordV2) -> ChangeSetDigest:
    payload = record.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("changeset_digest")
    return typed_digest(ChangeSetDigest, record.tag, payload)


def build_change_set_record(
    candidate: CandidateRecord | CandidateRecordV2,
    *,
    sequence: int,
    approvals: tuple[ApprovalSubmission, ...],
    actor_binding: ChangeActorBinding,
    mandate_digest: str | None = None,
) -> ChangeSetRecord | ChangeSetRecordV2:
    if isinstance(candidate, CandidateRecordV2):
        v2_values = {
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
        digest = typed_digest(
            ChangeSetDigest,
            "playbill-changeset-v2",
            _json_values(v2_values),
        )
        return ChangeSetRecordV2.model_validate({**v2_values, "changeset_digest": digest.tagged})
    values = {
        "sequence": sequence,
        "members": candidate.members,
        "closure_proof": ClosureProof(paths=candidate.closure_paths),
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
    digest = typed_digest(ChangeSetDigest, "playbill-changeset-v1", _json_values(values))
    return ChangeSetRecord.model_validate({**values, "changeset_digest": digest.tagged})


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


def change_set_path(record: ChangeSetRecord | ChangeSetRecordV2) -> str:
    return f"changesets/cs-{record.sequence:020d}.json"


def render_change_set(record: ChangeSetRecord | ChangeSetRecordV2) -> bytes:
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


def parse_change_set_record(content: bytes, *, path: str) -> ChangeSetRecord | ChangeSetRecordV2:
    """Parse either installed change-set version and verify exact canonical bytes."""

    adapter: TypeAdapter[ChangeSetRecord | ChangeSetRecordV2] = TypeAdapter(
        ChangeSetRecord | ChangeSetRecordV2
    )
    try:
        record = adapter.validate_json(content)
    except (ValueError, ValidationError) as exc:
        raise SettlementIntegrityError(f"generation change-set record is invalid: {path}") from exc
    if render_change_set(record) != content:
        raise SettlementIntegrityError(f"generation change-set record is not canonical: {path}")
    return record


def render_generation_descriptor(descriptor: GenerationDescriptor) -> bytes:
    return canonical_bytes(descriptor.model_dump(mode="json")) + b"\n"


def compute_semantic_root(
    *,
    manifest_root_value: str,
    changeset_digest_value: str,
    approval_digests: tuple[str, ...],
    parent_semantic_root: str,
) -> SemanticRoot:
    manifest = SemanticManifestRoot.from_tagged(manifest_root_value)
    changeset = ChangeSetDigest.from_tagged(changeset_digest_value)
    parent = SemanticRoot.from_tagged(parent_semantic_root)
    parsed_approvals = tuple(ApprovalDigest.from_tagged(value) for value in approval_digests)
    if approval_digests != tuple(sorted(set(approval_digests))):
        raise SettlementIntegrityError("semantic-root approval digests must be sorted and unique")
    return typed_digest(
        SemanticRoot,
        "playbill-sroot-v1",
        {
            "manifest_root": manifest.value,
            "changeset_digest": changeset.value,
            "approval_digests": [approval.value for approval in parsed_approvals],
            "parent_semantic_root": parent.value,
        },
    )


@dataclass(frozen=True)
class VerifiedGenerationBundle:
    settlement: SettlementBinding
    record: ChangeSetRecord | ChangeSetRecordV2
    record_path: str
    tree: dict[str, bytes]
    oid: str
    semantic_root: SemanticRoot
    descriptor: GenerationDescriptor
    generation_root: GenerationRoot
    principals: PrincipalRegistrySnapshot
    approvals: tuple[VerifiedApproval, ...]

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
            compiler=base.compiler,
            base_git_oid=base.git_oid,
        )


def _verify_claim_admission_constraints(
    candidate: CandidateRecord | CandidateRecordV2,
    approvals: tuple[VerifiedApproval, ...],
) -> None:
    """Recheck candidate-emitted Claim signer law against verified approvals."""

    if not isinstance(candidate, CandidateRecordV2):
        return
    policy_signers = tuple(
        VerifiedPolicySignerV1(
            signer_id=approval.signer_id,
            roles=tuple(str(role) for role in approval.signer_roles),
        )
        for approval in approvals
    )
    for evidence in candidate.law_evidence:
        raw_evaluations = evidence.result.get("claim_admission", [])
        if not isinstance(raw_evaluations, list):
            raise SettlementIntegrityError("Claim admission law evidence is malformed")
        for raw in raw_evaluations:
            if not isinstance(raw, dict) or "candidate_result" not in raw:
                raise SettlementIntegrityError("Claim admission law evidence is malformed")
            try:
                candidate_result = ClaimAdmissionCandidateResultV1.model_validate(
                    raw["candidate_result"]
                )
            except ValidationError as exc:
                raise SettlementIntegrityError(
                    "Claim admission candidate result is malformed"
                ) from exc
            result = evaluate_claim_admission_settlement(
                candidate_result,
                policy_signers,
                lineage_creation_actor_id=candidate_result.lineage_creation_actor_id,
            )
            if result.verdict == "refused":
                codes = ",".join(result.refusal_codes)
                raise SettlementIntegrityError(
                    f"Claim admission signer constraints are unsatisfied: {codes}"
                )


def prepare_generation(
    ledger: GitLedger,
    *,
    base: AcceptedProjectionCoordinate,
    candidate_tree: dict[str, bytes],
    candidate: CandidateRecord | CandidateRecordV2,
    approval_submissions: tuple[ApprovalSubmission, ...],
    bodies: BodyVerifierProtocol,
    actor_binding: ChangeActorBinding,
    sequence: int,
    laws: AcceptanceLawRegistry = PLAYBILL_ACCEPTANCE_LAWS,
    mandate_digest: str | None = None,
    crash_hook: SettlementCrashHook | None = None,
    promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
) -> VerifiedGenerationBundle:
    """Build and verify a generation bundle without mutating main or serving state."""

    if ledger.object_format() != base.git_object_format:
        raise SettlementIntegrityError("settlement base object format differs from ledger")
    if ledger.read_main() != base.git_oid:
        raise SettlementIntegrityError("settlement base is not the current main ref")
    base_tree = ledger.read_tree(base.git_oid)
    reevaluated = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree=candidate_tree,
        current=base,
        bodies=bodies,
        timestamp=candidate.candidate.timestamp,
        rebased=False,
        actor_id=actor_binding.actor_id,
        claim_type_expansions=claim_type_expansions_from_candidate(candidate),
        promotion_verifier=promotion_verifier,
    )
    if reevaluated.candidate is None or reevaluated.diagnostics:
        raise SettlementIntegrityError("candidate no longer passes its accepted laws")
    if reevaluated.candidate != candidate:
        raise SettlementIntegrityError("candidate law/closure evidence cannot be reproduced")
    for identifier, digest in candidate.law_digests.items():
        laws.require_historical(identifier=identifier, digest=digest)

    principals = principal_registry_from_tree(
        base_tree,
        semantic_root=base.semantic_root,
    )
    principal_lifecycle = all(
        member.artifact_kind == "principal-lifecycle" for member in candidate.members
    )
    verified_approvals = verify_candidate_approvals(
        candidate,
        approval_submissions,
        principals=principals,
        purpose="principal-lifecycle" if principal_lifecycle else "ordinary-artifact",
    )
    _verify_claim_admission_constraints(candidate, verified_approvals)
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
    generation_tree = {**candidate_tree, record_path: render_change_set(record)}

    semantic_tree = semantic_projection(generation_tree)
    candidate_manifest = manifest_root(semantic_tree)
    if candidate_manifest.tagged != candidate.candidate.candidate_manifest_root:
        raise SettlementIntegrityError("generation semantic projection differs from C_s")
    diff, scope = semantic_diff(base_tree, generation_tree)
    if (
        diff.tagged != candidate.candidate.semantic_diff_digest
        or scope != candidate.candidate.scope
    ):
        raise SettlementIntegrityError("generation semantic diff differs from C_s")

    _checkpoint("before", crash_hook)
    oid = ledger.create_signed_generation(
        generation_tree,
        parent_oid=binding.base_oid,
        sequence=sequence,
        timestamp=candidate.candidate.timestamp,
    )
    if ledger.parent_of(oid) != binding.base_oid or not ledger.verify_commit(oid):
        raise SettlementIntegrityError("generation parent or daemon signature failed")
    stored_tree = ledger.read_tree(oid)
    if stored_tree != generation_tree:
        raise SettlementIntegrityError("stored generation tree differs from verified payload")

    approval_digests = tuple(sorted(item.digest.tagged for item in verified_approvals))
    semantic_root = compute_semantic_root(
        manifest_root_value=candidate_manifest.tagged,
        changeset_digest_value=record.changeset_digest,
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
        oid=oid,
        semantic_root=semantic_root,
        descriptor=descriptor,
        generation_root=generation_root(descriptor),
        principals=principals,
        approvals=verified_approvals,
    )
    _checkpoint("after", crash_hook)
    return bundle


__all__ = [
    "ChangeActorBinding",
    "ChangeSetRecord",
    "ChangeSetRecordV2",
    "ClosureProof",
    "GENERATION_CONSTRUCTION",
    "SettlementBinding",
    "SettlementCrashHook",
    "VerifiedGenerationBundle",
    "build_change_set_record",
    "change_set_digest",
    "change_set_path",
    "compute_semantic_root",
    "prepare_generation",
    "parse_change_set_record",
    "render_change_set",
    "render_generation_descriptor",
]
