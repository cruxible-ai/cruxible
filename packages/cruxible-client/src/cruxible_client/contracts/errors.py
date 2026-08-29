"""Typed refusals for the opt-in Playbill substrate."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Self

from cruxible_client._error_base import CoreError


class PlaybillError(CoreError):
    """Base class for Playbill protocol and storage refusals."""


class CanonicalEncodingError(PlaybillError):
    """A value has no representation in the frozen canonical encoding."""


class MerkleIntegrityError(PlaybillError):
    """A merkle manifest does not reproduce its claimed root or node digests."""


class PlaybillFormatError(PlaybillError):
    """A descriptor or stored artifact declares an unsupported format."""


class PlaybillSinceRequestInvalid(PlaybillFormatError):
    """A ``playbill since`` request failed its frozen model boundary."""

    error_code = "playbill.since.request_invalid"

    def __init__(
        self,
        *,
        field_path: str,
        detail: str = "invalid value",
        message: str | None = None,
    ) -> None:
        self.field_path = field_path
        self.detail = detail
        super().__init__(
            message or f"{self.error_code}: request field {field_path} is invalid: {detail}"
        )

    @classmethod
    def from_validation_errors(
        cls,
        errors: Iterable[Mapping[str, Any]],
    ) -> Self:
        """Build one stable refusal from Pydantic's ordered error details."""

        first = next(iter(errors), {})
        location = first.get("loc", ())
        parts = list(location) if isinstance(location, list | tuple) else []
        if parts and parts[0] == "body":
            parts.pop(0)
        field_path = "$"
        for part in parts:
            if isinstance(part, int):
                field_path += f"[{part}]"
            else:
                field_path += f".{part}"
        return cls(
            field_path=field_path,
            detail=str(first.get("msg", "invalid value")),
        )


class ClaimAttestationRequestInvalid(PlaybillFormatError):
    """A Claim-attestation append failed its exact request model boundary."""

    error_code = "playbill.claim_attestation.request_invalid"

    def __init__(self, *, field_path: str, detail: str = "invalid value") -> None:
        self.field_path = field_path
        self.detail = detail
        super().__init__(f"{self.error_code}: request field {field_path} is invalid: {detail}")

    @classmethod
    def from_validation_errors(
        cls,
        errors: Iterable[Mapping[str, Any]],
    ) -> Self:
        first = next(iter(errors), {})
        location = first.get("loc", ())
        parts = list(location) if isinstance(location, list | tuple) else []
        if parts and parts[0] == "body":
            parts.pop(0)
        field_path = "$"
        for part in parts:
            field_path += f"[{part}]" if isinstance(part, int) else f".{part}"
        return cls(
            field_path=field_path,
            detail=str(first.get("msg", "invalid value")),
        )


class PlaybillInstanceIncompatiblePrereleaseContent(PlaybillFormatError):
    """An accepted prerelease artifact was intentionally removed before release."""

    error_code = "playbill.instance.incompatible_prerelease_content"

    def __init__(self, *, artifact_class: str) -> None:
        self.artifact_class = artifact_class
        super().__init__(
            f"{self.error_code}: accepted artifact class {artifact_class!r} is no longer "
            "supported; create a fresh prerelease instance"
        )


class PlaybillBootstrapError(PlaybillError):
    """Genesis does not reproduce from the supplied out-of-band trust root."""


class PlaybillGitError(PlaybillError):
    """System Git refused or failed a ledger operation."""


class PlaybillKeyError(PlaybillError):
    """Key generation, custody, or public/private correspondence failed."""


class PlaybillCasError(PlaybillError):
    """Content-addressed body storage is missing, corrupt, or unauthorized."""


class PlaybillJournalError(PlaybillError):
    """An operational journal record, head, range, or writer fence is invalid."""


class PlaybillExecutionError(PlaybillError):
    """A Procedure run cannot be admitted, executed, or finalized safely."""


class DocumentFormatError(PlaybillError):
    """A governed Document shell is unsupported or malformed."""


class DocumentNotFoundError(PlaybillError):
    """A requested accepted Document identity is absent at the coordinate."""


class SubjectFormatError(PlaybillError):
    """A governed Subject shell is unsupported, malformed, or mislocated."""


class SubjectNotFoundError(PlaybillError):
    """A requested accepted Subject identity is absent at the coordinate."""


class ClaimNotFoundError(PlaybillError):
    """A requested accepted Claim identity is absent at the coordinate."""


class ProposalAdmissionError(PlaybillError):
    """An unauthenticated, mis-scoped, oversized, or malformed proposal was refused."""


class ProposalActivationRequestInvalid(PlaybillFormatError):
    """A proposal activation route received a malformed proposal digest."""

    error_code = "playbill.proposal.activation_request_invalid"


class ProposalIntegrityError(PlaybillError):
    """Persisted proposal or candidate evidence failed deterministic verification."""


class ProposalEvaluationIntegrityError(ProposalIntegrityError):
    """Daemon-created proposal evaluation bytes failed their own typed contract."""


class ApprovalIntegrityError(PlaybillError):
    """An approval statement, signer, signature, or required quorum was refused."""


class PrincipalIntegrityError(PlaybillError):
    """A principal registry or historical key transition failed replay."""


class SettlementIntegrityError(PlaybillError):
    """A candidate, change set, generation, or root correspondence failed."""


class ReplayCheckpointError(PlaybillError):
    """A local replay checkpoint is missing, stale, or does not reproduce the ledger."""


class ProjectionError(PlaybillError):
    """Base refusal for deterministic Playbill projection operations."""


class ProjectionCoordinateError(ProjectionError):
    """A build request does not match one previously verified ledger coordinate."""


class ProjectionFormatError(ProjectionError):
    """A ledger artifact or normalized projection fact is unsupported or malformed."""


class ProjectionPublicationError(ProjectionError):
    """An immutable projection could not be durably or atomically published."""


class ProjectionIntegrityError(ProjectionError):
    """A published manifest or physical piece failed binding verification."""


__all__ = [
    "CanonicalEncodingError",
    "ApprovalIntegrityError",
    "ClaimNotFoundError",
    "DocumentFormatError",
    "DocumentNotFoundError",
    "PlaybillBootstrapError",
    "PlaybillCasError",
    "PlaybillError",
    "PlaybillFormatError",
    "PlaybillGitError",
    "PlaybillExecutionError",
    "PlaybillInstanceIncompatiblePrereleaseContent",
    "PlaybillJournalError",
    "PlaybillKeyError",
    "PlaybillSinceRequestInvalid",
    "PrincipalIntegrityError",
    "ProposalActivationRequestInvalid",
    "ProposalAdmissionError",
    "ProposalIntegrityError",
    "ProjectionCoordinateError",
    "ProjectionError",
    "ProjectionFormatError",
    "ProjectionIntegrityError",
    "ProjectionPublicationError",
    "ReplayCheckpointError",
    "SettlementIntegrityError",
    "SubjectFormatError",
    "SubjectNotFoundError",
]
