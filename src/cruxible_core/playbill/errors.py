"""Typed refusals for the opt-in Playbill substrate."""

from __future__ import annotations

from cruxible_core.errors import CoreError


class PlaybillError(CoreError):
    """Base class for Playbill protocol and storage refusals."""


class CanonicalEncodingError(PlaybillError):
    """A value has no representation in the frozen canonical encoding."""


class PlaybillFormatError(PlaybillError):
    """A descriptor or stored artifact declares an unsupported format."""


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


class ProposalIntegrityError(PlaybillError):
    """Persisted proposal or candidate evidence failed deterministic verification."""


class ApprovalIntegrityError(PlaybillError):
    """An approval statement, signer, signature, or required quorum was refused."""


class PrincipalIntegrityError(PlaybillError):
    """A principal registry or historical key transition failed replay."""


class SettlementIntegrityError(PlaybillError):
    """A candidate, change set, generation, or root correspondence failed."""


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
    "PlaybillJournalError",
    "PlaybillKeyError",
    "PrincipalIntegrityError",
    "ProposalAdmissionError",
    "ProposalIntegrityError",
    "ProjectionCoordinateError",
    "ProjectionError",
    "ProjectionFormatError",
    "ProjectionIntegrityError",
    "ProjectionPublicationError",
    "SettlementIntegrityError",
    "SubjectFormatError",
    "SubjectNotFoundError",
]
