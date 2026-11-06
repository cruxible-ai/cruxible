"""What a Claim attestation cites: write's evidence options, made into Captures first (D-R42).

An attestation is a signed append, never a proposal, and its signature names
the exact Captures it cites, so they exist before the caller signs. The caller
sends the same evidence a ``write`` change takes -- ``because`` (its own words,
a self-source Capture), and one ``evidence`` option: ``self``, ``section``
(``own_words`` makes the section's text own words), ``file``, ``capture`` or
``contract`` -- with file and section evidence observed on the writer's side
(``observe_evidence``). The daemon makes or resolves the Captures exactly as a
write would, at the current head, and answers their digests and the coordinate
the attestation then signs at. With neither, the attestation cites the Claim's
own evidence (``examined_existing``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import CasDigest
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.write import Evidence

#: The most text one attestation's own words (``because``) carry.
ATTESTATION_BECAUSE_MAX_BYTES = 4096


class _StrictAttestationEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimAttestationEvidenceRequest(_StrictAttestationEvidenceModel):
    tag: Literal["cruxible-claim-attestation-evidence-request-v1"] = (
        "cruxible-claim-attestation-evidence-request-v1"
    )
    claim_id: str = Field(min_length=1, max_length=128)
    because: str | None = Field(
        default=None, description="Your own words: cited as a self-source Capture."
    )
    evidence: Evidence | None = Field(
        default=None,
        description="One write evidence option, observed by the writer-side adapter.",
    )

    @field_validator("because")
    @classmethod
    def _because(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value.strip()
            or "\x00" in value
            or len(value.encode("utf-8")) > ATTESTATION_BECAUSE_MAX_BYTES
        ):
            raise ValueError(
                f"because must hold 1..{ATTESTATION_BECAUSE_MAX_BYTES} UTF-8 bytes and no NUL"
            )
        return value

    @model_validator(mode="after")
    def _something_cited(self) -> ClaimAttestationEvidenceRequest:
        if self.because is None and self.evidence is None:
            raise ValueError("name because or evidence; with neither, attest the Claim's own")
        return self


class ClaimAttestationEvidenceResult(_StrictAttestationEvidenceModel):
    tag: Literal["cruxible-claim-attestation-evidence-result-v1"] = (
        "cruxible-claim-attestation-evidence-result-v1"
    )
    claim_id: str
    #: The accepted head the Captures were made against: the attestation's referent.
    referent_coordinate: AcceptedCoordinate
    #: The Captures to cite, ASCII-sorted and unique.
    capture_digests: tuple[str, ...] = Field(min_length=1)

    @field_validator("capture_digests")
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("attestation evidence Captures must be ASCII-sorted and unique")
        for digest in value:
            CasDigest.from_tagged(digest)
        return value


__all__ = [
    "ATTESTATION_BECAUSE_MAX_BYTES",
    "ClaimAttestationEvidenceRequest",
    "ClaimAttestationEvidenceResult",
]
