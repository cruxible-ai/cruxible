"""Compiler-owned governance, provenance, coverage, and proof-reference facts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.attestations import approval_digest
from cruxible_core.playbill.canonical import (
    CandidateDigest,
    ChangeSetDigest,
    Sha256Value,
    normalize_ledger_path,
)
from cruxible_core.playbill.governance import governance_identifier
from cruxible_core.playbill.projection_extensions import ProjectionFact

if TYPE_CHECKING:
    from cruxible_core.playbill.settlement import ChangeSetRecord, ChangeSetRecordV2

AttestationCoverage = Literal[
    "exact_subject",
    "containing_artifact",
    "containing_change_set",
]
BasisRelationKind = Literal[
    "authority_ruled",
    "cryptographically_committed",
    "replay_verified",
]


class ProjectionCoordinateContext(Protocol):
    @property
    def instance_id(self) -> str: ...

    @property
    def git_object_format(self) -> str: ...

    @property
    def git_oid(self) -> str: ...

    @property
    def semantic_root(self) -> str: ...

    @property
    def generation_root(self) -> str: ...

    @property
    def compiler_digest(self) -> str: ...


class _StrictExplanationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LedgerProofReference(_StrictExplanationModel):
    """Exact accepted record sufficient for later independent verification."""

    change_set_path: str
    changeset_digest: str
    candidate_digest: str

    @field_validator("change_set_path")
    @classmethod
    def _path(cls, value: str) -> str:
        if normalize_ledger_path(value) != value or not value.startswith("changesets/"):
            raise ValueError("ledger proof reference must name a canonical change-set path")
        return value

    @field_validator("changeset_digest")
    @classmethod
    def _changeset_digest(cls, value: str) -> str:
        ChangeSetDigest.from_tagged(value)
        return value

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    def projection_value(self, coordinate: ProjectionCoordinateContext) -> dict[str, object]:
        return {
            "accepted_coordinate": {
                "compiler_digest": {"$digest": coordinate.compiler_digest},
                "generation_root": {"$digest": coordinate.generation_root},
                "git_object_format": coordinate.git_object_format,
                "git_oid": coordinate.git_oid,
                "instance_id": coordinate.instance_id,
                "semantic_root": {"$digest": coordinate.semantic_root},
            },
            "candidate_digest": {"$digest": self.candidate_digest},
            "change_set_path": {"$path": self.change_set_path},
            "changeset_digest": {"$digest": self.changeset_digest},
        }


class BasisRelation(_StrictExplanationModel):
    """One registry-governed mechanism supporting a projected fact."""

    kind: BasisRelationKind
    proof_ref: LedgerProofReference
    law_identifier: str | None = None
    law_digest: str | None = None
    attestation_digest: str | None = None

    @field_validator("law_identifier")
    @classmethod
    def _law_identifier(cls, value: str | None) -> str | None:
        if value is not None:
            return governance_identifier(value, label="basis law identifier")
        return value

    @field_validator("law_digest", "attestation_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _kind_shape(self) -> "BasisRelation":
        if self.kind == "authority_ruled":
            if self.law_identifier is None or self.law_digest is None:
                raise ValueError("authority_ruled basis requires its exact law coordinate")
            if self.attestation_digest is not None:
                raise ValueError("authority_ruled basis cannot carry an attestation digest")
        elif self.kind == "cryptographically_committed":
            if self.attestation_digest is None:
                raise ValueError("cryptographically_committed basis requires an attestation digest")
            if self.law_identifier is not None or self.law_digest is not None:
                raise ValueError("cryptographic basis cannot claim law authority")
        elif any(
            value is not None
            for value in (self.law_identifier, self.law_digest, self.attestation_digest)
        ):
            raise ValueError("replay_verified basis carries only its proof reference")
        return self

    def projection_value(self, coordinate: ProjectionCoordinateContext) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": self.kind,
            "proof_ref": self.proof_ref.projection_value(coordinate),
        }
        if self.law_identifier is not None:
            value["law_identifier"] = self.law_identifier
        if self.law_digest is not None:
            value["law_digest"] = {"$digest": self.law_digest}
        if self.attestation_digest is not None:
            value["attestation_digest"] = {"$digest": self.attestation_digest}
        return value


class CoverageBinding(_StrictExplanationModel):
    """Evidence-relative scope of one approval; never a trust-strength label."""

    coverage: AttestationCoverage
    subject_path: str
    signed_payload_digest: str
    proof_ref: LedgerProofReference

    @field_validator("subject_path")
    @classmethod
    def _subject_path(cls, value: str) -> str:
        return normalize_ledger_path(value)

    @field_validator("signed_payload_digest")
    @classmethod
    def _signed_payload_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _v1_candidate_coverage(self) -> "CoverageBinding":
        if self.coverage != "containing_change_set":
            raise ValueError("v1 C_s approval can cover only its containing change set")
        if self.signed_payload_digest != self.proof_ref.candidate_digest:
            raise ValueError("coverage signed payload differs from its proof reference")
        return self

    def projection_value(self, coordinate: ProjectionCoordinateContext) -> dict[str, object]:
        return {
            "coverage": self.coverage,
            "proof_ref": self.proof_ref.projection_value(coordinate),
            "signed_payload_digest": {"$digest": self.signed_payload_digest},
            "subject_path": {"$path": self.subject_path},
        }


def _record_for_current_artifact(
    records: tuple[tuple[str, ChangeSetRecord | ChangeSetRecordV2], ...],
    *,
    path: str,
    input_digest: str,
    artifact_digest: str,
) -> tuple[str, ChangeSetRecord | ChangeSetRecordV2] | None:
    def member_matches(member: object) -> bool:
        candidate_digest = getattr(member, "candidate_artifact_digest", None)
        if isinstance(candidate_digest, str):
            return candidate_digest == artifact_digest
        input_value = getattr(member, "artifact_digest", None)
        return isinstance(input_value, str) and input_value == input_digest

    matching_records = [
        (record_path, record)
        for record_path, record in records
        if any(member.path == path and member_matches(member) for member in record.members)
    ]
    return max(matching_records, key=lambda item: item[1].sequence) if matching_records else None


def accepted_artifact_explanation_facts(
    *,
    artifact_family: str,
    subject_identity: str,
    artifact_path: str,
    input_digest: str,
    artifact_digest: str,
    predecessor_digest: str | None,
    records: tuple[tuple[str, ChangeSetRecord | ChangeSetRecordV2], ...],
    coordinate: ProjectionCoordinateContext,
) -> tuple[ProjectionFact, ...]:
    """Compile explanation facts only when a stored change set binds exact bytes."""

    current = _record_for_current_artifact(
        records,
        path=artifact_path,
        input_digest=input_digest,
        artifact_digest=artifact_digest,
    )
    if current is None:
        return ()
    record_path, record = current
    member = next(member for member in record.members if member.path == artifact_path)
    law_digest = record.law_digests[member.law_identifier]
    proof = LedgerProofReference(
        change_set_path=record_path,
        changeset_digest=record.changeset_digest,
        candidate_digest=record.candidate_digest,
    )
    authority_basis = BasisRelation(
        kind="authority_ruled",
        proof_ref=proof,
        law_identifier=member.law_identifier,
        law_digest=law_digest,
    )
    replay_basis = BasisRelation(kind="replay_verified", proof_ref=proof)
    coverage = CoverageBinding(
        coverage="containing_change_set",
        subject_path=artifact_path,
        signed_payload_digest=record.candidate_digest,
        proof_ref=proof,
    )
    attestations = []
    cryptographic_basis = []
    for submission in record.approvals:
        digest = approval_digest(submission.attestation).tagged
        attestations.append(
            {
                "attestation_digest": {"$digest": digest},
                "key_history_ref": {
                    "principal_path": {
                        "$path": f"principals/{submission.attestation.signer_id}.yaml"
                    },
                    "semantic_root": {"$digest": submission.attestation.signing_semantic_root},
                },
                "signer_id": submission.attestation.signer_id,
                "submitted_by": submission.submitted_by,
            }
        )
        cryptographic_basis.append(
            BasisRelation(
                kind="cryptographically_committed",
                proof_ref=proof,
                attestation_digest=digest,
            ).projection_value(coordinate)
        )
    history = [
        {
            "candidate_digest": {"$digest": item.candidate_digest},
            "change_set_path": {"$path": path},
            "changeset_digest": {"$digest": item.changeset_digest},
            "sequence": item.sequence,
        }
        for path, item in records
        if any(candidate_member.path == artifact_path for candidate_member in item.members)
    ]
    common_basis = [
        authority_basis.projection_value(coordinate),
        replay_basis.projection_value(coordinate),
    ]
    return (
        ProjectionFact(
            schema_id=f"playbill.{artifact_family}.governance",
            schema_version=1,
            subject_identity=subject_identity,
            fact_key="accepted_governance",
            value={
                "activation_policy": record.activation_policy,
                "approval_requirements": [
                    requirement.model_dump(mode="json")
                    for requirement in record.approval_requirements
                ],
                "basis": common_basis,
                "law_digest": {"$digest": law_digest},
                "law_identifier": member.law_identifier,
                "required_tier": record.required_tier,
            },
        ),
        ProjectionFact(
            schema_id=f"playbill.{artifact_family}.provenance",
            schema_version=1,
            subject_identity=subject_identity,
            fact_key="accepted_source",
            value={
                "actor_id": record.actor_binding.actor_id,
                "artifact_digest": {"$digest": artifact_digest},
                "basis": [replay_basis.projection_value(coordinate)],
                "input_digest": {"$digest": input_digest},
                "source_compilation_digest": (
                    {"$digest": record.actor_binding.source_compilation_digest}
                    if record.actor_binding.source_compilation_digest is not None
                    else None
                ),
            },
        ),
        ProjectionFact(
            schema_id=f"playbill.{artifact_family}.attestation_coverage",
            schema_version=1,
            subject_identity=subject_identity,
            fact_key="accepted_approvals",
            value={
                "attestations": attestations,
                "basis": [*common_basis, *cryptographic_basis],
                "coverage_binding": coverage.projection_value(coordinate),
            },
        ),
        ProjectionFact(
            schema_id=f"playbill.{artifact_family}.history",
            schema_version=1,
            subject_identity=subject_identity,
            fact_key="ledger_history",
            value={
                "basis": [replay_basis.projection_value(coordinate)],
                "history": history,
                "predecessor_digest": (
                    {"$digest": predecessor_digest} if predecessor_digest is not None else None
                ),
                "proof_ref": proof.projection_value(coordinate),
            },
        ),
    )


def accepted_document_explanation_facts(
    *,
    document_identity: str,
    document_path: str,
    input_digest: str,
    artifact_digest: str,
    predecessor_digest: str | None,
    records: tuple[tuple[str, ChangeSetRecord | ChangeSetRecordV2], ...],
    coordinate: ProjectionCoordinateContext,
) -> tuple[ProjectionFact, ...]:
    """Retain the frozen Family-1 Document projection shape through an adapter."""

    return accepted_artifact_explanation_facts(
        artifact_family="document",
        subject_identity=document_identity,
        artifact_path=document_path,
        input_digest=input_digest,
        artifact_digest=artifact_digest,
        predecessor_digest=predecessor_digest,
        records=records,
        coordinate=coordinate,
    )


__all__ = [
    "AttestationCoverage",
    "BasisRelation",
    "BasisRelationKind",
    "CoverageBinding",
    "LedgerProofReference",
    "ProjectionCoordinateContext",
    "accepted_artifact_explanation_facts",
    "accepted_document_explanation_facts",
]
