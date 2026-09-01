"""Authenticated proposal admission and deterministic PB-C candidate evaluation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Final, Literal, Protocol

from pydantic import (
    BaseModel,
    ValidationError,
    field_validator,
)

from cruxible_client.contracts.acquisition_policies import (
    AcceptedSourceAcquisitionPolicyV1,
    SourceAcquisitionPolicyError,
    acquisition_policy_digest,
    evaluate_acquisition_policy_law,
    parse_acquisition_policy,
)
from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    ApprovalPolicyFormatError,
    approval_policy_digest,
    parse_approval_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring_profiles import (
    AuthoringProfileError,
    ClaimTypeExpansionEvidenceV1,
    verify_claim_type_expansion_evidence,
)
from cruxible_client.contracts.candidates import (
    PRODUCED_CANDIDATE_VERSION,
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    CandidateRecord,
    CandidateRecordAnyVersion,
    CandidateRecordV2,
    CandidateRecordV3,
    CandidateWireVersion,
    ClosureProofV2,
    ClosureProofV3,
    LawEvaluationCoordinateV1,
    MemberLawEvaluationV2,
    SemanticCandidate,
    SemanticCandidateV2,
    candidate_digest,
    candidate_member_evidence_digest,
    member_law_evidence_digest,
    validate_candidate_timestamp,
)
from cruxible_client.contracts.canonical import (
    Manifest,
    ProposalDigest,
    SemanticDiffDigest,
    Sha256Value,
    canonical_bytes,
    canonical_digest,
    file_digest,
    manifest_for_tree,
    manifest_for_tree_carrying,
    manifest_root_from_members,
    normalize_manifest_paths,
    semantic_diff,
    semantic_diff_from_members,
    semantic_projection,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    CaptureFormatError,
    CaptureObjectStoreProtocol,
    capture_contract_digest,
    evaluate_capture_contract_law,
    parse_capture_contract,
)
from cruxible_client.contracts.claim_attestations import (
    accepted_referent_coordinates_from_tree,
)
from cruxible_client.contracts.claim_type_structure import claim_type_structural_signature
from cruxible_client.contracts.claim_types import (
    AcceptedClaimType,
    ClaimType,
    ClaimTypeFormatError,
    ClaimTypeFreshnessHorizonInvalid,
    claim_type_accepts_subject,
    claim_type_digest,
    evaluate_claim_type_law,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifactAny,
    ClaimArtifactV3,
    ClaimFormatError,
    ExactContentClaimObject,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_retirement_pin_digest_updates,
    claim_statement_address,
    claim_statement_digest,
    evaluate_claim_law,
    parse_claim,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.discovery import (
    DiscoveryHintsV1,
    DistinctRelationMemberV1,
    ProposedSemanticInterfaceV1,
    ReuseDispositionV1,
    SemanticReuseInterfaceV1,
    VocabularyReuseRequestV1,
    evaluate_vocabulary_reuse,
)
from cruxible_client.contracts.documents import (
    AcceptedDocument,
    BodyVerifierProtocol,
    document_digest,
    evaluate_document_law,
    parse_document,
)
from cruxible_client.contracts.errors import (
    DocumentFormatError,
    PlaybillError,
    PlaybillReseedRequired,
    PrincipalIntegrityError,
    ProposalAdmissionError,
    ProposalEvaluationIntegrityError,
    ProposalIntegrityError,
    SubjectFormatError,
)
from cruxible_client.contracts.governance import (
    INDEPENDENT_APPROVAL_REQUIREMENTS,
    ActivationPolicy,
    ApprovalRequirement,
    MutationDisposition,
    PermissionTier,
)
from cruxible_client.contracts.laws import (
    APPROVAL_POLICY_ACCEPTANCE_LAW,
    PLAYBILL_ACCEPTANCE_LAWS,
    PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
    PROCEDURE_RUNTIME_POLICY_ACCEPTANCE_LAW,
    InstalledAcceptanceLaw,
)
from cruxible_client.contracts.merkle import (
    MerkleManifest,
    build_merkle_manifest,
    update_merkle_manifest,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionCandidateContextV1,
    ClaimAdmissionCandidateResultV1,
    ClaimAdmissionEvaluationAccountV1,
    ClaimAdmissionPolicyV1,
    ClaimCorroborationResultV1,
    evaluate_claim_admission_candidate,
)
from cruxible_client.contracts.principals import (
    PrincipalRegistrySnapshot,
    principal_registry_from_tree,
)
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_PATH,
    ProcedureRuntimePolicyFormatError,
    parse_procedure_runtime_policy,
    procedure_runtime_policy_digest,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureFormatError,
    evaluate_procedure_law,
    parse_procedure,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpecV1,
    LineSpecFormatError,
    evaluate_line_spec_law,
    line_spec_digest,
    parse_line_spec,
)
from cruxible_client.contracts.proposal_models import (
    AuthenticatedActor,
    ProposalAdmissionRecord,
    ProposalAdmissionRequest,
    ProposalEvaluationRecord,
    ProposalReceiveLimits,
    ProposalResult,
    ProposalTransportProtocol,
    _StrictProposalModel,
    claim_admission_account_order_key,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    ProviderInterfaceFormatError,
    evaluate_provider_interface_law,
    parse_provider_interface,
    provider_interface_digest,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderFormatError,
    evaluate_provider_law,
    parse_provider,
    provider_digest,
)
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinitionV1,
    QueryDefinitionFormatError,
    QueryDefinitionV1,
    evaluate_query_definition_law,
    parse_query_definition,
    query_definition_digest,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.standing_mandates import (
    AcceptedStandingMandateV1,
    StandingMandateError,
    evaluate_standing_mandate_law,
    parse_standing_mandate,
    standing_mandate_digest,
)
from cruxible_client.contracts.subjects import (
    AcceptedSubject,
    evaluate_subject_law,
    parse_subject,
    subject_digest,
    subject_reuse_signature,
)
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    PlaybillWorkspaceAdvertisement,
)
from cruxible_core.playbill.closure import (
    ArtifactDependencyStateV1,
    ClosureEvaluationV2,
    ClosureEvaluationV3,
    DependencyIndexV1,
    IncompleteClosureItemV1,
    UnresolvedArtifactPinV1,
    build_dependency_index,
    closure_evaluation_v2,
    closure_evaluation_v3,
    dependency_artifacts,
    judge_dependency_closure,
    update_dependency_index,
)
from cruxible_core.playbill.compiler import (
    current_compiler_coordinate,
    projection_registry_for_compiler,
)
from cruxible_core.playbill.exhaust.promotions import (
    AcceptedExhaustPromotionV1,
    ExhaustPromotionError,
    ExhaustPromotionLawResultV1,
    ExhaustPromotionV1,
    evaluate_exhaust_promotion_acceptance,
    exhaust_promotion_digest,
    parse_exhaust_promotion,
)
from cruxible_core.playbill.principal_lifecycle import evaluate_principal_lifecycle
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.query.backends import ClaimQueryFactsV1
from cruxible_core.playbill.query.engine import (
    evaluate_claim_query,
    query_execution_receipt,
)

_DOCUMENT_PATH_RE = re.compile(r"^documents/[a-z][a-z0-9_.-]{0,255}\.json$")
_APPROVAL_POLICY_PATH_RE = re.compile(r"^governance/approval-policy\.json$")
_PROCEDURE_RUNTIME_POLICY_PATH_RE = re.compile(r"^governance/procedure-runtime-policy\.json$")
_PRINCIPAL_PATH_RE = re.compile(r"^principals/[a-z][a-z0-9_.-]{0,127}\.json$")
_SUBJECT_PATH_RE = re.compile(
    r"^subjects/[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/"
    r"[a-z][a-z0-9_.-]{0,255}\.json$"
)
_CLAIM_TYPE_PATH_RE = re.compile(
    r"^claim-types/[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/"
    r"[a-z][a-z0-9_]{0,63}\.json$"
)
_CAPTURE_CONTRACT_PATH_RE = re.compile(r"^capture-contracts/[a-z][a-z0-9_.-]{0,255}\.json$")
_PROVIDER_PATH_RE = re.compile(r"^providers/[a-z][a-z0-9_.-]{0,255}\.json$")
_PROVIDER_INTERFACE_PATH_RE = re.compile(r"^provider-interfaces/[a-z][a-z0-9_.-]{0,255}\.json$")
_SOURCE_ACQUISITION_POLICY_PATH_RE = re.compile(
    r"^source-acquisition-policies/[a-z][a-z0-9_.-]{0,255}\.json$"
)
_STANDING_MANDATE_PATH_RE = re.compile(r"^standing-mandates/[a-z][a-z0-9_.-]{0,255}\.json$")
_CLAIM_PATH_RE = re.compile(r"^claims/[0-9a-f]{2}/CLM-[0-9a-f]{32}\.json$")
_PROCEDURE_PATH_RE = re.compile(r"^procedures/[a-z][a-z0-9_.-]{0,255}\.json$")
_LINE_PATH_RE = re.compile(r"^lines/[a-z][a-z0-9_.-]{0,255}\.json$")
_QUERY_DEFINITION_PATH_RE = re.compile(r"^query-definitions/[a-z][a-z0-9_.-]{0,255}\.json$")
_EXHAUST_PROMOTION_PATH_RE = re.compile(r"^exhaust-promotions/[a-z][a-z0-9_.-]{0,255}\.json$")

_DEPENDENCY_CLOSED_PATTERNS: Final = (
    _CLAIM_TYPE_PATH_RE,
    _CAPTURE_CONTRACT_PATH_RE,
    _PROVIDER_PATH_RE,
    _PROVIDER_INTERFACE_PATH_RE,
    _SOURCE_ACQUISITION_POLICY_PATH_RE,
    _STANDING_MANDATE_PATH_RE,
    _CLAIM_PATH_RE,
    _PROCEDURE_PATH_RE,
    _LINE_PATH_RE,
    _QUERY_DEFINITION_PATH_RE,
    _EXHAUST_PROMOTION_PATH_RE,
)
"""The member kinds whose three-way rebase reports exact per-member conflicts.

One list, one meaning. It used to be spelled out twice inside the evaluator --
once to pick a rebase and once to pick an evaluator -- and a member kind added to
one spelling but not the other would have been rebased one way and judged
another. There is only one evaluator now, so only the rebase still asks.
"""

_SEMANTIC_MEMBER_PATTERNS: Final = (
    _APPROVAL_POLICY_PATH_RE,
    _PROCEDURE_RUNTIME_POLICY_PATH_RE,
    _DOCUMENT_PATH_RE,
    _SUBJECT_PATH_RE,
    *_DEPENDENCY_CLOSED_PATTERNS,
)
"""Every member kind that participates in dependency closure, in one place."""

_AUTHORABLE_MEMBER_PATTERNS: Final = (_PRINCIPAL_PATH_RE, *_SEMANTIC_MEMBER_PATTERNS)
"""Every path an authenticated proposal may add, change, or drop.

Admission asks this in both directions -- may the proposal write this path, and
may it remove that one -- and a kind that were authorable in one direction but
not the other would be admissible to create and impossible to retire. One list
answers both.
"""


def _authorable(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in _AUTHORABLE_MEMBER_PATTERNS)


_LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"


class ExhaustPromotionVerifierProtocol(Protocol):
    """Operational verification seam shared by proposal, settlement, and recovery."""

    def verify_promotion(
        self,
        promotion: ExhaustPromotionV1,
    ) -> ExhaustPromotionLawResultV1: ...


class ProposalEvidenceProtocol(Protocol):
    """Daemon persistence seam consumed by the pure proposal service."""

    def write_admission(self, record: ProposalAdmissionRecord) -> object: ...

    def write_evaluation(self, record: ProposalEvaluationRecord) -> object: ...

    def write_candidate(self, record: CandidateRecordAnyVersion) -> object: ...


def validate_proposal_tree(
    tree: Mapping[str, bytes],
    *,
    limits: ProposalReceiveLimits,
    base_tree: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    if len(tree) > limits.max_files:
        raise ProposalAdmissionError("proposal exceeds its file-count limit")
    if base_tree is not None:
        # Counted before any member is parsed, and counted in both directions so
        # that dropping ten thousand members is bounded exactly as adding them is.
        changed = sum(1 for path, content in tree.items() if base_tree.get(path) != content)
        changed += sum(1 for path in base_tree if path not in tree)
        if changed > limits.max_changed_members:
            raise ProposalAdmissionError("proposal exceeds its changed-member limit")
    for raw_path in tree:
        if raw_path.count("/") + 1 > limits.max_path_depth:
            raise ProposalAdmissionError(f"proposal exceeds its path-depth limit: {raw_path}")
    normalized = normalize_manifest_paths(list(tree))
    if set(normalized) != set(tree):
        raise ProposalAdmissionError("proposal paths must already be canonical")
    total = 0
    result: dict[str, bytes] = {}
    base = base_tree or {}
    for path in normalized:
        content = tree[path]
        if not isinstance(content, bytes):
            raise ProposalAdmissionError("proposal tree values must be exact bytes")
        if not _authorable(path) and base.get(path) != content:
            raise ProposalAdmissionError(
                f"proposal changed a daemon-controlled or unregistered path: {path}"
            )
        if len(content) > limits.max_file_bytes:
            raise ProposalAdmissionError(f"proposal blob exceeds its byte limit: {path}")
        if content.startswith(_LFS_PREFIX):
            raise ProposalAdmissionError(f"proposal refuses Git LFS pointer: {path}")
        total += len(content)
        if total > limits.max_total_bytes:
            raise ProposalAdmissionError("proposal exceeds its total-byte limit")
        result[path] = content
    for path in normalize_manifest_paths(list(base)):
        if not _authorable(path) and path not in result:
            raise ProposalAdmissionError(
                f"proposal removed a daemon-controlled or unregistered path: {path}"
            )
    return result


@dataclass(frozen=True)
class CandidateEvaluation:
    """One evaluated proposal, plus the candidate tree's state when it passed.

    `state` is the derived state of the tree the candidate commits to -- its
    member manifest, its manifest trie, and its dependency index -- handed back
    so a caller walking a chain of generations can seed the next evaluation with
    it instead of rebuilding it. It is present only on an accepted candidate: a
    refused evaluation has no tree worth carrying forward.
    """

    tree: dict[str, bytes]
    candidate: CandidateRecordAnyVersion | None
    diagnostics: tuple[CompilerDiagnostic, ...]
    rebased: bool
    state: "EvaluatedTreeState | None" = None
    claim_admission_accounts: tuple[ClaimAdmissionEvaluationAccountV1, ...] = ()


def claim_type_expansions_from_candidate(
    candidate: CandidateRecordAnyVersion,
) -> tuple[ClaimTypeExpansionEvidenceV1, ...]:
    """Recover and revalidate authoring-only evidence committed by member law output."""

    if isinstance(candidate, CandidateRecord):
        return ()
    expansions: list[ClaimTypeExpansionEvidenceV1] = []
    for evidence in candidate.law_evidence:
        raw = evidence.result.get("authoring_expansion")
        if raw is None:
            continue
        try:
            expansions.append(ClaimTypeExpansionEvidenceV1.model_validate(raw))
        except (PlaybillError, ValidationError) as exc:
            raise ProposalIntegrityError(
                "candidate contains invalid ClaimType authoring expansion evidence"
            ) from exc
    return tuple(
        sorted(
            expansions,
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )


def claim_admission_accounts_from_candidate(
    candidate: CandidateRecordAnyVersion,
) -> tuple[ClaimAdmissionEvaluationAccountV1, ...]:
    """Recover the daemon-produced admission accounts committed by member evidence."""

    if isinstance(candidate, CandidateRecord):
        return ()
    accounts: dict[tuple[str, str, str], ClaimAdmissionEvaluationAccountV1] = {}
    committed_query_digests: set[str] = set()
    for evidence in candidate.law_evidence:
        committed_query_digests.update(evidence.query_receipt_digests)
        raw_entries = evidence.result.get("claim_admission", ())
        if not isinstance(raw_entries, list):
            continue
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise ProposalIntegrityError("candidate Claim admission entry is malformed")
            raw_account = raw_entry.get("admission_account")
            if raw_account is None:
                continue
            try:
                account = ClaimAdmissionEvaluationAccountV1.model_validate(raw_account)
            except ValidationError as exc:
                raise ProposalIntegrityError(
                    "candidate contains invalid Claim admission account"
                ) from exc
            key = (account.claim_path, account.claim_type_identity, account.policy_digest)
            if key in accounts and accounts[key] != account:
                raise ProposalIntegrityError("candidate Claim admission account is ambiguous")
            accounts[key] = account
    recovered_query_digests = {
        result.result_digest
        for account in accounts.values()
        for result in account.corroboration_results
    }
    if recovered_query_digests != committed_query_digests:
        raise ProposalIntegrityError(
            "candidate Claim admission accounts differ from query receipt commitments"
        )
    return tuple(sorted(accounts.values(), key=claim_admission_account_order_key))


def _diagnostic(code: str, message: str, path: str | None = None) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity="error",
        message=message,
        subject=SemanticAddress.whole_artifact(path) if path is not None else None,
    )


def deterministic_rebase(
    *,
    base_tree: Mapping[str, bytes],
    current_tree: Mapping[str, bytes],
    proposed_tree: Mapping[str, bytes],
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    """Apply the exact base→proposal delta over current or return conflicting paths."""

    paths = normalize_manifest_paths(
        list({*base_tree.keys(), *current_tree.keys(), *proposed_tree.keys()})
    )
    rebased = dict(current_tree)
    conflicts: list[str] = []
    for path in paths:
        base = base_tree.get(path)
        proposed = proposed_tree.get(path)
        if base == proposed:
            continue
        current = current_tree.get(path)
        if current != base and current != proposed:
            conflicts.append(path)
            continue
        if proposed is None:
            rebased.pop(path, None)
        else:
            rebased[path] = proposed
    return rebased, tuple(conflicts)


class RebaseMemberConflictV2(_StrictProposalModel):
    tag: Literal["playbill-rebase-member-conflict-v2"] = "playbill-rebase-member-conflict-v2"
    code: Literal["playbill.rebase.member_conflict"] = "playbill.rebase.member_conflict"
    path: str
    old_parent_digest: str | None
    proposed_digest: str | None
    new_parent_digest: str | None

    @field_validator("old_parent_digest", "proposed_digest", "new_parent_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value


class RebaseResultV2(_StrictProposalModel):
    tag: Literal["playbill-rebase-result-v2"] = "playbill-rebase-result-v2"
    tree: dict[str, bytes]
    conflicts: tuple[RebaseMemberConflictV2, ...]
    approvals_invalidated: Literal[True] = True


def _optional_file_digest(content: bytes | None) -> str | None:
    return None if content is None else file_digest(content).tagged


def deterministic_rebase_v2(
    *,
    old_parent_tree: Mapping[str, bytes],
    new_parent_tree: Mapping[str, bytes],
    proposed_tree: Mapping[str, bytes],
) -> RebaseResultV2:
    """Three-way member rebase with exact, canonically ordered conflicts."""

    paths = normalize_manifest_paths(
        list({*old_parent_tree.keys(), *new_parent_tree.keys(), *proposed_tree.keys()})
    )
    rebased = dict(new_parent_tree)
    conflicts: list[RebaseMemberConflictV2] = []
    for path in paths:
        old = old_parent_tree.get(path)
        proposed = proposed_tree.get(path)
        if old == proposed:
            continue
        new = new_parent_tree.get(path)
        if new == proposed:
            continue
        if new != old:
            conflicts.append(
                RebaseMemberConflictV2(
                    path=path,
                    old_parent_digest=_optional_file_digest(old),
                    proposed_digest=_optional_file_digest(proposed),
                    new_parent_digest=_optional_file_digest(new),
                )
            )
            continue
        if proposed is None:
            rebased.pop(path, None)
        else:
            rebased[path] = proposed
    return RebaseResultV2(tree=rebased, conflicts=tuple(conflicts))


def _canonical_model_digest(domain: str, model: BaseModel) -> str:
    payload = model.model_dump(mode="json")
    payload.pop("tag", None)
    return typed_digest(Sha256Value, domain, payload).tagged


def _reuse_interfaces(tree: Mapping[str, bytes]) -> tuple[SemanticReuseInterfaceV1, ...]:
    descriptor_terms: dict[bytes, dict[str, set[str]]] = {}

    def terms_for(address: SemanticAddress) -> dict[str, set[str]]:
        key = canonical_bytes(address.model_dump(mode="json"))
        return descriptor_terms.setdefault(
            key,
            {"aliases": set(), "tags": set(), "relations": set()},
        )

    for descriptor_path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not _CLAIM_PATH_RE.fullmatch(descriptor_path):
            continue
        descriptor = parse_claim(tree[descriptor_path], path=descriptor_path)
        if descriptor.lifecycle.state != "live":
            continue
        predicate = descriptor.statement.predicate
        if predicate in {"semantic.alias", "semantic.tag"} and isinstance(
            descriptor.statement.object, LiteralClaimObject
        ):
            value = descriptor.statement.object.value
            if not isinstance(value, str):
                continue
            field = "aliases" if predicate == "semantic.alias" else "tags"
            terms_for(descriptor.statement.subject)[field].add(value)
        elif predicate in {"semantic.related_to", "semantic.distinct_from"} and isinstance(
            descriptor.statement.object, SubjectClaimObject
        ):
            relation_label = descriptor.statement.object.address.artifact_path
            terms_for(descriptor.statement.subject)["relations"].add(relation_label)
            terms_for(descriptor.statement.object.address)["relations"].add(
                descriptor.statement.subject.artifact_path
            )

    interfaces: list[SemanticReuseInterfaceV1] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        content = tree[path]
        if _CLAIM_TYPE_PATH_RE.fullmatch(path):
            claim_type = parse_claim_type(content, path=path)
            if claim_type.lifecycle.state != "live":
                continue
            signature = claim_type_structural_signature(claim_type.structure)
            tokens = tuple(
                sorted(
                    {
                        claim_type.predicate,
                        claim_type.predicate.rpartition(".")[2],
                    },
                    key=lambda item: item.encode("utf-8"),
                )
            )
            descriptors = terms_for(SemanticAddress.whole_artifact(path))
            interfaces.append(
                SemanticReuseInterfaceV1(
                    address=SemanticAddress.whole_artifact(path),
                    identity=claim_type.identity,
                    kind="claim-type",
                    label=claim_type.predicate,
                    canonical_tokens=tokens,
                    structural_signature_digest=signature,
                    aliases=tuple(
                        sorted(descriptors["aliases"], key=lambda item: item.encode("utf-8"))
                    ),
                    tags=tuple(sorted(descriptors["tags"], key=lambda item: item.encode("utf-8"))),
                    relation_labels=tuple(
                        sorted(descriptors["relations"], key=lambda item: item.encode("utf-8"))
                    ),
                )
            )
        elif _SUBJECT_PATH_RE.fullmatch(path):
            subject = parse_subject(content, path=path)
            if subject.lifecycle.state != "live":
                continue
            signature = subject_reuse_signature(subject.identity)
            descriptors = terms_for(SemanticAddress.whole_artifact(path))
            interfaces.append(
                SemanticReuseInterfaceV1(
                    address=SemanticAddress.whole_artifact(path),
                    identity=subject.identity,
                    kind="subject",
                    label=subject.identity.qualified,
                    canonical_tokens=(subject.subject_id,),
                    structural_signature_digest=signature,
                    aliases=tuple(
                        sorted(descriptors["aliases"], key=lambda item: item.encode("utf-8"))
                    ),
                    tags=tuple(sorted(descriptors["tags"], key=lambda item: item.encode("utf-8"))),
                    relation_labels=tuple(
                        sorted(descriptors["relations"], key=lambda item: item.encode("utf-8"))
                    ),
                )
            )
    return tuple(interfaces)


def _claim_type_reuse_evidence(
    *,
    claim_type: ClaimType,
    path: str,
    lookup_tree: Mapping[str, bytes],
    candidate_scope: tuple[str, ...],
    current: AcceptedProjectionCoordinate,
) -> dict[str, object]:
    signature = claim_type_structural_signature(claim_type.structure)
    predicate = claim_type.predicate
    proposal = ProposedSemanticInterfaceV1(
        address=SemanticAddress.whole_artifact(path),
        identity=claim_type.identity,
        kind="claim-type",
        label=predicate,
        canonical_tokens=tuple(
            sorted(
                {predicate, predicate.rpartition(".")[2]},
                key=lambda item: item.encode("utf-8"),
            )
        ),
        structural_signature_digest=signature,
    )
    relations: list[DistinctRelationMemberV1] = []
    for relation_path in candidate_scope:
        if not _CLAIM_PATH_RE.fullmatch(relation_path):
            continue
        relation = parse_claim(lookup_tree[relation_path], path=relation_path)
        if relation.statement.predicate != "semantic.distinct_from" or not isinstance(
            relation.statement.object, SubjectClaimObject
        ):
            continue
        relations.append(
            DistinctRelationMemberV1(
                claim_address=claim_statement_address(relation_path),
                claim_artifact_digest=claim_artifact_digest(relation).tagged,
                subject=relation.statement.subject,
                object=relation.statement.object.address,
            )
        )
    evidence = evaluate_vocabulary_reuse(
        VocabularyReuseRequestV1(
            proposal=proposal,
            hints=DiscoveryHintsV1(),
            disposition=ReuseDispositionV1(kind="new_distinct"),
        ),
        accepted_interfaces=tuple(
            item for item in _reuse_interfaces(lookup_tree) if item.address.artifact_path != path
        ),
        coordinate=AcceptedCoordinate.from_internal(current),
        implementation_digest=current.compiler.rule_digest,
        distinct_relation_members=tuple(
            sorted(
                relations,
                key=lambda item: canonical_bytes(item.model_dump(mode="json")),
            )
        ),
        descriptor_claims_available=any(
            parse_claim(lookup_tree[item], path=item).statement.predicate
            in {
                "semantic.alias",
                "semantic.distinct_from",
                "semantic.related_to",
                "semantic.tag",
            }
            for item in lookup_tree
            if _CLAIM_PATH_RE.fullmatch(item)
        ),
    )
    return evidence.model_dump(mode="json")


def _member_disposition(
    *,
    predecessor_digest: str | None,
    candidate_digest_value: str | None,
    retired: bool,
) -> Literal["create", "replace", "retire", "delete"]:
    if predecessor_digest is None:
        return "create"
    if candidate_digest_value is None:
        return "delete"
    return "retire" if retired else "replace"


def _aggregate_tier(values: list[PermissionTier]) -> PermissionTier:
    order: dict[PermissionTier, int] = {
        "governed_write": 0,
        "graph_write": 1,
        "admin": 2,
    }
    return max(values, key=order.__getitem__)


def _aggregate_activation(values: list[ActivationPolicy]) -> ActivationPolicy:
    order: dict[ActivationPolicy, int] = {
        "snapshot": 0,
        "drain": 1,
        "epoch-check": 2,
        "abort": 3,
    }
    return max(values, key=order.__getitem__)


def _claim_policy_value(
    value: LiteralClaimObject | SubjectClaimObject | ExactContentClaimObject,
) -> object:
    if isinstance(value, LiteralClaimObject):
        return value.value
    return value.model_dump(mode="json")


def _effective_claim_values(
    tree: Mapping[str, bytes],
    *,
    evaluation_time: str,
) -> dict[str, dict[str, tuple[object, ...]]]:
    """Project live Claim objects by exact Subject and predicate for policy law."""

    at = datetime.strptime(evaluation_time, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    collected: dict[str, dict[str, dict[bytes, object]]] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not _CLAIM_PATH_RE.fullmatch(path):
            continue
        claim = parse_claim(tree[path], path=path)
        if claim.lifecycle.state != "live":
            continue
        statement = claim.statement
        if statement.effective_from is not None and statement.effective_from > at:
            continue
        if statement.effective_until is not None and statement.effective_until <= at:
            continue
        value = _claim_policy_value(statement.object)
        predicate_values = collected.setdefault(statement.subject.artifact_path, {}).setdefault(
            statement.predicate,
            {},
        )
        predicate_values[canonical_bytes(value)] = value
    return {
        subject_path: {
            predicate: tuple(values[key] for key in sorted(values))
            for predicate, values in sorted(
                predicates.items(),
                key=lambda item: item[0].encode("utf-8"),
            )
        }
        for subject_path, predicates in sorted(
            collected.items(),
            key=lambda item: item[0].encode("utf-8"),
        )
    }


ClaimQueryFactsProvider = Callable[[AcceptedProjectionCoordinate], ClaimQueryFactsV1]

_CORROBORATION_BINDING_TYPES = {
    "claim_predicate": "string",
    "claim_subject_id": "string",
    "claim_subject_identity": "subject_reference",
    "claim_subject_kind": "string",
}


def _accepted_queries_by_digest(
    tree: Mapping[str, bytes],
) -> dict[str, AcceptedQueryDefinitionV1]:
    accepted: dict[str, AcceptedQueryDefinitionV1] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("query-definitions/") or not path.endswith(".json"):
            continue
        query = parse_query_definition(tree[path], path=path)
        if query.lifecycle.state != "live":
            continue
        digest = query_definition_digest(query).tagged
        if digest in accepted:
            raise ProposalIntegrityError("accepted QueryDefinition digest is not unique")
        accepted[digest] = AcceptedQueryDefinitionV1(
            path=path,
            query=query,
            artifact_digest=digest,
        )
    return accepted


def _corroboration_parameters(
    definition: AcceptedQueryDefinitionV1,
    *,
    subject: AcceptedSubject,
    predicate: str,
) -> tuple[dict[str, object] | None, tuple[str, str, str] | None]:
    values: dict[str, object] = {
        "claim_predicate": predicate,
        "claim_subject_id": subject.shell.subject_id,
        "claim_subject_identity": subject.shell.identity.qualified,
        "claim_subject_kind": subject.shell.subject_kind,
    }
    parameters: dict[str, object] = {}
    for declaration in definition.query.parameters:
        expected = _CORROBORATION_BINDING_TYPES.get(declaration.name)
        if expected is None:
            continue
        if declaration.value_type != expected:
            return None, (declaration.name, expected, declaration.value_type)
        parameters[declaration.name] = values[declaration.name]
    return parameters, None


def _run_corroboration_requirements(
    *,
    policy: ClaimAdmissionPolicyV1,
    accepted_type: AcceptedClaimType,
    subject: AcceptedSubject,
    definitions: Mapping[str, AcceptedQueryDefinitionV1],
    facts: ClaimQueryFactsV1,
    current: AcceptedProjectionCoordinate,
    timestamp: str,
) -> tuple[
    tuple[ClaimCorroborationResultV1, ...],
    tuple[tuple[str, str], ...],
]:
    evaluated_at = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    results: list[ClaimCorroborationResultV1] = []
    issues: list[tuple[str, str]] = []
    for requirement in policy.corroboration_requirements:
        definition = definitions.get(requirement.query_definition_digest)
        if definition is None:
            issues.append(
                (
                    "playbill.claim.corroboration_query_unresolved",
                    "Claim corroboration requirement "
                    f"{requirement.requirement_id!r} pins unresolved accepted "
                    f"QueryDefinition {requirement.query_definition_digest}.",
                )
            )
            continue
        parameters, invalid = _corroboration_parameters(
            definition,
            subject=subject,
            predicate=accepted_type.claim_type.predicate,
        )
        if invalid is not None:
            name, expected, declared = invalid
            issues.append(
                (
                    "playbill.claim.corroboration_parameter_contract_invalid",
                    "Claim corroboration requirement "
                    f"{requirement.requirement_id!r} declares reserved parameter {name!r} "
                    f"as {declared!r}; the daemon binding requires {expected!r}.",
                )
            )
            continue
        result = evaluate_claim_query(
            definition,
            facts=facts,
            coordinate=current,
            evaluation_time=evaluated_at,
            parameters=parameters,
        )
        receipt = query_execution_receipt(result)
        observed_count = len(result.rows)
        satisfied = result.verdict == "completed" and observed_count >= requirement.min_count
        requirement_result = ClaimCorroborationResultV1(
            requirement_id=requirement.requirement_id,
            query_definition_digest=requirement.query_definition_digest,
            parameter_digest=receipt.parameter_digest,
            result_digest=receipt.result_digest,
            query_verdict=result.verdict,
            query_refusal_code=receipt.refusal_code,
            observed_count=observed_count,
            truncated=result.truncation.truncated,
            satisfied=satisfied,
        )
        results.append(requirement_result)
        if result.verdict == "refused":
            issues.append(
                (
                    "playbill.claim.corroboration_query_refused",
                    "Claim corroboration requirement "
                    f"{requirement.requirement_id!r} ({requirement.query_definition_digest}) "
                    f"was refused by query code {receipt.refusal_code!r}.",
                )
            )
        elif not satisfied:
            issues.append(
                (
                    "playbill.claim.corroboration_insufficient",
                    "Claim corroboration requirement "
                    f"{requirement.requirement_id!r} requires {requirement.min_count} rows "
                    f"but observed {observed_count} from "
                    f"{requirement.query_definition_digest}.",
                )
            )
    return tuple(results), tuple(issues)


def _claim_admission_evaluations(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
    timestamp: str,
    subjects: Mapping[str, AcceptedSubject],
    claim_types: Mapping[str, AcceptedClaimType],
    current: AcceptedProjectionCoordinate,
    query_facts_provider: ClaimQueryFactsProvider | None,
    replay_accounts: tuple[ClaimAdmissionEvaluationAccountV1, ...] | None,
) -> tuple[
    dict[str, tuple[dict[str, object], ...]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    tuple[ClaimAdmissionEvaluationAccountV1, ...],
    tuple[CompilerDiagnostic, ...],
]:
    """Evaluate the authored ClaimType policy for each changed Claim member."""

    changed_by_subject: dict[str, list[tuple[str, ClaimArtifactAny]]] = {}
    for path in scope:
        if not _CLAIM_PATH_RE.fullmatch(path) or path not in candidate_tree:
            continue
        claim = parse_claim(candidate_tree[path], path=path)
        changed_by_subject.setdefault(claim.statement.subject.artifact_path, []).append(
            (path, claim)
        )
    if not changed_by_subject:
        return {}, {}, {}, (), ()

    parent_values = _effective_claim_values(current_tree, evaluation_time=timestamp)
    candidate_values = _effective_claim_values(candidate_tree, evaluation_time=timestamp)
    entries_by_path: dict[str, tuple[dict[str, object], ...]] = {}
    digests_by_path: dict[str, tuple[str, ...]] = {}
    query_digests_by_path: dict[str, tuple[str, ...]] = {}
    accounts: list[ClaimAdmissionEvaluationAccountV1] = []
    diagnostics: list[CompilerDiagnostic] = []
    definitions = _accepted_queries_by_digest(current_tree)
    facts: ClaimQueryFactsV1 | None = None
    for subject_path, changed_claims in sorted(
        changed_by_subject.items(),
        key=lambda item: item[0].encode("utf-8"),
    ):
        subject = subjects.get(subject_path)
        if subject is None:
            continue  # Claim law emits the exact unresolved-subject diagnostic.
        declared_predicates = tuple(
            sorted(
                {
                    item.claim_type.predicate
                    for item in claim_types.values()
                    if claim_type_accepts_subject(item.claim_type, subject.shell.subject_kind)
                },
                key=lambda item: item.encode("utf-8"),
            )
        )

        Evaluation = tuple[
            AcceptedClaimType,
            str,
            ClaimAdmissionCandidateResultV1,
            tuple[ClaimCorroborationResultV1, ...],
            tuple[tuple[str, str], ...],
            bool,
        ]

        def evaluate_type_policy(
            accepted_type: AcceptedClaimType,
            policy: ClaimAdmissionPolicyV1,
            *,
            carries_corroboration: bool,
        ) -> Evaluation:
            nonlocal facts
            policy_digest = _canonical_model_digest(
                "playbill-claim-admission-policy-v1",
                accepted_type.claim_type.admission_policy,
            )
            results: tuple[ClaimCorroborationResultV1, ...] = ()
            issues: tuple[tuple[str, str], ...] = ()
            if policy.corroboration_requirements:
                if query_facts_provider is None:
                    raise ProposalIntegrityError(
                        "Claim corroboration requires accepted query facts"
                    )
                if facts is None:
                    facts = query_facts_provider(current)
                    if facts.coordinate != current:
                        raise ProposalIntegrityError(
                            "accepted query facts coordinate differs from proposal coordinate"
                        )
                results, issues = _run_corroboration_requirements(
                    policy=policy,
                    accepted_type=accepted_type,
                    subject=subject,
                    definitions=definitions,
                    facts=facts,
                    current=current,
                    timestamp=timestamp,
                )
            context = ClaimAdmissionCandidateContextV1(
                evaluation_time=timestamp,
                declared_predicates=declared_predicates,
                parent_values=parent_values.get(subject_path, {}),
                candidate_values=candidate_values.get(subject_path, {}),
                corroboration_results=results,
            )
            return (
                accepted_type,
                policy_digest,
                evaluate_claim_admission_candidate(policy, context),
                results,
                issues,
                carries_corroboration,
            )

        # Freeze is deliberately Subject-scoped: a policy may freeze predicates
        # owned by other ClaimTypes accepting this Subject kind. Corroboration is
        # deliberately ClaimType-scoped and enters only through the authored
        # type below.
        freeze_evaluations: dict[str, Evaluation] = {}
        for freeze_type in sorted(
            claim_types.values(),
            key=lambda item: item.claim_type.identity.qualified.encode("utf-8"),
        ):
            policy = freeze_type.claim_type.admission_policy
            if not (
                claim_type_accepts_subject(
                    freeze_type.claim_type,
                    subject.shell.subject_kind,
                )
                and policy.freeze_requirements
            ):
                continue
            freeze_evaluations[freeze_type.claim_type.identity.qualified] = evaluate_type_policy(
                freeze_type,
                policy.model_copy(update={"corroboration_requirements": ()}),
                carries_corroboration=False,
            )
        authored_evaluations: dict[str, Evaluation] = {}
        for changed_path, parsed_claim in sorted(
            changed_claims,
            key=lambda item: item[0].encode("utf-8"),
        ):
            # The Claim law emits the exact unresolved or mismatched ClaimType
            # diagnostic. Corroboration belongs only to the authored type;
            # Subject-scoped freeze evaluations remain additional gates.
            claim_type_identity = parsed_claim.statement.claim_type.qualified
            accepted_type = claim_types.get(claim_type_identity)
            if accepted_type is None:
                continue
            policy = accepted_type.claim_type.admission_policy
            evaluations = dict(freeze_evaluations)
            if policy.corroboration_requirements:
                # The full authored policy evaluates its corroboration and its
                # own freeze exactly once for this (Subject, ClaimType). Replace
                # the freeze-only projection to avoid evaluating that freeze twice.
                authored = authored_evaluations.get(claim_type_identity)
                if authored is None:
                    authored = evaluate_type_policy(
                        accepted_type,
                        policy,
                        carries_corroboration=True,
                    )
                    authored_evaluations[claim_type_identity] = authored
                evaluations[claim_type_identity] = authored
            if not evaluations:
                continue
            entries: list[dict[str, object]] = []
            policy_digests: set[str] = set()
            query_digests: set[str] = set()
            for (
                governing_type,
                policy_digest,
                evaluated,
                results,
                issues,
                carries_corroboration,
            ) in sorted(
                evaluations.values(),
                key=lambda item: item[0].claim_type.identity.qualified.encode("utf-8"),
            ):
                account: ClaimAdmissionEvaluationAccountV1 | None = None
                if carries_corroboration:
                    requirements = (
                        governing_type.claim_type.admission_policy.corroboration_requirements
                    )
                    complete = len(results) == len(requirements)
                    account = ClaimAdmissionEvaluationAccountV1(
                        claim_path=changed_path,
                        claim_type_identity=governing_type.claim_type.identity.qualified,
                        claim_type_digest=governing_type.artifact_digest,
                        policy_digest=policy_digest,
                        corroboration_results=results,
                        satisfied=complete and all(item.satisfied for item in results),
                    )
                    accounts.append(account)
                    query_digests.update(item.result_digest for item in results)
                entries.append(
                    {
                        "admission_account": (
                            None if account is None else account.model_dump(mode="json")
                        ),
                        "claim_type_digest": governing_type.artifact_digest,
                        "claim_type_identity": governing_type.claim_type.identity.qualified,
                        "policy_digest": policy_digest,
                        "candidate_result": evaluated.model_dump(mode="json"),
                    }
                )
                policy_digests.add(policy_digest)
                for code, message in issues:
                    diagnostics.append(_diagnostic(code, message, changed_path))
                issue_codes = {item[0] for item in issues}
                for code in evaluated.refusal_codes:
                    if code in issue_codes:
                        continue
                    diagnostics.append(
                        _diagnostic(
                            code,
                            "A governing ClaimType admission policy refused "
                            "this closed change set.",
                            changed_path,
                        )
                    )
            entries_by_path[changed_path] = tuple(
                sorted(entries, key=lambda item: canonical_bytes(item))
            )
            digests_by_path[changed_path] = tuple(sorted(policy_digests))
            query_digests_by_path[changed_path] = tuple(sorted(query_digests))
    ordered_diagnostics = tuple(
        sorted(
            diagnostics,
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    ordered_accounts = tuple(sorted(accounts, key=claim_admission_account_order_key))
    if replay_accounts is not None:
        committed_accounts = tuple(sorted(replay_accounts, key=claim_admission_account_order_key))
        if ordered_accounts != committed_accounts:
            raise ProposalIntegrityError(
                "Claim corroboration replay account differs from accepted query re-derivation"
            )
    return (
        entries_by_path,
        digests_by_path,
        query_digests_by_path,
        ordered_accounts,
        ordered_diagnostics,
    )


@dataclass(frozen=True)
class EvaluatedTreeState:
    """One tree's derived state: its members, their trie, and its dependencies.

    Every field is a pure function of the tree's exact member bytes, and every
    field can be reached two ways: built from scratch over the whole tree, or
    advanced from the state of a predecessor tree by applying only the members
    whose bytes differ. `advance_tree_state` is the second way and is what makes
    per-generation verification cost track the change set instead of the
    instance; `build_tree_state` is the first, is what every cold start takes,
    and is the oracle the second is tested against.
    """

    members: Manifest
    merkle: MerkleManifest
    dependencies: DependencyIndexV1


def build_tree_state(tree: Mapping[str, bytes]) -> EvaluatedTreeState:
    """Derive one tree's whole state by hashing and parsing every member."""

    projected = semantic_projection(tree)
    members = manifest_for_tree(projected)
    return EvaluatedTreeState(
        members=members,
        merkle=build_merkle_manifest(members),
        dependencies=build_dependency_index(projected),
    )


@dataclass(frozen=True)
class AdvancedMembers:
    """One change set's member commitments, decided before anything is parsed.

    Advancing the manifest and its trie reads member bytes only to hash the ones
    that changed; it never asks what a member *is*. Keeping that separate from
    the dependency index is what lets a malformed member be refused as a format
    error, by the evaluator, in the order it always was -- rather than escaping
    as a parse failure from a cache update the caller never asked for.
    """

    members: Manifest
    merkle: MerkleManifest
    diff_digest: SemanticDiffDigest
    scope: tuple[str, ...]


def advance_tree_members(
    state: EvaluatedTreeState,
    *,
    previous_tree: Mapping[str, bytes],
    tree: Mapping[str, bytes],
) -> AdvancedMembers:
    """Advance a predecessor's manifest and trie over one change set.

    The semantic diff names exactly the members whose digests moved, so it is
    both the candidate's committed scope and the change set every carried index
    is updated by: one traversal decides what changed, and the manifest trie, the
    dependency index, and the scope all follow from it. The returned digest and
    scope equal `semantic_diff(previous_tree, tree)` by construction.
    """

    members = manifest_for_tree_carrying(
        semantic_projection(tree),
        previous_tree=semantic_projection(previous_tree),
        previous_manifest=state.members,
    )
    diff_digest, scope = semantic_diff_from_members(state.members, members)
    return AdvancedMembers(
        members=members,
        merkle=update_merkle_manifest(
            state.merkle,
            updated={path: members[path] for path in scope if path in members},
            removed=[path for path in scope if path not in members],
        ),
        diff_digest=diff_digest,
        scope=scope,
    )


def advance_tree_state(
    state: EvaluatedTreeState,
    *,
    tree: Mapping[str, bytes],
    advanced: AdvancedMembers,
) -> EvaluatedTreeState:
    """Complete an advanced manifest with the dependency index over the same change."""

    return EvaluatedTreeState(
        members=advanced.members,
        merkle=advanced.merkle,
        dependencies=update_dependency_index(
            state.dependencies,
            tree=semantic_projection(tree),
            changed=advanced.scope,
        ),
    )


@dataclass(frozen=True)
class _AcceptedMember:
    """One scoped member that passed its own acceptance law.

    This is the whole hand-off between a member kind's law and the candidate the
    evaluator assembles. It used to be an unnamed twelve-tuple destructured by
    position two hundred lines away from where it was built.
    """

    path: str
    artifact_kind: str
    predecessor_artifact_digest: str | None
    candidate_artifact_digest: str
    required_tier: PermissionTier
    # Historical law output retained for canonical compatibility. G12e demoted
    # role-based approval scope, so candidate construction deliberately ignores it.
    approval_scope: tuple[str, ...]
    activation_policy: ActivationPolicy
    law_identifier: str
    law_digest: str
    result: dict[str, object]
    policy_digests: tuple[str, ...] = ()
    query_receipt_digests: tuple[str, ...] = ()
    retired: bool = False


@dataclass(frozen=True)
class _MemberVerdict:
    """A member kind's answer: one accepted member, or exact refusal evidence."""

    member: _AcceptedMember | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


@dataclass(frozen=True)
class _ResolvedArtifacts:
    """Candidate-state artifacts a member law may need to read outside its own path."""

    subjects: dict[str, AcceptedSubject]
    claim_types: dict[str, AcceptedClaimType]
    capture_contracts: dict[str, AcceptedCaptureContract]
    providers: dict[str, AcceptedProviderV1]
    provider_interfaces: dict[str, AcceptedProviderInterfaceRegistrationV1]
    procedures: dict[str, AcceptedProcedureV1]


@dataclass(frozen=True)
class _MemberContext:
    """Everything one member kind's law is allowed to read, and nothing else."""

    path: str
    content: bytes
    parent_content: bytes | None
    current: AcceptedProjectionCoordinate
    scope: tuple[str, ...]
    timestamp: str
    actor_id: str | None
    principals: PrincipalRegistrySnapshot
    bodies: BodyVerifierProtocol
    promotion_verifier: ExhaustPromotionVerifierProtocol | None
    accepted_referent_coordinates: frozenset[AcceptedCoordinate]
    candidate_tree: Mapping[str, bytes]
    candidate_states: Mapping[str, ArtifactDependencyStateV1]
    candidate_identities: Mapping[str, tuple[ArtifactIdentity, str]]
    resolved: _ResolvedArtifacts
    claim_admission_by_path: Mapping[str, tuple[dict[str, object], ...]]
    claim_admission_digests_by_path: Mapping[str, tuple[str, ...]]
    claim_admission_query_digests_by_path: Mapping[str, tuple[str, ...]]
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...]
    used_expansions: set[str]

    def accepted_coordinate(self) -> AcceptedCoordinate:
        return AcceptedCoordinate.from_internal(self.current)


@dataclass(frozen=True)
class _MemberKind:
    """One registered member kind: how to recognize it and how to judge it.

    Recognition, the refusal a removal earns, the refusal a malformed member
    earns, and the law itself travel together, so adding a kind is one entry
    rather than an edit in four places, and no kind can be recognized by one part
    of the evaluator and unknown to another.

    A kind that had its own format or removal refusal when it was a single-member
    special case keeps it; a kind that never had one takes the change set's.
    """

    name: str
    pattern: re.Pattern[str]
    removal_code: str
    removal_message: str
    evaluate: Callable[[_MemberContext], _MemberVerdict]
    format_code: str = "playbill.proposal.member_format_invalid"


def _installed(artifact_tag: str) -> InstalledAcceptanceLaw:
    return PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag=artifact_tag)


def _accepted(
    context: _MemberContext,
    installed: InstalledAcceptanceLaw,
    *,
    predecessor_artifact_digest: str | None,
    candidate_artifact_digest: str,
    required_tier: PermissionTier,
    approval_scope: tuple[str, ...],
    activation_policy: ActivationPolicy,
    result: dict[str, object],
    policy_digests: tuple[str, ...] = (),
    query_receipt_digests: tuple[str, ...] = (),
    retired: bool = False,
) -> _MemberVerdict:
    return _MemberVerdict(
        member=_AcceptedMember(
            path=context.path,
            artifact_kind=installed.artifact_kind,
            predecessor_artifact_digest=predecessor_artifact_digest,
            candidate_artifact_digest=candidate_artifact_digest,
            required_tier=required_tier,
            approval_scope=approval_scope,
            activation_policy=activation_policy,
            law_identifier=installed.coordinate.identifier,
            law_digest=installed.coordinate.digest,
            result=result,
            policy_digests=policy_digests,
            query_receipt_digests=query_receipt_digests,
            retired=retired,
        )
    )


def _procedure_member(context: _MemberContext) -> _MemberVerdict:
    procedure = parse_procedure(context.content, path=context.path)
    predecessor: AcceptedProcedureV1 | None = None
    if context.parent_content is not None:
        previous = parse_procedure(context.parent_content, path=context.path)
        predecessor = AcceptedProcedureV1(
            path=context.path,
            procedure=previous,
            artifact_digest=procedure_artifact_digest(previous).tagged,
        )
    law = evaluate_procedure_law(
        procedure,
        path=context.path,
        predecessor=predecessor,
        providers={
            accepted.artifact_digest: accepted for accepted in context.resolved.providers.values()
        },
        provider_interfaces={
            accepted.artifact_digest: accepted
            for accepted in context.resolved.provider_interfaces.values()
        },
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted Procedure law result is incomplete")
    annotations = procedure.definition.annotations
    return _accepted(
        context,
        _installed(procedure.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=(),
        activation_policy=procedure.activation_policy,
        result={
            "artifact_digest": law.artifact_digest,
            "authoring_expansion": (
                annotations
                if isinstance(annotations, dict) and "builder_kind" in annotations
                else None
            ),
            "definition_digest": procedure.definition_digest,
            "directly_runnable": procedure.directly_runnable,
            "verdict": "accepted",
        },
        retired=procedure.lifecycle.state == "retired",
    )


def _exhaust_promotion_member(context: _MemberContext) -> _MemberVerdict:
    promotion = parse_exhaust_promotion(context.content, path=context.path)
    predecessor: AcceptedExhaustPromotionV1 | None = None
    if context.parent_content is not None:
        previous = parse_exhaust_promotion(context.parent_content, path=context.path)
        predecessor = AcceptedExhaustPromotionV1(
            path=context.path,
            promotion=previous,
            artifact_digest=exhaust_promotion_digest(previous),
            accepted_coordinate=context.accepted_coordinate(),
        )
    if context.promotion_verifier is None:
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    "playbill.promotion.verifier_unavailable",
                    "ExhaustPromotion evaluation requires the exact journal/reducer verifier.",
                    context.path,
                ),
            )
        )
    law = evaluate_exhaust_promotion_acceptance(
        promotion,
        path=context.path,
        predecessor=predecessor,
        operational_result=context.promotion_verifier.verify_promotion(promotion),
    )
    if law.verdict == "refused":
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    law.refusal_code or "playbill.promotion.refused",
                    law.message or "ExhaustPromotion law refused.",
                    context.path,
                ),
            )
        )
    if law.artifact_digest is None or law.required_tier is None or law.activation_policy is None:
        raise ProposalIntegrityError("accepted ExhaustPromotion law result is incomplete")
    return _accepted(
        context,
        _installed(promotion.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy=law.activation_policy,
        result=law.model_dump(mode="json"),
        retired=promotion.lifecycle.state == "retired",
    )


def _line_member(context: _MemberContext) -> _MemberVerdict:
    line = parse_line_spec(context.content, path=context.path)
    accepted_procedure = context.resolved.procedures.get(line.procedure.target.qualified)
    if accepted_procedure is None:
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    "playbill.line.procedure_unavailable",
                    "LineSpec's exact Procedure is unavailable in candidate state.",
                    context.path,
                ),
            )
        )
    predecessor: AcceptedLineSpecV1 | None = None
    if context.parent_content is not None:
        previous = parse_line_spec(context.parent_content, path=context.path)
        predecessor = AcceptedLineSpecV1(
            path=context.path,
            line=previous,
            artifact_digest=line_spec_digest(previous).tagged,
        )
    interface_digests: dict[str, str] = {}
    for state in context.candidate_states.values():
        interface_digests[state.artifact_digest] = state.artifact_digest
        interface_pin = next((pin for pin in state.pins if pin.role == "interface"), None)
        if interface_pin is not None:
            interface_digests[state.artifact_digest] = interface_pin.artifact_digest
    law = evaluate_line_spec_law(
        line,
        path=context.path,
        procedure=accepted_procedure,
        interface_digests=interface_digests,
        predecessor=predecessor,
        providers={
            accepted.artifact_digest: accepted for accepted in context.resolved.providers.values()
        },
        provider_interfaces={
            accepted.artifact_digest: accepted
            for accepted in context.resolved.provider_interfaces.values()
        },
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted LineSpec law result is incomplete")
    return _accepted(
        context,
        _installed(line.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={
            "artifact_digest": law.artifact_digest,
            "occurrence_epoch": line.occurrence_epoch,
            "procedure_digest": line.procedure.artifact_digest,
            "verdict": "accepted",
        },
        retired=line.lifecycle.state == "retired",
    )


def _literal_object_traversal(
    query: QueryDefinitionV1,
    *,
    claim_types: Mapping[str, AcceptedClaimType],
) -> CompilerDiagnostic | None:
    """Refuse a traversal whose predicate cannot carry a Subject-typed edge.

    A relation traversal walks Claim objects, so only ``object_kind="subject"``
    ClaimTypes can produce an edge. Forward traversal refused this at run time
    once a row reached it; reverse traversal simply returned nothing, so the
    definition looked healthy and answered every run with silence. The
    ClaimType is only knowable here, where the pinned artifacts are resolved,
    so this is the one place that can name it.
    """
    for step in query.traversal:
        accepted = claim_types.get(f"ClaimType:{step.predicate}")
        if accepted is None:
            # The pin law resolves every referenced ClaimType before this runs, so
            # an unresolved one here means the two disagree about what was pinned.
            raise ProposalIntegrityError(
                "QueryDefinition traversal names a ClaimType the pin closure did not "
                f"resolve: {step.predicate}"
            )
        if accepted.claim_type.object_kind == "subject":
            continue
        return CompilerDiagnostic(
            code="playbill.query_definition.traversal_object_not_subject",
            severity="error",
            message=(
                f"Traversal step {step.binding!r} walks predicate {step.predicate!r}, "
                f"whose ClaimType declares object_kind="
                f"{accepted.claim_type.object_kind!r}: relation traversal requires "
                "object_kind='subject'."
            ),
            subject=SemanticAddress.whole_artifact(accepted.path),
        )
    return None


def _query_definition_member(context: _MemberContext) -> _MemberVerdict:
    query = parse_query_definition(context.content, path=context.path)
    predecessor: AcceptedQueryDefinitionV1 | None = None
    if context.parent_content is not None:
        previous = parse_query_definition(context.parent_content, path=context.path)
        predecessor = AcceptedQueryDefinitionV1(
            path=context.path,
            query=previous,
            artifact_digest=query_definition_digest(previous).tagged,
        )
    law = evaluate_query_definition_law(
        query,
        path=context.path,
        predecessor=predecessor,
        accepted_artifacts=context.candidate_identities,
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    literal_traversal = _literal_object_traversal(query, claim_types=context.resolved.claim_types)
    if literal_traversal is not None:
        return _MemberVerdict(diagnostics=(literal_traversal,))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted QueryDefinition law result is incomplete")
    return _accepted(
        context,
        _installed(query.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={
            "artifact_digest": law.artifact_digest,
            "result_cardinality": query.result_cardinality,
            "verdict": "accepted",
        },
        retired=query.lifecycle.state == "retired",
    )


def _provider_member(context: _MemberContext) -> _MemberVerdict:
    provider = parse_provider(context.content, path=context.path)
    predecessor: AcceptedProviderV1 | None = None
    if context.parent_content is not None:
        previous = parse_provider(context.parent_content, path=context.path)
        predecessor = AcceptedProviderV1(
            path=context.path,
            provider=previous,
            artifact_digest=provider_digest(previous).tagged,
        )
    law = evaluate_provider_law(
        provider,
        path=context.path,
        predecessor=predecessor,
        interface_registrations=context.resolved.provider_interfaces,
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted Provider law result is incomplete")
    return _accepted(
        context,
        _installed(provider.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={"artifact_digest": law.artifact_digest, "verdict": "accepted"},
        retired=provider.lifecycle.state == "retired",
    )


def _provider_interface_member(context: _MemberContext) -> _MemberVerdict:
    registration = parse_provider_interface(context.content, path=context.path)
    predecessor: AcceptedProviderInterfaceRegistrationV1 | None = None
    if context.parent_content is not None:
        previous = parse_provider_interface(context.parent_content, path=context.path)
        predecessor = AcceptedProviderInterfaceRegistrationV1(
            path=context.path,
            registration=previous,
            artifact_digest=provider_interface_digest(previous).tagged,
        )
    # V1 fixture authority is compiler-shipped. The implementation unit installs
    # the catalog alongside the classifier registry; proposal evaluation receives
    # the exact accepted proof bytes through this deterministic catalog helper.
    law = evaluate_provider_interface_law(
        registration,
        path=context.path,
        predecessor=predecessor,
        conformance_fixtures={},
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted ProviderInterface law result is incomplete")
    return _accepted(
        context,
        _installed(registration.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={"artifact_digest": law.artifact_digest, "verdict": "accepted"},
        retired=registration.lifecycle.state == "retired",
    )
def _acquisition_policy_member(context: _MemberContext) -> _MemberVerdict:
    policy = parse_acquisition_policy(context.content, path=context.path)
    predecessor: AcceptedSourceAcquisitionPolicyV1 | None = None
    if context.parent_content is not None:
        previous = parse_acquisition_policy(context.parent_content, path=context.path)
        predecessor = AcceptedSourceAcquisitionPolicyV1(
            path=context.path,
            policy=previous,
            artifact_digest=acquisition_policy_digest(previous).tagged,
        )
    law = evaluate_acquisition_policy_law(
        policy,
        path=context.path,
        predecessor=predecessor,
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted SourceAcquisitionPolicy law result is incomplete")
    return _accepted(
        context,
        _installed(policy.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={"artifact_digest": law.artifact_digest, "verdict": "accepted"},
        retired=policy.lifecycle.state == "retired",
    )


def _standing_mandate_member(context: _MemberContext) -> _MemberVerdict:
    mandate = parse_standing_mandate(context.content, path=context.path)
    predecessor: AcceptedStandingMandateV1 | None = None
    if context.parent_content is not None:
        previous = parse_standing_mandate(context.parent_content, path=context.path)
        predecessor = AcceptedStandingMandateV1(
            path=context.path,
            mandate=previous,
            artifact_digest=standing_mandate_digest(previous).tagged,
        )
    law = evaluate_standing_mandate_law(
        mandate,
        path=context.path,
        predecessor=predecessor,
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted StandingMandate law result is incomplete")
    return _accepted(
        context,
        _installed(mandate.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={"artifact_digest": law.artifact_digest, "verdict": "accepted"},
        retired=mandate.lifecycle.state == "retired",
    )


def _capture_contract_member(context: _MemberContext) -> _MemberVerdict:
    contract = parse_capture_contract(context.content, path=context.path)
    predecessor: AcceptedCaptureContract | None = None
    if context.parent_content is not None:
        previous = parse_capture_contract(context.parent_content, path=context.path)
        predecessor = AcceptedCaptureContract(
            path=context.path,
            contract=previous,
            artifact_digest=capture_contract_digest(previous).tagged,
        )
    law = evaluate_capture_contract_law(
        contract,
        path=context.path,
        predecessor=predecessor,
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted CaptureContract law result is incomplete")
    return _accepted(
        context,
        _installed(contract.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={"artifact_digest": law.artifact_digest, "verdict": "accepted"},
        retired=contract.lifecycle.state == "retired",
    )


def _claim_member(context: _MemberContext) -> _MemberVerdict:
    claim = parse_claim(context.content, path=context.path)
    predecessor: AcceptedClaim | None = None
    if context.parent_content is not None:
        previous = parse_claim(context.parent_content, path=context.path)
        predecessor = AcceptedClaim(
            path=context.path,
            claim=previous,
            statement_digest=claim_statement_digest(previous.statement).tagged,
            artifact_digest=claim_artifact_digest(previous).tagged,
        )
    installed = _installed(claim.artifact_format)
    if not isinstance(context.bodies, CaptureObjectStoreProtocol):
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    "playbill.claim.capture_store_unavailable",
                    "Claim evaluation requires the managed evidence CAS.",
                    context.path,
                ),
            )
        )
    law = evaluate_claim_law(
        claim,
        path=context.path,
        principals=context.principals,
        predecessor=predecessor,
        subjects=context.resolved.subjects,
        claim_types=context.resolved.claim_types,
        capture_contracts=context.resolved.capture_contracts,
        capture_store=context.bodies,
        providers={
            identity: accepted.provider
            for identity, accepted in context.resolved.providers.items()
        },
        law_digest=installed.coordinate.digest,
        instance_id=context.current.instance_id,
        accepted_coordinate=context.accepted_coordinate(),
        accepted_referent_coordinates=context.accepted_referent_coordinates,
        evaluation_time=datetime.fromisoformat(context.timestamp.replace("Z", "+00:00")),
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted Claim law result is incomplete")
    claim_type = context.resolved.claim_types[claim.statement.claim_type.qualified].claim_type
    governing_policy_digests = {
        *context.claim_admission_digests_by_path.get(context.path, ()),
        _canonical_model_digest(
            "playbill-claim-admission-policy-v1",
            claim_type.admission_policy,
        ),
        _canonical_model_digest(
            "playbill-claim-evidence-admission-policy-v1",
            claim_type.evidence_admission_policy,
        ),
        _canonical_model_digest(
            "playbill-claim-resolution-policy-v1",
            claim_type.resolution_policy,
        ),
    }
    return _accepted(
        context,
        installed,
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={
            "artifact_digest": law.artifact_digest,
            "claim_evidence": (
                None if law.evidence is None else law.evidence.model_dump(mode="json")
            ),
            "claim_admission": list(context.claim_admission_by_path.get(context.path, ())),
            "statement_digest": law.statement_digest,
            "verdict": "accepted",
        },
        policy_digests=tuple(sorted(governing_policy_digests)),
        query_receipt_digests=context.claim_admission_query_digests_by_path.get(context.path, ()),
        retired=claim.lifecycle.state == "retired",
    )


def _claim_type_member(context: _MemberContext) -> _MemberVerdict:
    # Parseability was decided by the pre-pass, which reports this kind's own
    # format refusal; reaching a law means the member already parsed.
    claim_type = parse_claim_type(context.content, path=context.path)
    predecessor: AcceptedClaimType | None = None
    if context.parent_content is not None:
        previous = parse_claim_type(context.parent_content, path=context.path)
        predecessor = AcceptedClaimType(
            path=context.path,
            claim_type=previous,
            artifact_digest=claim_type_digest(previous).tagged,
        )
    law = evaluate_claim_type_law(
        claim_type,
        path=context.path,
        predecessor=predecessor,
        accepted_artifacts=context.candidate_identities,
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted ClaimType law result is incomplete")
    reuse: dict[str, object] | None = None
    if predecessor is None:
        reuse = _claim_type_reuse_evidence(
            claim_type=claim_type,
            path=context.path,
            lookup_tree=context.candidate_tree,
            candidate_scope=context.scope,
            current=context.current,
        )
        if reuse["verdict"] == "refused":
            return _MemberVerdict(
                diagnostics=(
                    _diagnostic(
                        str(reuse["refusal_code"]),
                        "ClaimType vocabulary reuse disposition was refused.",
                        context.path,
                    ),
                )
            )
    expansion = next(
        (
            item
            for item in context.claim_type_expansions
            if item.expanded_artifact_digest == law.artifact_digest
        ),
        None,
    )
    if expansion is not None:
        try:
            verify_claim_type_expansion_evidence(
                expansion,
                claim_type=claim_type,
                compiler_digest=context.current.compiler.rule_digest,
            )
        except AuthoringProfileError as exc:
            return _MemberVerdict(
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.profile_evidence_invalid",
                        str(exc),
                        context.path,
                    ),
                )
            )
        context.used_expansions.add(expansion.expanded_artifact_digest)
    return _accepted(
        context,
        _installed(claim_type.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={
            "artifact_digest": law.artifact_digest,
            "authoring_expansion": (
                None if expansion is None else expansion.model_dump(mode="json")
            ),
            "expanded_claim_type": claim_type.model_dump(mode="json"),
            "reuse": reuse,
            "verdict": "accepted",
        },
        policy_digests=tuple(
            sorted(
                (
                    _canonical_model_digest(
                        "playbill-claim-admission-policy-v1",
                        claim_type.admission_policy,
                    ),
                    _canonical_model_digest(
                        "playbill-claim-evidence-admission-policy-v1",
                        claim_type.evidence_admission_policy,
                    ),
                    _canonical_model_digest(
                        "playbill-claim-resolution-policy-v1",
                        claim_type.resolution_policy,
                    ),
                )
            )
        ),
        retired=claim_type.lifecycle.state == "retired",
    )


def _subject_member(context: _MemberContext) -> _MemberVerdict:
    shell = parse_subject(context.content, path=context.path)
    predecessor: AcceptedSubject | None = None
    if context.parent_content is not None:
        try:
            previous = parse_subject(context.parent_content, path=context.path)
        except SubjectFormatError as exc:
            raise ProposalIntegrityError("current accepted Subject cannot be parsed") from exc
        predecessor = AcceptedSubject(
            path=context.path,
            shell=previous,
            artifact_digest=subject_digest(previous).tagged,
        )
    law = evaluate_subject_law(
        shell,
        path=context.path,
        predecessor=predecessor,
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if law.artifact_digest is None or law.required_tier is None:
        raise ProposalIntegrityError("accepted Subject law result is incomplete")
    return _accepted(
        context,
        _installed(shell.artifact_format),
        predecessor_artifact_digest=None if predecessor is None else predecessor.artifact_digest,
        candidate_artifact_digest=law.artifact_digest,
        required_tier=law.required_tier,
        approval_scope=law.approval_scope,
        activation_policy="snapshot",
        result={"artifact_digest": law.artifact_digest, "verdict": "accepted"},
        retired=shell.lifecycle.state == "retired",
    )


def _document_member(context: _MemberContext) -> _MemberVerdict:
    shell = parse_document(context.content, path=context.path)
    predecessor: AcceptedDocument | None = None
    if context.parent_content is not None:
        try:
            previous = parse_document(context.parent_content, path=context.path)
        except DocumentFormatError as exc:  # accepted-state corruption, not author refusal
            raise ProposalIntegrityError("current accepted Document cannot be parsed") from exc
        predecessor = AcceptedDocument(
            path=context.path,
            shell=previous,
            envelope_digest=document_digest(previous).tagged,
        )
    law = evaluate_document_law(
        shell,
        path=context.path,
        bodies=context.bodies,
        predecessor=predecessor,
    )
    if law.verdict == "refused":
        return _MemberVerdict(diagnostics=tuple(law.diagnostics))
    if (
        law.envelope_digest is None or law.required_tier is None or law.activation_policy is None
    ):  # pragma: no cover - guarded by DocumentLawResult
        raise ProposalIntegrityError("accepted Document law result is incomplete")
    return _accepted(
        context,
        _installed(shell.tag),
        predecessor_artifact_digest=None if predecessor is None else predecessor.envelope_digest,
        candidate_artifact_digest=law.envelope_digest,
        required_tier=law.required_tier,
        approval_scope=(),
        activation_policy=law.activation_policy,
        result={"artifact_digest": law.envelope_digest, "verdict": "accepted"},
    )


def _approval_policy_member(context: _MemberContext) -> _MemberVerdict:
    if context.path != APPROVAL_POLICY_PATH or context.scope != (APPROVAL_POLICY_PATH,):
        return _MemberVerdict(diagnostics=(_unregistered(context.path),))
    if context.parent_content is None:
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    "playbill.approval_policy.successor_required",
                    "Approval policy is a genesis singleton and may only change by successor.",
                    context.path,
                ),
            )
        )
    try:
        policy = parse_approval_policy(context.content, path=context.path)
    except ApprovalPolicyFormatError as exc:
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    "playbill.approval_policy.format_invalid",
                    str(exc),
                    context.path,
                ),
            )
        )
    try:
        predecessor = parse_approval_policy(context.parent_content, path=context.path)
    except ApprovalPolicyFormatError as exc:
        raise ProposalIntegrityError("accepted approval policy cannot be reproduced") from exc
    if policy.mode == "independent_approval_required":
        active_ordinary_count = sum(
            record.kind == "ordinary" and record.status == "active"
            for record in context.principals.principals
        )
        if active_ordinary_count < 2:
            return _MemberVerdict(
                diagnostics=(
                    _diagnostic(
                        "playbill.approval_policy.independent_approval_minimum",
                        "independent_approval_required mode needs at least two active ordinary "
                        "principals; register the missing approver before tightening the policy.",
                        context.path,
                    ),
                )
            )
    return _accepted(
        context,
        APPROVAL_POLICY_ACCEPTANCE_LAW,
        predecessor_artifact_digest=approval_policy_digest(predecessor).tagged,
        candidate_artifact_digest=approval_policy_digest(policy).tagged,
        required_tier="governed_write",
        approval_scope=(),
        activation_policy="snapshot",
        result={
            "artifact_digest": approval_policy_digest(policy).tagged,
            "mode": policy.mode,
            "verdict": "accepted",
        },
    )


def _procedure_runtime_policy_member(context: _MemberContext) -> _MemberVerdict:
    if context.path != PROCEDURE_RUNTIME_POLICY_PATH or context.scope != (
        PROCEDURE_RUNTIME_POLICY_PATH,
    ):
        return _MemberVerdict(diagnostics=(_unregistered(context.path),))
    if not projection_registry_for_compiler(context.current.compiler).supports_artifact_kind(
        "procedure-runtime-policy"
    ):
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    "playbill.procedure_runtime_policy.compiler_unsupported",
                    "The instance compiler does not recognize ProcedureRuntimePolicy; "
                    "migrate the instance compiler before proposing this singleton.",
                    context.path,
                ),
            )
        )
    try:
        policy = parse_procedure_runtime_policy(context.content, path=context.path)
    except ProcedureRuntimePolicyFormatError as exc:
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    "playbill.procedure_runtime_policy.format_invalid",
                    str(exc),
                    context.path,
                ),
            )
        )
    predecessor_digest: str | None = None
    if context.parent_content is not None:
        try:
            predecessor = parse_procedure_runtime_policy(
                context.parent_content,
                path=context.path,
            )
        except ProcedureRuntimePolicyFormatError as exc:
            raise ProposalIntegrityError(
                "accepted Procedure runtime policy cannot be reproduced"
            ) from exc
        predecessor_digest = procedure_runtime_policy_digest(predecessor).tagged
    return _accepted(
        context,
        PROCEDURE_RUNTIME_POLICY_ACCEPTANCE_LAW,
        predecessor_artifact_digest=predecessor_digest,
        candidate_artifact_digest=procedure_runtime_policy_digest(policy).tagged,
        required_tier="governed_write",
        approval_scope=(),
        activation_policy="snapshot",
        result={
            "artifact_digest": procedure_runtime_policy_digest(policy).tagged,
            "provider_output_bytes_cap": policy.provider_output_bytes_cap,
            "verdict": "accepted",
        },
    )


def _principal_member(context: _MemberContext) -> _MemberVerdict:
    """Judge one control-plane principal transition as an ordinary scoped member.

    A principal record is not a dependency-closed artifact -- nothing pins it and
    it pins nothing -- so it reaches this law without a parsed dependency state,
    and closure has nothing to say about it. What closure cannot supply, the
    scope rule does: a principal transition is only recognized when it is the
    whole change set, exactly as it was when it had its own evaluator. Bundling a
    key rotation with artifact edits would move it out of the principal-lifecycle
    approval purpose, and that widening is refused here rather than survived.
    """

    if context.scope != (context.path,):
        return _MemberVerdict(diagnostics=(_unregistered(context.path),))
    lifecycle = evaluate_principal_lifecycle(
        candidate_content=context.content,
        parent_content=context.parent_content,
        principals=context.principals,
        candidate_tree=context.candidate_tree,
        current=context.current,
        path=context.path,
        actor_id=context.actor_id,
    )
    if lifecycle.action is None:
        return _MemberVerdict(
            diagnostics=(
                _diagnostic(
                    lifecycle.error_code or "playbill.principal.transition_refused",
                    lifecycle.error_message or "Principal transition was refused.",
                    context.path,
                ),
            )
        )
    return _accepted(
        context,
        PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW,
        predecessor_artifact_digest=(
            None if context.parent_content is None else file_digest(context.parent_content).tagged
        ),
        candidate_artifact_digest=file_digest(context.content).tagged,
        required_tier="admin",
        approval_scope=(),
        activation_policy="snapshot",
        result={
            "artifact_digest": file_digest(context.content).tagged,
            "governance_operation": lifecycle.action,
            "verdict": "accepted",
        },
        retired=lifecycle.action == "revoke",
    )


_MEMBER_KINDS: Final[tuple[_MemberKind, ...]] = (
    _MemberKind(
        name="approval-policy",
        pattern=_APPROVAL_POLICY_PATH_RE,
        removal_code="playbill.approval_policy.removal_unsupported",
        removal_message="Approval policy is a genesis singleton and cannot be removed.",
        evaluate=_approval_policy_member,
        format_code="playbill.approval_policy.format_invalid",
    ),
    _MemberKind(
        name="procedure-runtime-policy",
        pattern=_PROCEDURE_RUNTIME_POLICY_PATH_RE,
        removal_code="playbill.procedure_runtime_policy.removal_unsupported",
        removal_message=("Procedure runtime policy is a governed singleton and cannot be removed."),
        evaluate=_procedure_runtime_policy_member,
        format_code="playbill.procedure_runtime_policy.format_invalid",
    ),
    _MemberKind(
        name="procedure",
        pattern=_PROCEDURE_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_procedure_member,
    ),
    _MemberKind(
        name="exhaust-promotion",
        pattern=_EXHAUST_PROMOTION_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_exhaust_promotion_member,
    ),
    _MemberKind(
        name="line",
        pattern=_LINE_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_line_member,
    ),
    _MemberKind(
        name="query-definition",
        pattern=_QUERY_DEFINITION_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_query_definition_member,
    ),
    _MemberKind(
        name="provider",
        pattern=_PROVIDER_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_provider_member,
    ),
    _MemberKind(
        name="provider-interface",
        pattern=_PROVIDER_INTERFACE_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_provider_interface_member,
    ),
    _MemberKind(
        name="source-acquisition-policy",
        pattern=_SOURCE_ACQUISITION_POLICY_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_acquisition_policy_member,
    ),
    _MemberKind(
        name="standing-mandate",
        pattern=_STANDING_MANDATE_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_standing_mandate_member,
    ),
    _MemberKind(
        name="capture-contract",
        pattern=_CAPTURE_CONTRACT_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_capture_contract_member,
    ),
    _MemberKind(
        name="claim",
        pattern=_CLAIM_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_claim_member,
    ),
    _MemberKind(
        name="claim-type",
        pattern=_CLAIM_TYPE_PATH_RE,
        removal_code="playbill.change_set.delete_unsupported",
        removal_message="PC-A2 does not activate artifact deletion semantics.",
        evaluate=_claim_type_member,
        format_code="playbill.claim_type.format_invalid",
    ),
    _MemberKind(
        name="subject",
        pattern=_SUBJECT_PATH_RE,
        removal_code="playbill.subject.removal_unsupported",
        removal_message="Subjects are retired by successor, never removed from accepted state.",
        evaluate=_subject_member,
        format_code="playbill.subject.format_invalid",
    ),
    _MemberKind(
        name="document",
        pattern=_DOCUMENT_PATH_RE,
        removal_code="playbill.document.removal_unsupported",
        removal_message="PB-C does not activate Document removal semantics.",
        evaluate=_document_member,
        format_code="playbill.document.format_invalid",
    ),
    _MemberKind(
        name="principal",
        pattern=_PRINCIPAL_PATH_RE,
        removal_code="playbill.principal.removal_unsupported",
        removal_message="Principal records are revoked, never removed from accepted state.",
        evaluate=_principal_member,
    ),
)
ROLE_DEMOTED_MEMBER_FAMILIES: Final[tuple[str, ...]] = (
    "approval-policy",
    "procedure-runtime-policy",
    "procedure",
    "exhaust-promotion",
    "line",
    "query-definition",
    "provider",
    "provider-interface",
    "source-acquisition-policy",
    "standing-mandate",
    "capture-contract",
    "claim",
    "claim-type",
    "subject",
    "document",
    "principal",
)
if tuple(kind.name for kind in _MEMBER_KINDS) != ROLE_DEMOTED_MEMBER_FAMILIES:
    raise RuntimeError("role-demotion inventory must enumerate every candidate member family")
"""Every member kind one change set may contain, and the law that judges it.

The order is the order the evaluator tries patterns in, which is the order the
old if/elif chain tried them in, so a path that two patterns could match is
still claimed by the same kind.
"""


def _member_kind(path: str) -> _MemberKind | None:
    for kind in _MEMBER_KINDS:
        if kind.pattern.fullmatch(path):
            return kind
    return None


def _unregistered(path: str) -> CompilerDiagnostic:
    return _diagnostic(
        "playbill.proposal.unregistered_semantic_kind",
        "No PC-A2 acceptance law is registered for this changed path.",
        path,
    )


def _refused_closure(
    missing_dependents: tuple[IncompleteClosureItemV1, ...],
    unresolved_pins: tuple[UnresolvedArtifactPinV1, ...],
) -> tuple[CompilerDiagnostic, ...]:
    diagnostics: list[CompilerDiagnostic] = []
    if missing_dependents:
        diagnostics.append(
            _diagnostic(
                "playbill.change_set.incomplete_closure",
                canonical_bytes(
                    {"missing": [item.model_dump(mode="json") for item in missing_dependents]}
                ).decode("utf-8"),
            )
        )
    if unresolved_pins:
        diagnostics.append(
            _diagnostic(
                "playbill.change_set.unresolved_pin",
                canonical_bytes(
                    {"pins": [item.model_dump(mode="json") for item in unresolved_pins]}
                ).decode("utf-8"),
            )
        )
    return tuple(diagnostics)


def _resolved_artifacts(
    candidate_tree: Mapping[str, bytes],
    states: Mapping[str, ArtifactDependencyStateV1],
) -> _ResolvedArtifacts:
    """Resolve the candidate-state artifacts member laws read across paths.

    This still reads the whole candidate tree, because a Claim's ClaimType may
    live anywhere in it and nothing in the change set says where. It is the one
    remaining per-generation cost proportional to the instance rather than to the
    change, and retiring it needs a carried resolution index of its own.
    """

    resolved = _ResolvedArtifacts({}, {}, {}, {}, {}, {})
    for state in states.values():
        content = candidate_tree[state.path]
        if state.artifact_kind == "subject":
            resolved.subjects[state.path] = AcceptedSubject(
                path=state.path,
                shell=parse_subject(content, path=state.path),
                artifact_digest=state.artifact_digest,
            )
        elif state.artifact_kind == "claim-type":
            artifact = parse_claim_type(content, path=state.path)
            resolved.claim_types[artifact.identity.qualified] = AcceptedClaimType(
                path=state.path,
                claim_type=artifact,
                artifact_digest=state.artifact_digest,
            )
        elif state.artifact_kind == "capture-contract":
            contract = parse_capture_contract(content, path=state.path)
            resolved.capture_contracts[contract.identity.qualified] = AcceptedCaptureContract(
                path=state.path,
                contract=contract,
                artifact_digest=state.artifact_digest,
            )
        elif state.artifact_kind == "provider":
            provider = parse_provider(content, path=state.path)
            accepted = AcceptedProviderV1(
                path=state.path,
                provider=provider,
                artifact_digest=state.artifact_digest,
            )
            resolved.providers[provider.identity.qualified] = accepted
        elif state.artifact_kind == "provider-interface":
            registration = parse_provider_interface(content, path=state.path)
            resolved.provider_interfaces[registration.identity.qualified] = (
                AcceptedProviderInterfaceRegistrationV1(
                    path=state.path,
                    registration=registration,
                    artifact_digest=state.artifact_digest,
                )
            )
        elif state.artifact_kind == "procedure":
            procedure = parse_procedure(content, path=state.path)
            resolved.procedures[procedure.identity.qualified] = AcceptedProcedureV1(
                path=state.path,
                procedure=procedure,
                artifact_digest=state.artifact_digest,
            )
    return resolved


def _evaluate_scoped_members(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: dict[str, bytes],
    current: AcceptedProjectionCoordinate,
    bodies: BodyVerifierProtocol,
    timestamp: str,
    advanced: AdvancedMembers,
    parent: EvaluatedTreeState,
    actor_id: str | None,
    rebased: bool,
    wire_version: CandidateWireVersion,
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...],
    promotion_verifier: ExhaustPromotionVerifierProtocol | None,
    query_facts_provider: ClaimQueryFactsProvider | None,
    replay_claim_admission_accounts: tuple[ClaimAdmissionEvaluationAccountV1, ...] | None,
) -> CandidateEvaluation:
    """Judge every scoped member under its own law and close the change set.

    One pipeline, in one order: refuse a member this build cannot parse, close
    the dependency graph, then dispatch each scoped path to the single law
    registered for its kind and assemble what they returned. There is no second
    evaluator and no single-member special case -- a change set of one Document
    is a change set, judged by the Document law, exactly as a change set of two
    hundred Claims is judged by the Claim law.
    """

    scope = advanced.scope
    diff_digest = advanced.diff_digest
    for path in scope:
        proposed = candidate_tree.get(path)
        kind = _member_kind(path)
        if proposed is None or kind is None:
            continue
        try:
            if any(pattern.fullmatch(path) for pattern in _SEMANTIC_MEMBER_PATTERNS):
                dependency_artifacts({path: proposed})
        except ClaimTypeFreshnessHorizonInvalid as exc:
            return CandidateEvaluation(
                candidate_tree,
                None,
                (
                    _diagnostic(
                        "playbill.claim_type.freshness_horizon_invalid",
                        str(exc),
                        path,
                    ),
                ),
                rebased,
            )
        except (
            ApprovalPolicyFormatError,
            CaptureFormatError,
            ClaimFormatError,
            DocumentFormatError,
            ProviderFormatError,
            ProviderInterfaceFormatError,
            SourceAcquisitionPolicyError,
            StandingMandateError,
            SubjectFormatError,
            ClaimTypeFormatError,
            ProcedureFormatError,
            LineSpecFormatError,
            QueryDefinitionFormatError,
            ExhaustPromotionError,
        ) as exc:
            return CandidateEvaluation(
                candidate_tree,
                None,
                (_diagnostic(kind.format_code, str(exc), path),),
                rebased,
            )

    # Only now, with every scoped member proven parseable, is the carried
    # dependency index advanced: a malformed member must be refused as one.
    candidate_state = advance_tree_state(parent, tree=candidate_tree, advanced=advanced)
    candidate_dependencies = candidate_state.dependencies
    # The v1 candidate predates dependency closure entirely, and re-verifying an
    # accepted v1 generation may not apply a law its own acceptance never faced:
    # a Subject that had live dependents outside its change set was acceptable
    # then, so replaying it must find it acceptable now.
    facts = (
        None
        if wire_version == "playbill-validated-candidate-v1"
        else judge_dependency_closure(
            parent=parent.dependencies,
            candidate=candidate_dependencies,
            scope=scope,
        )
    )
    if facts is not None and facts.verdict == "refused":
        return CandidateEvaluation(
            candidate_tree,
            None,
            _refused_closure(
                facts.missing_dependents,
                facts.unresolved_pins,
            ),
            rebased,
        )

    principals = principal_registry_from_tree(current_tree, semantic_root=current.semantic_root)
    if actor_id is not None:
        try:
            principals.require_active(actor_id)
        except PrincipalIntegrityError:
            return CandidateEvaluation(
                candidate_tree,
                None,
                (
                    _diagnostic(
                        "playbill.proposal.creator_principal_invalid",
                        "Authenticated actor does not resolve to an active Principal at the "
                        "accepted coordinate.",
                        scope[0],
                    ),
                ),
                rebased,
            )
    candidate_states = candidate_dependencies.states
    resolved = _resolved_artifacts(candidate_tree, candidate_states)
    claim_admission_by_path: dict[str, tuple[dict[str, object], ...]] = {}
    claim_admission_digests_by_path: dict[str, tuple[str, ...]] = {}
    claim_admission_query_digests_by_path: dict[str, tuple[str, ...]] = {}
    claim_admission_accounts: tuple[ClaimAdmissionEvaluationAccountV1, ...] = ()
    diagnostics: list[CompilerDiagnostic] = []
    for path in scope:
        parent_content = current_tree.get(path)
        candidate_content = candidate_tree.get(path)
        if parent_content is None or candidate_content is None or not path.startswith("claims/"):
            continue
        previous_claim = parse_claim(parent_content, path=path)
        candidate_claim = parse_claim(candidate_content, path=path)
        if not isinstance(candidate_claim, ClaimArtifactV3):
            continue
        for previous_pin, candidate_pin in claim_retirement_pin_digest_updates(
            candidate_claim,
            predecessor=previous_claim,
        ):
            target_path = candidate_dependencies.paths_by_identity.get(
                candidate_pin.target.qualified
            )
            target = None if target_path is None else candidate_states.get(target_path)
            parent_target_path = parent.dependencies.paths_by_identity.get(
                previous_pin.target.qualified
            )
            parent_target = (
                None
                if parent_target_path is None
                else parent.dependencies.states.get(parent_target_path)
            )
            # Exact parent/candidate artifact correspondence below implies
            # same-ChangeSet membership because scope is the canonical tree
            # diff. A separate target_path-in-scope predicate was redundant
            # and could not fail independently of these digest equalities.
            if (
                target is None
                or target.artifact_kind != "claim"
                or target.lifecycle.state != "retired"
                or target.lifecycle.predecessor_digest != previous_pin.artifact_digest
                or target.artifact_digest != candidate_pin.artifact_digest
                or parent_target is None
                or parent_target.artifact_digest != previous_pin.artifact_digest
            ):
                diagnostics.append(
                    _diagnostic(
                        "playbill.claim.retirement_pin_delta_invalid",
                        "A retirement may update a Claim-target pin digest only when the "
                        "exact target retires by one succession hop in the same complete "
                        "ChangeSet.",
                        path,
                    )
                )
    if actor_id is not None:
        (
            claim_admission_by_path,
            claim_admission_digests_by_path,
            claim_admission_query_digests_by_path,
            claim_admission_accounts,
            claim_admission_diagnostics,
        ) = _claim_admission_evaluations(
            current_tree=current_tree,
            candidate_tree=candidate_tree,
            scope=scope,
            timestamp=timestamp,
            subjects=resolved.subjects,
            claim_types=resolved.claim_types,
            current=current,
            query_facts_provider=query_facts_provider,
            replay_accounts=replay_claim_admission_accounts,
        )
        diagnostics.extend(claim_admission_diagnostics)

    used_expansions: set[str] = set()
    accepted: list[_AcceptedMember] = []
    for path in scope:
        kind = _member_kind(path)
        proposed = candidate_tree.get(path)
        if proposed is None:
            # A removal is refused by the kind that owns the path, so the member
            # law states what its own artifacts may never do; a path no kind
            # claims was never acceptable to remove either.
            diagnostics.append(
                _unregistered(path)
                if kind is None
                else _diagnostic(kind.removal_code, kind.removal_message, path)
            )
            continue
        if kind is None:
            diagnostics.append(_unregistered(path))
            continue
        verdict = kind.evaluate(
            _MemberContext(
                path=path,
                content=proposed,
                parent_content=current_tree.get(path),
                current=current,
                scope=scope,
                timestamp=timestamp,
                actor_id=actor_id,
                principals=principals,
                bodies=bodies,
                promotion_verifier=promotion_verifier,
                accepted_referent_coordinates=accepted_referent_coordinates_from_tree(
                    current_tree,
                    current=AcceptedCoordinate.from_internal(current),
                ),
                candidate_tree=candidate_tree,
                candidate_states=candidate_states,
                candidate_identities={
                    item.identity.qualified: (item.identity, item.artifact_digest)
                    for item in candidate_states.values()
                },
                resolved=resolved,
                claim_admission_by_path=claim_admission_by_path,
                claim_admission_digests_by_path=claim_admission_digests_by_path,
                claim_admission_query_digests_by_path=(claim_admission_query_digests_by_path),
                claim_type_expansions=claim_type_expansions,
                used_expansions=used_expansions,
            )
        )
        diagnostics.extend(verdict.diagnostics)
        if verdict.member is not None:
            accepted.append(verdict.member)

    if any(item.expanded_artifact_digest not in used_expansions for item in claim_type_expansions):
        diagnostics.append(
            _diagnostic(
                "playbill.claim_type.profile_output_mismatch",
                "ClaimType profile evidence does not bind any expanded candidate artifact.",
            )
        )
    if diagnostics:
        return CandidateEvaluation(
            candidate_tree,
            None,
            tuple(diagnostics),
            rebased,
            claim_admission_accounts=claim_admission_accounts,
        )
    if tuple(item.path for item in accepted) != scope:
        raise ProposalIntegrityError("evaluator did not cover every scoped member")
    approval_requirements = _approval_requirements(current_tree)
    if wire_version == "playbill-validated-candidate-v1":
        record: CandidateRecordAnyVersion = _candidate_record_v1(
            accepted,
            candidate_tree=candidate_tree,
            current=current,
            scope=scope,
            diff_digest=diff_digest,
            manifest_root_value=manifest_root_from_members(candidate_state.members).tagged,
            timestamp=timestamp,
            approval_requirements=approval_requirements,
        )
    elif facts is None:  # pragma: no cover - only v1 skips closure
        raise ProposalIntegrityError("multi-member candidate requires a closure judgement")
    elif wire_version == "playbill-validated-candidate-v2":
        record = _candidate_record_v2(
            accepted,
            closure=closure_evaluation_v2(facts),
            current=current,
            scope=scope,
            diff_digest=diff_digest,
            manifest_root_value=manifest_root_from_members(candidate_state.members).tagged,
            timestamp=timestamp,
            approval_requirements=approval_requirements,
        )
    else:
        record = _candidate_record_v3(
            accepted,
            closure=closure_evaluation_v3(facts),
            current=current,
            scope=scope,
            diff_digest=diff_digest,
            manifest_root_value=candidate_state.merkle.root.tagged,
            timestamp=timestamp,
            approval_requirements=approval_requirements,
        )
    return CandidateEvaluation(
        candidate_tree,
        record,
        (),
        rebased,
        candidate_state,
        claim_admission_accounts,
    )


def _multi_member_evidence(
    accepted: list[_AcceptedMember],
    *,
    closure: ClosureEvaluationV2 | ClosureEvaluationV3,
    current: AcceptedProjectionCoordinate,
    timestamp: str,
) -> tuple[tuple[CandidateMemberLawEvidenceV2, ...], tuple[MemberLawEvaluationV2, ...]]:
    """Render what the member laws returned as the member/law evidence pair.

    The evidence shape is the same on both sides of the succession -- only the
    candidate's manifest root and the closure proof's graph commitment move -- so
    it is rendered once and both record versions carry the identical bytes.
    """

    law_evidence: list[MemberLawEvaluationV2] = []
    members: list[CandidateMemberLawEvidenceV2] = []
    for item in accepted:
        proofs = closure.proofs_for(item.path)
        evidence = MemberLawEvaluationV2(
            path=item.path,
            law_identifier=item.law_identifier,
            law_digest=item.law_digest,
            evaluation_time=timestamp,
            evaluation_coordinate=LawEvaluationCoordinateV1(
                git_oid=current.git_oid,
                semantic_root=current.semantic_root,
                generation_root=current.generation_root,
                compiler_digest=current.compiler.rule_digest,
            ),
            dependency_proof_refs=proofs,
            policy_digests=item.policy_digests,
            query_receipt_digests=item.query_receipt_digests,
            result=item.result,
        )
        law_evidence.append(evidence)
        disposition = _member_disposition(
            predecessor_digest=item.predecessor_artifact_digest,
            candidate_digest_value=item.candidate_artifact_digest,
            retired=item.retired,
        )
        members.append(
            CandidateMemberLawEvidenceV2(
                path=item.path,
                artifact_kind=item.artifact_kind,
                disposition=disposition,
                predecessor_artifact_digest=item.predecessor_artifact_digest,
                candidate_artifact_digest=item.candidate_artifact_digest,
                law_identifier=item.law_identifier,
                law_digest=item.law_digest,
                law_evidence_digest=member_law_evidence_digest(evidence),
                closure_role="invalidation" if disposition == "retire" else "authored",
                dependency_proof_refs=proofs,
            )
        )
    return tuple(members), tuple(law_evidence)


def _approval_requirements(
    current_tree: Mapping[str, bytes],
) -> tuple[ApprovalRequirement, ...]:
    content = current_tree.get(APPROVAL_POLICY_PATH)
    if content is None:
        raise ProposalIntegrityError("accepted state is missing its governed approval policy")
    try:
        policy = parse_approval_policy(content, path=APPROVAL_POLICY_PATH)
    except ApprovalPolicyFormatError as exc:
        raise ProposalIntegrityError("accepted approval policy cannot be reproduced") from exc
    if policy.mode == "independent_approval_required":
        return INDEPENDENT_APPROVAL_REQUIREMENTS
    return ()


def _law_digests(accepted: list[_AcceptedMember]) -> dict[str, str]:
    return {
        identifier: digest
        for identifier, digest in sorted(
            {(item.law_identifier, item.law_digest) for item in accepted},
            key=lambda item: item[0].encode("utf-8"),
        )
    }


def _candidate_record_v3(
    accepted: list[_AcceptedMember],
    *,
    closure: ClosureEvaluationV3,
    current: AcceptedProjectionCoordinate,
    scope: tuple[str, ...],
    diff_digest: SemanticDiffDigest,
    manifest_root_value: str,
    timestamp: str,
    approval_requirements: tuple[ApprovalRequirement, ...],
) -> CandidateRecordV3:
    """Assemble the candidate this build produces: merkle root, edge root."""

    semantic_candidate = SemanticCandidateV2(
        parent_semantic_root=current.semantic_root,
        candidate_manifest_root=manifest_root_value,
        semantic_diff_digest=diff_digest.tagged,
        scope=scope,
        timestamp=timestamp,
    )
    members, law_evidence = _multi_member_evidence(
        accepted,
        closure=closure,
        current=current,
        timestamp=timestamp,
    )
    return CandidateRecordV3(
        candidate=semantic_candidate,
        candidate_digest=candidate_digest(semantic_candidate).tagged,
        required_tier=_aggregate_tier([item.required_tier for item in accepted]),
        approval_requirements=approval_requirements,
        activation_policy=_aggregate_activation([item.activation_policy for item in accepted]),
        closure_proof=ClosureProofV3(
            paths=scope,
            dependency_edge_root=closure.dependency_edge_root,
            member_evidence_digest=candidate_member_evidence_digest(members),
        ),
        members=members,
        law_evidence=law_evidence,
        law_digests=_law_digests(accepted),
        compiler_digest=current.compiler.rule_digest,
    )


def _candidate_record_v2(
    accepted: list[_AcceptedMember],
    *,
    closure: ClosureEvaluationV2,
    current: AcceptedProjectionCoordinate,
    scope: tuple[str, ...],
    diff_digest: SemanticDiffDigest,
    manifest_root_value: str,
    timestamp: str,
    approval_requirements: tuple[ApprovalRequirement, ...],
) -> CandidateRecordV2:
    """Reproduce the candidate an accepted v2 generation was judged against.

    Nothing produces one of these for a new proposal. It exists so that replaying
    a generation settled before the succession re-derives the exact object that
    generation's receipt carries, rather than a newer object that would have to
    be compared loosely.
    """

    semantic_candidate = SemanticCandidate(
        parent_semantic_root=current.semantic_root,
        candidate_manifest_root=manifest_root_value,
        semantic_diff_digest=diff_digest.tagged,
        scope=scope,
        timestamp=timestamp,
    )
    members, law_evidence = _multi_member_evidence(
        accepted,
        closure=closure,
        current=current,
        timestamp=timestamp,
    )
    return CandidateRecordV2(
        candidate=semantic_candidate,
        candidate_digest=candidate_digest(semantic_candidate).tagged,
        required_tier=_aggregate_tier([item.required_tier for item in accepted]),
        approval_requirements=approval_requirements,
        activation_policy=_aggregate_activation([item.activation_policy for item in accepted]),
        closure_proof=ClosureProofV2(
            paths=scope,
            dependency_graph_digest=closure.dependency_graph_digest,
            member_evidence_digest=candidate_member_evidence_digest(members),
        ),
        members=members,
        law_evidence=law_evidence,
        law_digests=_law_digests(accepted),
        compiler_digest=current.compiler.rule_digest,
    )


_V1_MEMBER_KINDS: Final = frozenset({"document", "subject", "principal-lifecycle"})
_V1_GOVERNANCE_DISPOSITIONS: Final[dict[str, MutationDisposition]] = {
    "register": "replacement",
    "rotate": "hand-authored-successor",
    "recover": "hand-authored-successor",
    "revoke": "invalidation",
}


def _candidate_record_v1(
    accepted: list[_AcceptedMember],
    *,
    candidate_tree: Mapping[str, bytes],
    current: AcceptedProjectionCoordinate,
    scope: tuple[str, ...],
    diff_digest: SemanticDiffDigest,
    manifest_root_value: str,
    timestamp: str,
    approval_requirements: tuple[ApprovalRequirement, ...],
) -> CandidateRecord:
    """Reproduce the candidate an accepted v1 generation was judged against.

    A v1 candidate is always one member of one of three kinds, records that
    member by its exact file digest rather than by its artifact digest, and names
    the transition in the governance vocabulary of the time. Every one of those
    is a property of the object accepted history holds, so they are reproduced
    here rather than translated.
    """

    if len(accepted) != 1 or accepted[0].artifact_kind not in _V1_MEMBER_KINDS:
        raise ProposalIntegrityError("a v1 candidate carries exactly one governed member")
    item = accepted[0]
    content = candidate_tree[item.path]
    operation = item.result.get("governance_operation")
    disposition: MutationDisposition
    if isinstance(operation, str):
        disposition = _V1_GOVERNANCE_DISPOSITIONS[operation]
    else:
        disposition = (
            "replacement" if item.predecessor_artifact_digest is None else "hand-authored-successor"
        )
    semantic_candidate = SemanticCandidate(
        parent_semantic_root=current.semantic_root,
        candidate_manifest_root=manifest_root_value,
        semantic_diff_digest=diff_digest.tagged,
        scope=scope,
        timestamp=timestamp,
    )
    return CandidateRecord(
        candidate=semantic_candidate,
        candidate_digest=candidate_digest(semantic_candidate).tagged,
        required_tier=item.required_tier,
        approval_requirements=approval_requirements,
        activation_policy=item.activation_policy,
        closure_paths=scope,
        members=(
            CandidateMemberEvidence(
                path=item.path,
                artifact_kind=item.artifact_kind,
                artifact_digest=file_digest(content).tagged,
                disposition=disposition,
                law_identifier=item.law_identifier,
                governance_operation=operation if isinstance(operation, str) else None,
            ),
        ),
        law_digests=_law_digests(accepted),
        compiler_digest=current.compiler.rule_digest,
    )


def evaluate_proposal_tree(
    *,
    base_tree: Mapping[str, bytes],
    current_tree: Mapping[str, bytes],
    proposed_tree: Mapping[str, bytes],
    current: AcceptedProjectionCoordinate,
    bodies: BodyVerifierProtocol,
    timestamp: str,
    rebased: bool,
    actor_id: str | None = None,
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...] = (),
    promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    parent_state: EvaluatedTreeState | None = None,
    wire_version: CandidateWireVersion = PRODUCED_CANDIDATE_VERSION,
    query_facts_provider: ClaimQueryFactsProvider | None = None,
    replay_claim_admission_accounts: tuple[ClaimAdmissionEvaluationAccountV1, ...] | None = None,
) -> CandidateEvaluation:
    """Rebase, scope, judge every member, and close: the whole evaluation.

    `wire_version` names the candidate shape to speak the judgement in. A new
    proposal never passes it: this build produces one version. Re-verifying an
    accepted generation passes the version that generation's own receipt carries,
    so the reproduced candidate is comparable to the recorded one object for
    object rather than approximately.

    `parent_state` is the accepted parent's already-derived state -- its member
    manifest, its manifest trie, and its dependency index. A caller that holds
    one, which replay does for every generation after the first, hands it in and
    the evaluation advances it over the change set instead of rebuilding it; a
    caller that does not, which settlement never can, passes nothing and the
    state is built from scratch. Both reach the same candidate: the carried state
    is a cache of derivations, never a source of them.
    """

    candidate_tree = dict(proposed_tree)
    if rebased:
        _original_diff, original_scope = semantic_diff(base_tree, proposed_tree)
        if len(original_scope) > 1 or any(
            any(pattern.fullmatch(path) for pattern in _DEPENDENCY_CLOSED_PATTERNS)
            for path in original_scope
        ):
            result = deterministic_rebase_v2(
                old_parent_tree=base_tree,
                new_parent_tree=current_tree,
                proposed_tree=proposed_tree,
            )
            candidate_tree = result.tree
            if result.conflicts:
                return CandidateEvaluation(
                    candidate_tree,
                    None,
                    tuple(
                        _diagnostic(
                            conflict.code,
                            canonical_bytes(conflict.model_dump(mode="json")).decode("utf-8"),
                            conflict.path,
                        )
                        for conflict in result.conflicts
                    ),
                    True,
                )
        else:
            candidate_tree, conflicts = deterministic_rebase(
                base_tree=base_tree,
                current_tree=current_tree,
                proposed_tree=proposed_tree,
            )
            if conflicts:
                return CandidateEvaluation(
                    candidate_tree,
                    None,
                    tuple(
                        _diagnostic(
                            "playbill.proposal.rebase_conflict",
                            "The accepted artifact changed incompatibly after the proposed base.",
                            path,
                        )
                        for path in conflicts
                    ),
                    True,
                )

    parent = parent_state if parent_state is not None else build_tree_state(current_tree)
    advanced = advance_tree_members(parent, previous_tree=current_tree, tree=candidate_tree)
    if not advanced.scope:
        return CandidateEvaluation(
            candidate_tree,
            None,
            (
                _diagnostic(
                    "playbill.proposal.non_singleton_scope",
                    "The proposal changes no registered semantic member.",
                ),
            ),
            rebased,
        )
    return _evaluate_scoped_members(
        current_tree=current_tree,
        candidate_tree=candidate_tree,
        current=current,
        bodies=bodies,
        timestamp=timestamp,
        advanced=advanced,
        parent=parent,
        actor_id=actor_id,
        rebased=rebased,
        wire_version=wire_version,
        claim_type_expansions=claim_type_expansions,
        promotion_verifier=promotion_verifier,
        query_facts_provider=query_facts_provider,
        replay_claim_admission_accounts=replay_claim_admission_accounts,
    )


def _proposal_id_payload(
    *,
    actor_id: str,
    request: ProposalAdmissionRequest,
    candidate_commit_oid: str,
    candidate_tree_oid: str,
    admitted_at: str,
    limits: ProposalReceiveLimits,
) -> str:
    return ProposalDigest(
        canonical_digest(
            "playbill-proposal-admission-v1",
            {
                "actor_id": actor_id,
                "target_ref": request.target_ref,
                "proposed_base_oid": request.proposed_base_oid,
                "candidate_commit_oid": candidate_commit_oid,
                "candidate_tree_oid": candidate_tree_oid,
                "source_compilation_digest": request.source_compilation_digest,
                "claim_type_expansions": [
                    item.model_dump(mode="json") for item in request.claim_type_expansions
                ],
                "limits": limits.model_dump(mode="json"),
                "admitted_at": admitted_at,
            },
        )
    ).tagged


class ProposalService:
    """Daemon-only PB-C proposal service; it never updates main or accepted state."""

    def __init__(
        self,
        transport: ProposalTransportProtocol,
        *,
        accepted: AcceptedProjectionCoordinate,
        bodies: BodyVerifierProtocol,
        evidence: ProposalEvidenceProtocol,
        receive_limits: ProposalReceiveLimits = ProposalReceiveLimits(),
        current_coordinate: Callable[[], AcceptedProjectionCoordinate] | None = None,
        promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
        query_facts_provider: ClaimQueryFactsProvider | None = None,
        workspace_advertiser: Callable[[], PlaybillWorkspaceAdvertisement] | None = None,
    ) -> None:
        self.transport = transport
        self.accepted = accepted
        self.bodies = bodies
        self.evidence = evidence
        self.receive_limits = receive_limits
        self._current_coordinate = current_coordinate or (lambda: accepted)
        self.promotion_verifier = promotion_verifier
        self.query_facts_provider = query_facts_provider
        self.workspace_advertiser = workspace_advertiser

    def submit(
        self,
        *,
        actor: AuthenticatedActor,
        request: ProposalAdmissionRequest,
        candidate_tree: Mapping[str, bytes],
        timestamp: str,
    ) -> ProposalResult:
        validate_candidate_timestamp(timestamp)
        if "propose" not in actor.capabilities:
            raise ProposalAdmissionError("authenticated actor lacks the propose capability")
        current = self._current_coordinate()
        if current.compiler != current_compiler_coordinate():
            raise PlaybillReseedRequired()
        namespace = request.target_ref.split("/")[2]
        if namespace != actor.actor_id:
            raise ProposalAdmissionError(
                "proposal ref namespace differs from the authenticated actor"
            )
        expected_oid_length = 40 if self.transport.object_format() == "sha1" else 64
        if len(request.proposed_base_oid) != expected_oid_length:
            raise ProposalAdmissionError("proposed base OID length differs from object format")
        if (
            current.instance_id != self.accepted.instance_id
            or current.repository_path != self.accepted.repository_path
            or current.git_object_format != self.accepted.git_object_format
        ):
            raise ProposalAdmissionError("current coordinate names a different instance ledger")
        if current.git_oid == self.accepted.git_oid and current != self.accepted:
            raise ProposalAdmissionError("current coordinate contradicts the verified base")
        if self.transport.read_main() != current.git_oid:
            raise ProposalAdmissionError("current coordinate is not the accepted main ref")
        current_tree = self.transport.read_tree(current.git_oid)
        try:
            principal_registry_from_tree(
                current_tree,
                semantic_root=current.semantic_root,
            ).require_active(actor.actor_id)
        except PrincipalIntegrityError as exc:
            raise ProposalAdmissionError(
                "playbill.proposal.creator_principal_invalid: authenticated actor does not "
                "resolve to an active Principal at the accepted coordinate"
            ) from exc

        base_tree = self.transport.read_tree(request.proposed_base_oid)
        validated_tree = validate_proposal_tree(
            candidate_tree,
            limits=self.receive_limits,
            base_tree=base_tree,
        )
        existing = self.transport.read_proposal_ref(request.target_ref)
        commit_oid, tree_oid = self.transport.create_proposal_commit(
            validated_tree,
            base_oid=request.proposed_base_oid,
            target_ref=request.target_ref,
            actor_id=actor.actor_id,
            timestamp=timestamp,
            expected_ref_oid=existing,
        )
        proposal_id = _proposal_id_payload(
            actor_id=actor.actor_id,
            request=request,
            candidate_commit_oid=commit_oid,
            candidate_tree_oid=tree_oid,
            admitted_at=timestamp,
            limits=self.receive_limits,
        )
        admission = ProposalAdmissionRecord(
            proposal_id=proposal_id,
            actor_id=actor.actor_id,
            target_ref=request.target_ref,
            proposed_base_oid=request.proposed_base_oid,
            candidate_commit_oid=commit_oid,
            candidate_tree_oid=tree_oid,
            source_compilation_digest=request.source_compilation_digest,
            claim_type_expansions=request.claim_type_expansions,
            limits=self.receive_limits,
            admitted_at=timestamp,
        )
        self.evidence.write_admission(admission)

        is_rebase = current.git_oid != request.proposed_base_oid
        outcome = evaluate_proposal_tree(
            base_tree=base_tree,
            current_tree=current_tree,
            proposed_tree=validated_tree,
            current=current,
            bodies=self.bodies,
            timestamp=timestamp,
            rebased=is_rebase,
            actor_id=actor.actor_id,
            claim_type_expansions=request.claim_type_expansions,
            promotion_verifier=self.promotion_verifier,
            query_facts_provider=self.query_facts_provider,
        )

        evaluated_tree_oid: str | None = tree_oid
        if outcome.candidate is not None and is_rebase:
            commit_oid, evaluated_tree_oid = self.transport.create_proposal_commit(
                outcome.tree,
                base_oid=current.git_oid,
                target_ref=request.target_ref,
                actor_id=actor.actor_id,
                timestamp=timestamp,
                expected_ref_oid=commit_oid,
            )
        candidate_value = outcome.candidate.candidate_digest if outcome.candidate else None
        try:
            evaluation = ProposalEvaluationRecord(
                proposal_id=proposal_id,
                verdict="candidate" if outcome.candidate is not None else "refused",
                evaluated_base_oid=current.git_oid,
                evaluated_tree_oid=evaluated_tree_oid if outcome.candidate is not None else None,
                rebased=is_rebase,
                candidate_digest=candidate_value,
                diagnostics=outcome.diagnostics,
                claim_admission_accounts=outcome.claim_admission_accounts,
                evaluated_at=timestamp,
            )
        except ValidationError as exc:
            raise ProposalEvaluationIntegrityError(
                "proposal evaluation record failed deterministic validation"
            ) from exc
        self.evidence.write_evaluation(evaluation)
        if outcome.candidate is not None:
            self.evidence.write_candidate(outcome.candidate)
        if self.transport.read_main() != current.git_oid:
            raise ProposalIntegrityError("proposal evaluation changed or raced accepted main")
        if self.workspace_advertiser is None:
            advertisement = NOT_ATTACHED_ADVERTISEMENT
        else:
            try:
                advertisement = self.workspace_advertiser()
            except BaseException:
                advertisement = PlaybillWorkspaceAdvertisement(
                    status="failed",
                    workspace_path=None,
                    failure_code="unexpected_failure",
                )
        return ProposalResult(
            admission=admission,
            evaluation=evaluation,
            candidate=outcome.candidate,
            workspace_advertisement=advertisement,
        )


__all__ = [
    "AuthenticatedActor",
    "CandidateEvaluation",
    "ProposalAdmissionRecord",
    "ProposalAdmissionRequest",
    "ProposalEvaluationRecord",
    "ProposalEvidenceProtocol",
    "ProposalReceiveLimits",
    "ProposalResult",
    "ProposalService",
    "ProposalTransportProtocol",
    "ExhaustPromotionVerifierProtocol",
    "claim_type_expansions_from_candidate",
    "deterministic_rebase",
    "deterministic_rebase_v2",
    "evaluate_proposal_tree",
    "validate_proposal_tree",
]
