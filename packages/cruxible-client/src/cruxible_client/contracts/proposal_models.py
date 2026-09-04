"""Pure proposal admission, evaluation, and transport contracts."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.actor_types import TransportCapability
from cruxible_client.contracts.authoring_profiles import ClaimTypeExpansionEvidenceV1
from cruxible_client.contracts.candidates import (
    CandidateRecordAnyVersion,
    validate_candidate_timestamp,
)
from cruxible_client.contracts.canonical import (
    CandidateDigest,
    ProposalDigest,
    Sha256Value,
    canonical_bytes,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.policies import ClaimAdmissionEvaluationAccountV1
from cruxible_client.contracts.types import GitObjectFormat
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    PlaybillWorkspaceAdvertisement,
)

_ACTOR_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PROPOSAL_REF_RE = re.compile(r"^refs/proposals/[a-z][a-z0-9_.-]{0,127}/[a-z][a-z0-9_.-]{0,127}$")
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def claim_admission_account_order_key(
    account: ClaimAdmissionEvaluationAccountV1,
) -> bytes:
    """Return the one canonical ordering key for persisted admission accounts."""

    return canonical_bytes(
        [
            account.claim_path,
            account.claim_type_identity,
            account.policy_digest,
        ]
    )


def canonical_proposal_ref_name(display_name: str) -> str:
    """Lower a human display label into the closed proposal-ref grammar."""

    normalized = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9_.-]+", "-", normalized.strip().lower()).strip("._-")
    if not slug:
        raise ValueError("proposal display name has no canonical ref characters")
    if not slug[0].isalpha():
        slug = "proposal-" + slug
    if len(slug) > 128:
        suffix = hashlib.sha256(display_name.encode("utf-8")).hexdigest()[:12]
        slug = slug[: 128 - len(suffix) - 1].rstrip("._-") + "-" + suffix
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", slug):
        raise ValueError("proposal display name cannot be lowered into ref grammar")
    return slug


class _StrictProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthenticatedActor(_StrictProposalModel):
    """Identity established by the daemon's authentication boundary."""

    tag: Literal["playbill-authenticated-actor-v1"] = "playbill-authenticated-actor-v1"
    actor_id: str
    capabilities: tuple[TransportCapability, ...] = ("propose",)

    @field_validator("actor_id")
    @classmethod
    def _actor_id(cls, value: str) -> str:
        if not _ACTOR_RE.fullmatch(value):
            raise ValueError("authenticated actor_id is not canonical")
        return value

    @field_validator("capabilities")
    @classmethod
    def _capabilities(
        cls,
        value: tuple[TransportCapability, ...],
    ) -> tuple[TransportCapability, ...]:
        if tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))) != value:
            raise ValueError("transport capabilities must be sorted and unique")
        return value


#: Bytes ONE change-set member costs inside the ledger's own record of the
#: change set. The record holds one ``members`` entry and one ``law_evidence``
#: entry per member plus the candidate's scope, and it holds digests and paths
#: rather than evidence: a 1,002-member record measured 7,046,087 bytes on one
#: corpus and 7,046,087 bytes -- identical to the byte -- on a second carrying a
#: third of the evidence, i.e. 7,032 bytes per member either way. This is that
#: measurement rounded up, so the projection this bound is computed from is
#: never smaller than the record it predicts.
CHANGE_SET_RECORD_BYTES_PER_MEMBER = 7_200


class ProposalReceiveLimits(_StrictProposalModel):
    """Every bound proposal receive enforces before a single member is parsed.

    The file-count and aggregate-byte ceilings track the adoption posture of
    `TreeReadLimits`: a proposal carries the whole candidate tree, so a receive
    ceiling below the tree-read ceiling would make a legally accepted instance
    unproposable. The other three keep receive itself bounded no matter how
    large the accepted tree has grown -- how many members one submission may
    change, how large a single member may be, and how deep a member path may
    nest -- so an oversized submission is refused on cheap metadata instead of
    after parsing.

    The last two are ADVERTISEMENTS rather than receive gates, and they are here
    because this is the object a caller reads to learn what a submission may
    carry. The ledger writes its record OF a change set as one blob, measured
    against the per-blob ceiling; a set that satisfies every receive bound above
    can still exceed it, which used to be discovered only at activation, after
    the compile. Advertising the ceiling and the per-member cost next to the
    member budget lets a caller compute the member count that fits before
    authoring anything -- and the fact that it is far below `max_changed_members`
    is the point, not a contradiction: one is what receive accepts, the other is
    what the ledger can record.
    """

    max_files: int = Field(default=250_000, ge=1, le=1_000_000)
    max_changed_members: int = Field(default=5_000, ge=1, le=1_000_000)
    max_file_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=2**40)
    max_total_bytes: int = Field(default=512 * 1024 * 1024, ge=1, le=2**44)
    max_path_depth: int = Field(default=8, ge=1, le=64)
    max_change_set_record_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=2**40)
    change_set_record_bytes_per_member: int = Field(
        default=CHANGE_SET_RECORD_BYTES_PER_MEMBER,
        ge=1,
        le=2**20,
    )

    @property
    def max_change_set_members(self) -> int:
        """Members that fit under the record ceiling, at the measured cost."""

        return max(
            1,
            self.max_change_set_record_bytes // self.change_set_record_bytes_per_member,
        )

    def projected_change_set_record_bytes(self, members: int) -> int:
        """Project the record size a change set of *members* members will write."""

        return members * self.change_set_record_bytes_per_member


class ProposalAdmissionRequest(_StrictProposalModel):
    tag: Literal["playbill-proposal-request-v1"] = "playbill-proposal-request-v1"
    target_ref: str
    proposed_base_oid: str
    source_compilation_digest: str | None = None
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...] = ()

    @field_validator("target_ref")
    @classmethod
    def _target_ref(cls, value: str) -> str:
        if not _PROPOSAL_REF_RE.fullmatch(value):
            raise ValueError("target_ref must be a canonical namespaced proposal ref")
        return value

    @field_validator("proposed_base_oid")
    @classmethod
    def _proposed_base_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("proposed_base_oid is malformed")
        return value

    @field_validator("source_compilation_digest")
    @classmethod
    def _source_compilation_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("claim_type_expansions")
    @classmethod
    def _claim_type_expansions(
        cls,
        value: tuple[ClaimTypeExpansionEvidenceV1, ...],
    ) -> tuple[ClaimTypeExpansionEvidenceV1, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("ClaimType expansion evidence must be sorted and unique")
        digests = tuple(item.expanded_artifact_digest for item in value)
        if len(digests) != len(set(digests)):
            raise ValueError("ClaimType expansion evidence must be unique by expanded artifact")
        return value


class ProposalAdmissionRecord(_StrictProposalModel):
    tag: Literal["playbill-proposal-admission-v1"] = "playbill-proposal-admission-v1"
    proposal_id: str
    actor_id: str
    target_ref: str
    proposed_base_oid: str
    candidate_commit_oid: str
    candidate_tree_oid: str
    source_compilation_digest: str | None
    claim_type_expansions: tuple[ClaimTypeExpansionEvidenceV1, ...] = ()
    limits: ProposalReceiveLimits
    admitted_at: str

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id(cls, value: str) -> str:
        ProposalDigest.from_tagged(value)
        return value

    @field_validator("actor_id")
    @classmethod
    def _actor_id(cls, value: str) -> str:
        return AuthenticatedActor(actor_id=value).actor_id

    @field_validator("target_ref")
    @classmethod
    def _target_ref(cls, value: str) -> str:
        if not _PROPOSAL_REF_RE.fullmatch(value):
            raise ValueError("proposal admission target_ref is malformed")
        return value

    @field_validator("proposed_base_oid", "candidate_commit_oid", "candidate_tree_oid")
    @classmethod
    def _oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("proposal admission Git OID is malformed")
        return value

    @field_validator("source_compilation_digest")
    @classmethod
    def _compilation_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("admitted_at")
    @classmethod
    def _admitted_at(cls, value: str) -> str:
        return validate_candidate_timestamp(value)

    @model_validator(mode="after")
    def _namespace_binding(self) -> "ProposalAdmissionRecord":
        if self.target_ref.split("/")[2] != self.actor_id:
            raise ValueError("admission target namespace differs from authenticated actor")
        if (
            len(
                {
                    len(self.proposed_base_oid),
                    len(self.candidate_commit_oid),
                    len(self.candidate_tree_oid),
                }
            )
            != 1
        ):
            raise ValueError("proposal admission mixes Git object formats")
        return self


class ProposalEvaluationRecord(_StrictProposalModel):
    tag: Literal["playbill-proposal-evaluation-v1"] = "playbill-proposal-evaluation-v1"
    proposal_id: str
    verdict: Literal["candidate", "refused"]
    evaluated_base_oid: str
    evaluated_tree_oid: str | None
    rebased: bool
    candidate_digest: str | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()
    claim_admission_accounts: tuple[ClaimAdmissionEvaluationAccountV1, ...] = ()
    evaluated_at: str

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id(cls, value: str) -> str:
        ProposalDigest.from_tagged(value)
        return value

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str | None) -> str | None:
        if value is not None:
            CandidateDigest.from_tagged(value)
        return value

    @field_validator("evaluated_base_oid", "evaluated_tree_oid")
    @classmethod
    def _oid(cls, value: str | None) -> str | None:
        if value is not None and not _OID_RE.fullmatch(value):
            raise ValueError("proposal evaluation Git OID is malformed")
        return value

    @field_validator("evaluated_at")
    @classmethod
    def _evaluated_at(cls, value: str) -> str:
        return validate_candidate_timestamp(value)

    @field_validator("claim_admission_accounts")
    @classmethod
    def _claim_admission_accounts(
        cls, value: tuple[ClaimAdmissionEvaluationAccountV1, ...]
    ) -> tuple[ClaimAdmissionEvaluationAccountV1, ...]:
        keys = tuple(claim_admission_account_order_key(item) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("claim admission accounts must be canonically sorted and unique")
        return value

    @model_validator(mode="after")
    def _verdict_shape(self) -> "ProposalEvaluationRecord":
        if self.verdict == "candidate":
            if self.candidate_digest is None or self.evaluated_tree_oid is None or self.diagnostics:
                raise ValueError("candidate evaluation record is incomplete")
        elif self.candidate_digest is not None:
            raise ValueError("refused evaluation cannot carry a candidate digest")
        if self.evaluated_tree_oid is not None and len(self.evaluated_tree_oid) != len(
            self.evaluated_base_oid
        ):
            raise ValueError("proposal evaluation mixes Git object formats")
        return self


class ProposalResult(_StrictProposalModel):
    admission: ProposalAdmissionRecord
    evaluation: ProposalEvaluationRecord
    candidate: CandidateRecordAnyVersion | None = None
    workspace_advertisement: PlaybillWorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT

    @model_validator(mode="after")
    def _result_shape(self) -> "ProposalResult":
        if (self.evaluation.verdict == "candidate") != (self.candidate is not None):
            raise ValueError("proposal result candidate shape differs from evaluation verdict")
        return self


class ProposalTransportProtocol(Protocol):
    def object_format(self) -> GitObjectFormat: ...
    def read_main(self) -> str: ...
    def read_tree(self, oid: str) -> dict[str, bytes]: ...
    def read_proposal_ref(self, target_ref: str) -> str | None: ...
    def create_proposal_commit(
        self,
        tree: Mapping[str, bytes],
        *,
        base_oid: str,
        target_ref: str,
        actor_id: str,
        timestamp: str,
        expected_ref_oid: str | None,
    ) -> tuple[str, str]: ...


__all__ = [
    "AuthenticatedActor",
    "ProposalAdmissionRecord",
    "ProposalAdmissionRequest",
    "ProposalEvaluationRecord",
    "CHANGE_SET_RECORD_BYTES_PER_MEMBER",
    "ProposalReceiveLimits",
    "ProposalResult",
    "ProposalTransportProtocol",
    "claim_admission_account_order_key",
    "canonical_proposal_ref_name",
]
