"""Immutable governed placement of the existing signed V2 attestation envelope.

Identity is ClaimAttestation:<envelope sha256>. The file contains exactly the
canonical V2 envelope, without a lifecycle wrapper. Artifact and file digests
have distinct domains; neither is the envelope identity. Corrections are new
identities, never revisions of these signed bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    Sha256Value,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationCoverage,
    ClaimAttestationV2,
    ClaimStance,
    VerifiedClaimAttestationV1,
    claim_attestation_v2_envelope_digest,
)
from cruxible_client.contracts.errors import PlaybillFormatError

ATTESTATION_ARTIFACT_DOMAIN = "cruxible-accepted-claim-attestation-artifact-v1"


def attestation_identity(attestation: ClaimAttestationV2) -> ArtifactIdentity:
    return ArtifactIdentity(
        kind="ClaimAttestation", name=claim_attestation_v2_envelope_digest(attestation)
    )


def attestation_path(envelope_digest: str) -> str:
    Sha256Value.from_tagged(envelope_digest)
    digest = envelope_digest.removeprefix("sha256:")
    return f"attestations/{digest[:2]}/{digest}.json"


def attestation_artifact_digest(attestation: ClaimAttestationV2) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        ATTESTATION_ARTIFACT_DOMAIN,
        {"envelope_digest": claim_attestation_v2_envelope_digest(attestation)},
    )


def render_accepted_attestation(attestation: ClaimAttestationV2) -> bytes:
    return pretty_canonical_bytes(attestation.model_dump(mode="json"))


def parse_accepted_attestation(
    content: bytes, *, path: str, codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC
) -> ClaimAttestationV2:
    if codec != ArtifactCodec.CURRENT_PRETTY_JSON:
        raise PlaybillFormatError("accepted attestations require the JSON artifact codec")
    try:
        value = ClaimAttestationV2.model_validate_json(content)
    except ValueError as exc:
        raise PlaybillFormatError("accepted attestation envelope is malformed") from exc
    if path != attestation_path(claim_attestation_v2_envelope_digest(value)):
        raise PlaybillFormatError("accepted attestation path does not match its signed envelope")
    if content != render_accepted_attestation(value):
        raise PlaybillFormatError("accepted attestation bytes are not canonical")
    return value


# This is a read result, not a signed envelope or another authority record.
# The signed V2 envelope is retained verbatim; these properties only adapt its
# vocabulary to the common verdict reducer, which also verifies frozen V1 inputs.
@dataclass(frozen=True)
class AcceptedAttestationVerdictStatement:
    envelope: ClaimAttestationV2

    @property
    def observed_at(self) -> datetime:
        return self.envelope.statement.attested_at

    @property
    def valid_until(self) -> datetime | None:
        return self.envelope.statement.valid_until

    @property
    def capture_digests(self) -> tuple[str, ...]:
        return self.envelope.statement.cited_capture_digests

    @property
    def stance(self) -> ClaimStance:
        return self.envelope.statement.stance

    @property
    def provider_or_principal(self) -> ArtifactIdentity:
        return ArtifactIdentity(
            kind="Principal", name=self.envelope.statement.attesting_principal_id
        )

    @property
    def claim_statement_digest(self) -> str:
        return self.envelope.statement.claim_statement_digest


class AcceptedClaimAttestationEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["cruxible-accepted-claim-attestation-evidence-v1"] = (
        "cruxible-accepted-claim-attestation-evidence-v1"
    )
    envelope: ClaimAttestationV2
    coverage: ClaimAttestationCoverage
    current: bool

    @property
    def statement(self) -> AcceptedAttestationVerdictStatement:
        return AcceptedAttestationVerdictStatement(self.envelope)

    @property
    def attestation_digest(self) -> str:
        return claim_attestation_v2_envelope_digest(self.envelope)

    @property
    def control_domain(self) -> str:
        return f"principal.{self.envelope.statement.attesting_principal_id}"

    @property
    def upstream_provenance(self) -> tuple[ArtifactIdentity, ...]:
        return ()

    @property
    def attestation_grade(self) -> Literal["verified_principal"]:
        return "verified_principal"


ClaimAttestationEvidence: TypeAlias = (
    VerifiedClaimAttestationV1 | AcceptedClaimAttestationEvidenceV1
)
