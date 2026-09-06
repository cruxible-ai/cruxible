"""Closed candidate/change-set/generation correspondence before main settlement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
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
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    CandidateRecordAnyVersion,
    CandidateRecordV2,
    CandidateRecordV3,
    ClosureProofV2,
    ClosureProofV3,
    MemberLawEvaluationV2,
    SemanticCandidate,
    SemanticCandidateV2,
    candidate_digest,
)
from cruxible_client.contracts.canonical import (
    ApprovalDigest,
    CandidateDigest,
    ChangeSetDigest,
    GenerationRoot,
    SemanticManifestRoot,
    SemanticMerkleRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.captures import ProducerReceiptResolverProtocol
from cruxible_client.contracts.documents import BodyVerifierProtocol
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.governance import (
    ActivationPolicy,
    ApprovalRequirement,
    PermissionTier,
    governance_identifier,
    validate_approval_requirements,
)
from cruxible_client.contracts.laws import PLAYBILL_ACCEPTANCE_LAWS, AcceptanceLawRegistry
from cruxible_client.contracts.principals import (
    PrincipalRegistrySnapshot,
    principal_registry_from_tree,
)
from cruxible_client.contracts.types import GenerationDescriptor
from cruxible_core.playbill.bootstrap import generation_root
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.projection import (
    AcceptedProjectionCoordinate,
    CandidateGenerationProjectionCoordinate,
)
from cruxible_core.playbill.proposal_message import generation_commit_message
from cruxible_core.playbill.proposals import (
    ClaimQueryFactsProvider,
    ExhaustPromotionVerifierProtocol,
    TreeStateProvider,
    claim_admission_accounts_from_candidate,
    claim_type_expansions_from_candidate,
    evaluate_proposal_tree,
)


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

    @field_validator("approval_requirements")
    @classmethod
    def _approval_requirements(
        cls, value: tuple[ApprovalRequirement, ...]
    ) -> tuple[ApprovalRequirement, ...]:
        return validate_approval_requirements(value, label="v1 change-set")

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
        return _validated_change_set_law_digests(value, label="v2 change-set")

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
        _verify_multi_member_change_set(self, label="v2 change-set")
        return self


class ChangeSetRecordV3(_StrictSettlementModel):
    """The v2 receipt carrying a v2 `C_s` and a v3 closure proof.

    This is the wire form of the coordinated succession: the candidate signs a
    merkle manifest root, and the closure proof commits to an incrementally
    maintainable edge root. Nothing else about the receipt moves -- members, law
    evidence, approvals, actor binding, and mandate are the v2 shapes.

    Every generation this build accepts settles as a v3 receipt. A ledger older
    than the succession keeps the v1 or v2 receipts it settled, unaltered and
    replayable, so accepted history is a v1/v2 prefix followed by a v3 suffix
    and the boundary between them is a plain generation edge.
    """

    tag: Literal["playbill-changeset-v3"] = "playbill-changeset-v3"
    sequence: int = Field(ge=1)
    members: tuple[CandidateMemberLawEvidenceV2, ...]
    closure_proof: ClosureProofV3
    law_evidence: tuple[MemberLawEvaluationV2, ...]
    required_tier: PermissionTier
    approval_requirements: tuple[ApprovalRequirement, ...]
    activation_policy: ActivationPolicy
    candidate: SemanticCandidateV2
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
    def _complete_correspondence(self) -> "ChangeSetRecordV3":
        CandidateRecordV3(
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


ChangeSetRecordAnyVersion = ChangeSetRecord | ChangeSetRecordV2 | ChangeSetRecordV3


def _validated_change_set_law_digests(value: dict[str, str], *, label: str) -> dict[str, str]:
    if not value or list(value) != sorted(value, key=lambda item: item.encode("utf-8")):
        raise ValueError(f"{label} acceptance-law mapping must be nonempty and sorted")
    for identifier, digest in value.items():
        governance_identifier(identifier, label=f"{label} acceptance-law identifier")
        Sha256Value.from_tagged(digest)
    return value


def _verify_multi_member_change_set(
    record: "ChangeSetRecordV2 | ChangeSetRecordV3",
    *,
    label: str,
) -> None:
    """Close the receipt-side correspondence shared by the v2 and v3 receipts."""

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


def change_set_digest(record: ChangeSetRecordAnyVersion) -> ChangeSetDigest:
    payload = record.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("changeset_digest")
    return typed_digest(ChangeSetDigest, record.tag, payload)


def build_change_set_record(
    candidate: CandidateRecordAnyVersion,
    *,
    sequence: int,
    approvals: tuple[ApprovalSubmission, ...],
    actor_binding: ChangeActorBinding,
    mandate_digest: str | None = None,
) -> ChangeSetRecordAnyVersion:
    """Wrap one validated candidate in the receipt version its evidence demands.

    The receipt version is read off the candidate, never chosen here: a candidate
    that signs a merkle manifest root and proves closure through an edge root can
    only travel in a v3 receipt, and a v1 or v2 candidate recovered from accepted
    history can only travel in the receipt it originally settled in.
    """

    if isinstance(candidate, CandidateRecordV2 | CandidateRecordV3):
        tag = (
            "playbill-changeset-v3"
            if isinstance(candidate, CandidateRecordV3)
            else "playbill-changeset-v2"
        )
        multi_member_values = {
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
        digest = typed_digest(ChangeSetDigest, tag, _json_values(multi_member_values))
        record = {**multi_member_values, "changeset_digest": digest.tagged}
        if isinstance(candidate, CandidateRecordV3):
            return ChangeSetRecordV3.model_validate(record)
        return ChangeSetRecordV2.model_validate(record)
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


def change_set_path(record: ChangeSetRecordAnyVersion) -> str:
    return f"changesets/cs-{record.sequence:020d}.json"


def render_change_set(record: ChangeSetRecordAnyVersion) -> bytes:
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


_CHANGE_SET_RECORD_ADAPTER: TypeAdapter[ChangeSetRecordAnyVersion] = TypeAdapter(
    ChangeSetRecordAnyVersion
)
_TAGGED_CHANGE_SET_RECORD_ADAPTER: TypeAdapter[ChangeSetRecordAnyVersion] = TypeAdapter(
    Annotated[ChangeSetRecordAnyVersion, Field(discriminator="tag")]
)


def parse_change_set_record(content: bytes, *, path: str) -> ChangeSetRecordAnyVersion:
    """Parse any accepted change-set version and verify exact canonical bytes.

    This is the one seam through which accepted change-set bytes enter replay,
    checkpoint re-derivation, and accepted projection. Every version a Playbill
    instance has ever settled parses here, and each is verified by the derivation
    it was written under: a ledger that crossed the succession boundary carries a
    v1 or v2 prefix and a v3 suffix, and replaying it end to end is ordinary.
    """

    try:
        try:
            record = _TAGGED_CHANGE_SET_RECORD_ADAPTER.validate_json(content)
        except (ValueError, ValidationError):
            # Retain the ordinary union's historical malformed/missing-tag
            # behavior and refusal cause. Only valid tagged records avoid
            # probing unrelated versions; parsed models are never retained.
            record = _CHANGE_SET_RECORD_ADAPTER.validate_json(content)
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


SemanticRootDerivation = Literal["playbill-sroot-v1", "playbill-sroot-v2"]

SEMANTIC_ROOT_V2_DOMAIN: Final = "playbill-sroot-v2"


def compute_semantic_root_v2(
    *,
    manifest_root_value: str,
    changeset_digest_value: str,
    approval_digests: tuple[str, ...],
    parent_semantic_root: str,
    parent_derivation: SemanticRootDerivation,
) -> SemanticRoot:
    """Derive a semantic root from tagged inputs and an explicit parent derivation.

    v1 hashed every input as bare hex, which threw away exactly the domain
    separation the tagged spellings exist to carry: two structurally different
    32-byte values entered its preimage identically. v2 hashes each input in its
    full tagged spelling, so a merkle manifest root can never be read as a flat
    one inside the preimage any more than it can on the wire.

    The preimage is exactly, and only:

    - `tag`: `"playbill-sroot-v2"`, this derivation's own name, supplied by the
      canonical digest domain and thus present in every preimage byte string;
    - `manifest_root`: the candidate manifest root's tagged spelling, which is a
      `merkle-sha256:` merkle root -- a flat root is refused;
    - `changeset_digest`: the change-set digest's tagged spelling;
    - `approval_digests`: the sorted, unique approval digests, each tagged;
    - `parent_semantic_root`: the parent semantic root's tagged spelling;
    - `parent_derivation`: the name of the derivation that produced the parent.

    The last field is the succession chain rule. A v1 and a v2 semantic root are
    both `SemanticRoot` values with the same `sha256:` spelling, so the parent's
    spelling alone cannot say which derivation produced it. The first v2
    generation's parent is the last v1 semantic root, and it enters as
    `parent_derivation="playbill-sroot-v1"`; every later generation's parent
    enters as `"playbill-sroot-v2"`. The same 32-byte parent value therefore
    yields two different children under the two claims, so a chain cannot be
    re-narrated across the succession boundary after the fact.
    """

    manifest = SemanticMerkleRoot.from_tagged(manifest_root_value)
    changeset = ChangeSetDigest.from_tagged(changeset_digest_value)
    parent = SemanticRoot.from_tagged(parent_semantic_root)
    parsed_approvals = tuple(ApprovalDigest.from_tagged(value) for value in approval_digests)
    if approval_digests != tuple(sorted(set(approval_digests))):
        raise SettlementIntegrityError("semantic-root approval digests must be sorted and unique")
    return typed_digest(
        SemanticRoot,
        SEMANTIC_ROOT_V2_DOMAIN,
        {
            "manifest_root": manifest.tagged,
            "changeset_digest": changeset.tagged,
            "approval_digests": [approval.tagged for approval in parsed_approvals],
            "parent_semantic_root": parent.tagged,
            "parent_derivation": parent_derivation,
        },
    )


def record_semantic_root_derivation(
    record: ChangeSetRecordAnyVersion | None,
) -> SemanticRootDerivation:
    """Name the derivation that produced one accepted generation's semantic root.

    The name is read off the generation's own receipt, never asserted beside it:
    a v3 receipt's root was derived by `playbill-sroot-v2` and any earlier
    receipt's by `playbill-sroot-v1`. Genesis has no receipt, and its root is
    what a v1 chain starts from, so it answers `playbill-sroot-v1` -- which makes
    the first accepted generation of every instance, old or new, a v3 record
    whose preimage names a v1 parent. Every ledger therefore states the
    succession boundary from generation one rather than hiding it.
    """

    return SEMANTIC_ROOT_V2_DOMAIN if isinstance(record, ChangeSetRecordV3) else "playbill-sroot-v1"


def semantic_root_for_record(
    record: ChangeSetRecordAnyVersion,
    *,
    approval_digests: tuple[str, ...],
    parent_semantic_root: str,
    parent_record: ChangeSetRecordAnyVersion | None,
) -> SemanticRoot:
    """Derive one accepted generation's semantic root under its own derivation.

    Settlement, replay, and checkpoint prefix re-derivation all ask this one
    question, so they ask it in one place: three copies of a version dispatch
    over a succession boundary is three chances to disagree about which side of
    it a generation is on. The manifest root enters from the receipt the caller
    has already reproduced from member bytes, so nothing here is believed that
    was not first recomputed.
    """

    if isinstance(record, ChangeSetRecordV3):
        return compute_semantic_root_v2(
            manifest_root_value=record.candidate.candidate_manifest_root,
            changeset_digest_value=record.changeset_digest,
            approval_digests=approval_digests,
            parent_semantic_root=parent_semantic_root,
            parent_derivation=record_semantic_root_derivation(parent_record),
        )
    return compute_semantic_root(
        manifest_root_value=record.candidate.candidate_manifest_root,
        changeset_digest_value=record.changeset_digest,
        approval_digests=approval_digests,
        parent_semantic_root=parent_semantic_root,
    )


def parent_change_set_record(
    parent_tree: Mapping[str, bytes],
    *,
    sequence: int,
) -> ChangeSetRecordAnyVersion | None:
    """Read the receipt of the generation a new one succeeds, or None for genesis.

    `changesets/` is append-only in accepted history, so the parent's own receipt
    is present in the parent's daemon-signed tree and its version is a fact about
    that tree rather than a claim the new generation makes about its parent.
    """

    if sequence <= 1:
        return None
    path = f"changesets/cs-{sequence - 1:020d}.json"
    content = parent_tree.get(path)
    if content is None:
        raise SettlementIntegrityError("settlement base is missing its own change-set record")
    return parse_change_set_record(content, path=path)


@dataclass(frozen=True)
class VerifiedGenerationBundle:
    settlement: SettlementBinding
    record: ChangeSetRecordAnyVersion
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


def prepare_generation(
    ledger: GitLedger,
    *,
    base: AcceptedProjectionCoordinate,
    candidate_tree: dict[str, bytes],
    candidate: CandidateRecordAnyVersion,
    approval_submissions: tuple[ApprovalSubmission, ...],
    bodies: BodyVerifierProtocol,
    actor_binding: ChangeActorBinding,
    proposal_actor_id: str,
    sequence: int,
    laws: AcceptanceLawRegistry = PLAYBILL_ACCEPTANCE_LAWS,
    mandate_digest: str | None = None,
    crash_hook: SettlementCrashHook | None = None,
    promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    query_facts_provider: ClaimQueryFactsProvider | None = None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None = None,
    tree_state_provider: TreeStateProvider | None = None,
) -> VerifiedGenerationBundle:
    """Build and verify a generation bundle without mutating main or serving state."""

    if actor_binding.actor_id != proposal_actor_id:
        raise SettlementIntegrityError(
            "settlement actor binding differs from the proposal admission actor"
        )
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
        query_facts_provider=query_facts_provider,
        producer_receipt_resolver=producer_receipt_resolver,
        replay_claim_admission_accounts=claim_admission_accounts_from_candidate(candidate),
        wire_version=candidate.tag,
        acceptance_laws=laws,
        tree_state_provider=tree_state_provider,
        historical_law_coordinates={
            member.path: (
                member.law_identifier,
                candidate.law_digests[member.law_identifier],
            )
            for member in candidate.members
        },
    )
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
        creator_principal_id=actor_binding.actor_id,
        purpose="principal-lifecycle" if principal_lifecycle else "ordinary-artifact",
    )
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

    _checkpoint("before", crash_hook)
    oid = ledger.create_signed_generation(
        generation_tree,
        parent_oid=binding.base_oid,
        sequence=sequence,
        timestamp=candidate.candidate.timestamp,
        # The settled generation carries the proposal's own member summary, so
        # accepted history reads as the same change set a reviewer approved
        # rather than as an anonymous sequence number.
        message=generation_commit_message(candidate.members, sequence=sequence),
    )
    if ledger.parent_of(oid) != binding.base_oid or not ledger.verify_commit(oid):
        raise SettlementIntegrityError("generation parent or daemon signature failed")
    stored_tree = ledger.read_tree(oid)
    if stored_tree != generation_tree:
        raise SettlementIntegrityError("stored generation tree differs from verified payload")

    approval_digests = tuple(sorted(item.digest.tagged for item in verified_approvals))
    semantic_root = semantic_root_for_record(
        record,
        approval_digests=approval_digests,
        parent_semantic_root=base.semantic_root,
        parent_record=parent_change_set_record(base_tree, sequence=sequence),
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
    "SEMANTIC_ROOT_V2_DOMAIN",
    "ChangeActorBinding",
    "ChangeSetRecord",
    "ChangeSetRecordAnyVersion",
    "ChangeSetRecordV2",
    "ChangeSetRecordV3",
    "ClosureProof",
    "GENERATION_CONSTRUCTION",
    "SemanticRootDerivation",
    "SettlementBinding",
    "SettlementCrashHook",
    "VerifiedGenerationBundle",
    "build_change_set_record",
    "change_set_digest",
    "change_set_path",
    "compute_semantic_root",
    "compute_semantic_root_v2",
    "prepare_generation",
    "parent_change_set_record",
    "parse_change_set_record",
    "record_semantic_root_derivation",
    "render_change_set",
    "render_generation_descriptor",
    "semantic_root_for_record",
]
