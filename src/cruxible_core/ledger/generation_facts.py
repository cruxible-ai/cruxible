"""Versioned immutable facts carried in a generation's signed Git header.

The header is outside the frozen change-set receipt and the semantic manifest.
Its bytes are covered by the commit signature and OID, so it travels with the
ledger and cannot change independently of the accepted coordinate. Commit
messages remain prose. A missing header identifies a pre-succession generation.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.canonical import ProposalDigest, Sha256Value, canonical_bytes
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.proposal_models import ProposalAdmissionRecord
from cruxible_core.proposals.proposal_notes import admission_bytes


class AcceptingAdmission(BaseModel):
    """The one admission selected by acceptance, never by a later inventory read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-accepting-admission-v1"] = "playbill-accepting-admission-v1"
    proposal_id: str
    admission_digest: str

    @field_validator("proposal_id")
    @classmethod
    def _proposal(cls, value: str) -> str:
        ProposalDigest.from_tagged(value)
        return value

    @field_validator("admission_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @classmethod
    def of(cls, admission: ProposalAdmissionRecord) -> AcceptingAdmission:
        return cls(
            proposal_id=admission.proposal_id,
            admission_digest="sha256:" + hashlib.sha256(admission_bytes(admission)).hexdigest(),
        )

    def header_value(self) -> str:
        return canonical_bytes(self.model_dump(mode="json")).decode("utf-8")


def parse_accepting_admission(raw: bytes) -> AcceptingAdmission:
    try:
        fact = AcceptingAdmission.model_validate_json(raw)
        if fact.header_value().encode("utf-8") != raw:
            raise ValueError("noncanonical header")
    except ValueError as exc:
        raise SettlementIntegrityError("accepting admission header is malformed") from exc
    return fact
