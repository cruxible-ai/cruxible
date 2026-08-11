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


class DocumentFormatError(PlaybillError):
    """A governed Document shell is unsupported or malformed."""


class ProposalAdmissionError(PlaybillError):
    """An unauthenticated, mis-scoped, oversized, or malformed proposal was refused."""


class ProposalIntegrityError(PlaybillError):
    """Persisted proposal or candidate evidence failed deterministic verification."""


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
    "DocumentFormatError",
    "PlaybillBootstrapError",
    "PlaybillCasError",
    "PlaybillError",
    "PlaybillFormatError",
    "PlaybillGitError",
    "PlaybillKeyError",
    "ProposalAdmissionError",
    "ProposalIntegrityError",
    "ProjectionCoordinateError",
    "ProjectionError",
    "ProjectionFormatError",
    "ProjectionIntegrityError",
    "ProjectionPublicationError",
]
