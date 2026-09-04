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


#: Bytes ONE change-set record ENTRY costs inside the ledger's own record of the
#: change set. The record holds one ``members`` entry and one ``law_evidence``
#: entry per entry in the candidate's scope, and it holds digests and paths
#: rather than evidence: a 1,002-entry record measured 7,046,087 bytes on one
#: corpus and 7,046,087 bytes -- identical to the byte -- on a second carrying a
#: third of the evidence.
#:
#: An entry's cost varies by the artifact kind that wrote it, because the laws
#: evaluated against a Claim are not the laws evaluated against a Subject. The
#: marginal cost of one further entry, measured on a hermetic instance by
#: settling each kind at two sizes and reading the accepted record back:
#:
#: ===========================================  ==============
#: Entry written by                             Bytes / entry
#: ===========================================  ==============
#: ``SubjectAuthoringPayloadV1``                         1,551
#: ``ClaimTypeAuthoringPayloadV1``                       4,027
#: ``ClaimAuthoringPayloadV1`` (and insertion)           6,215
#: ``ClaimRetirementMemberV1``                           8,670
#: ``ClaimTypeSuccessionMemberV1`` dependent            10,230
#: ===========================================  ==============
#:
#: This is the LARGEST of those rounded up to the next kibibyte, so a projection
#: computed from it is never smaller than the record it predicts -- which is the
#: whole point of the bound: a set it admits must be a set the ledger can write.
#: A per-kind cost would refuse later and admit more, and is what the post-freeze
#: card asks for along with a record the ceiling no longer has to bound.
CHANGE_SET_RECORD_BYTES_PER_MEMBER = 11 * 1024


#: The bounds RECEIVE itself enforces, and therefore the only ones any stored
#: identity is computed over. FROZEN: this set names the shape a proposal id,
#: a preflight certificate and an admission record were written under before
#: the advertised record ceiling existed, and a build that widened it would
#: make every one of those written by an older build unreadable.
PROPOSAL_RECEIVE_BOUND_KEYS = frozenset(
    {
        "max_files",
        "max_changed_members",
        "max_file_bytes",
        "max_total_bytes",
        "max_path_depth",
    }
)


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
    could once still exceed it, which was discovered only at activation, after
    the compile. Advertising the ceiling and the per-entry cost next to the
    member budget lets a caller compute the member count that fits before
    authoring anything.

    They are no longer a SECOND budget. At the 4 MiB ceiling this shipped with,
    only 372 entries fit -- far below `max_changed_members` -- so the two numbers
    stood side by side bounding different things, one what receive accepts and
    one what the ledger can record. The per-blob ceiling is now 64 MiB and they
    are ONE number: 5,000 entries at 11,264 bytes each project to 56,320,000
    bytes, about 53.7 MiB, so the advertised member budget fits under the record
    ceiling with room left, and `max_change_set_members` (5,957 at the defaults)
    is a derived reading of the same budget rather than a competing one.

    What both bound is RECORD ENTRIES, not authored members: the record holds one
    entry per path the set lowers to, and a member may lower to several -- a
    ClaimType succession writes one per dependent it dispositions, a retirement
    one per Claim in its closure. `max_change_set_members` is therefore the
    entry budget, and a set of that many 1:1 members is the largest set it
    names exactly.
    """

    max_files: int = Field(default=250_000, ge=1, le=1_000_000)
    max_changed_members: int = Field(default=5_000, ge=1, le=1_000_000)
    max_file_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=2**40)
    max_total_bytes: int = Field(default=512 * 1024 * 1024, ge=1, le=2**44)
    max_path_depth: int = Field(default=8, ge=1, le=64)
    # Equal to `TreeReadLimits.max_blob_bytes` by hand -- the client package
    # cannot import core -- and held equal by `test_ledger_record_bounds`.
    # Raising it is backward compatible: it widens a READ limit, and every
    # record already accepted was written under the narrower 4 MiB ceiling.
    max_change_set_record_bytes: int = Field(default=64 * 1024 * 1024, ge=1, le=2**40)
    change_set_record_bytes_per_member: int = Field(
        default=CHANGE_SET_RECORD_BYTES_PER_MEMBER,
        ge=1,
        le=2**20,
    )

    def receive_bound_payload(self) -> dict[str, object]:
        """Render only the bounds RECEIVE enforces, for a stored identity.

        Every durable identity computed over these limits -- a proposal id, a
        preflight certificate digest, an admission record's canonical bytes --
        is computed over THIS, never over the whole model. The advertised
        ceilings are a preflight bound and a published number; adding one, or
        moving one after a measurement, must not restate the identity of every
        proposal admitted and every certificate minted before it, which is
        exactly what a whole-model preimage would do.
        """

        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key in PROPOSAL_RECEIVE_BOUND_KEYS
        }

    @property
    def max_change_set_members(self) -> int:
        """Record entries that fit under the ceiling, at the measured cost."""

        return max(
            1,
            self.max_change_set_record_bytes // self.change_set_record_bytes_per_member,
        )

    def projected_change_set_record_bytes(self, members: int) -> int:
        """Project the record a change set of *members* record entries writes."""

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


class ProposalWithdrawalRecordV1(_StrictProposalModel):
    """One actor's durable statement that an open proposal will never be settled.

    Out-of-band evidence, exactly like the admission and evaluation records it
    sits beside: nothing about accepted state changes, and the candidate stays
    readable forever. What changes is the inventory -- a proposal that cannot
    activate stops being reported open, so `proposal list` is a list of work and
    not a graveyard. Immutable and one per proposal: withdrawal is a terminal
    statement, so a second one over the same proposal is refused rather than
    silently restating the first with a new reason.
    """

    tag: Literal["playbill-proposal-withdrawal-v1"] = "playbill-proposal-withdrawal-v1"
    proposal_id: str
    actor_id: str
    reason: str = Field(min_length=1, max_length=1_000)
    withdrawn_at: str

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id(cls, value: str) -> str:
        ProposalDigest.from_tagged(value)
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if value.strip() != value or not value.strip():
            raise ValueError("withdrawal reason must be nonblank and normalized")
        if any(character in value for character in "\r\n"):
            raise ValueError("withdrawal reason must be a single line")
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
    "PROPOSAL_RECEIVE_BOUND_KEYS",
    "ProposalReceiveLimits",
    "ProposalWithdrawalRecordV1",
    "ProposalResult",
    "ProposalTransportProtocol",
    "claim_admission_account_order_key",
    "canonical_proposal_ref_name",
]
