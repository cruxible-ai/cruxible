"""Typed refusals for the opt-in Playbill substrate."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Self

from cruxible_client._error_base import CoreError, printable

if TYPE_CHECKING:
    from cruxible_client.contracts.repairs import RepairOperationV1


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


class PlaybillReseedRequired(PlaybillFormatError):
    """A prerelease instance is readable but no longer accepts mutations."""

    error_code = "playbill.instance.reseed_required"

    def __init__(self) -> None:
        super().__init__(
            f"{self.error_code}: this instance's compiler selects the frozen compact "
            "artifact codec from before PC-HR; archive it and initialize a fresh instance"
        )


class PlaybillInstanceDecommissioned(PlaybillFormatError):
    """A decommissioned instance is readable forever but never writable again."""

    error_code = "playbill.instance.decommissioned"

    def __init__(self, *, instance_id: str, reason: str, decommissioned_at: str) -> None:
        self.instance_id = instance_id
        self.reason = reason
        self.decommissioned_at = decommissioned_at
        self.repair_commands = (
            "cruxible playbill host create --workspace <root>  # start a fresh instance",
        )
        super().__init__(
            f"{self.error_code}: instance {instance_id!r} was decommissioned at "
            f"{decommissioned_at} ({printable(reason)}); its accepted state stays readable "
            "and nothing "
            "was deleted, but it accepts no further governed writes. Repair: allocate a new "
            "instance with `cruxible playbill host create`, or archive this directory yourself"
        )


class SemanticDeltaLimitError(PlaybillFormatError):
    """A semantic delta exceeds its deterministic served-response budget."""

    error_code = "playbill.semantic_delta.limit_exceeded"


class PlaybillBootstrapError(PlaybillError):
    """Genesis does not reproduce from the supplied out-of-band trust root."""


class PlaybillObjectFormatConflict(PlaybillBootstrapError):
    """An explicit ledger object format contradicts the attached workspace's.

    Its own code, not the advertisement vocabulary's ``object_format_mismatch``:
    that identifier already names a ``WorkspaceAdvertisementFailureCode`` member
    for a refresh that failed AFTER an instance existed, and an init that
    refuses never reaches advertisement at all.
    """

    error_code = "playbill.init.object_format_conflict"

    def __init__(self, message: str, *, workspace_format: str | None = None) -> None:
        self.workspace_format = workspace_format
        self.repair_commands = (
            "cruxible playbill init  # omit --object-format to inherit the workspace",
        )
        super().__init__(message)


class PlaybillGitError(PlaybillError):
    """System Git refused or failed a ledger operation."""


class PlaybillKeyError(PlaybillError):
    """Key generation, custody, or public/private correspondence failed."""


class PlaybillCasError(PlaybillError):
    """Content-addressed body storage is missing, corrupt, or unauthorized."""


class PlaybillJournalError(PlaybillError):
    """An operational journal record, head, range, or writer fence is invalid."""


class PlaybillJournalConflictError(PlaybillJournalError):
    """Another writer holds the partition, or the expected head has moved.

    A conflict is a concurrency fact: the same append succeeds once the caller
    reads the current head and re-fences. Carrying it as its own class is what
    lets a served refusal classify by type instead of by grepping English out of
    a message.
    """


class PlaybillJournalIntegrityError(PlaybillJournalError):
    """Stored journal bytes, their chain, or a partition identity is corrupt.

    An integrity failure is never retryable and never a concurrency fact; it
    names damaged or substituted storage.
    """


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


class ProposalWithdrawnError(ProposalAdmissionError):
    """A settlement door was asked to settle a proposal its actor withdrew.

    Withdrawal is a durable statement that an open proposal will never be
    settled, so every door that would settle one -- approval, activation,
    readmission -- refuses here rather than leaving the statement advisory. The
    refusal names the record that makes it, because the only thing to say to an
    author who meant to activate is who withdrew it, when, and why.
    """

    error_code = "playbill.proposal_withdrawn"

    def __init__(
        self,
        proposal_id: str,
        *,
        actor_id: str,
        reason: str,
        withdrawn_at: str,
    ) -> None:
        self.proposal_id = proposal_id
        self.actor_id = actor_id
        self.reason = reason
        self.withdrawn_at = withdrawn_at
        super().__init__(
            f"{self.error_code}: proposal {proposal_id} was withdrawn by {actor_id!r} at "
            f"{withdrawn_at} ({reason}); a withdrawal is terminal, so author the change "
            "again as a new proposal."
        )


class ProposalNotFoundError(PlaybillError):
    """A proposal selector did not resolve to immutable admission evidence."""

    error_code = "playbill.proposal_not_found"

    def __init__(self, selector: str, *, message: str | None = None) -> None:
        self.selector = selector
        self.accepted_forms = (
            "full proposal digest",
            "unique digest prefix (sha256: plus at least 8 hex characters)",
            "target ref",
        )
        self.repair_commands = ("cruxible playbill proposal list",)
        super().__init__(
            message
            or f"{self.error_code}: proposal selector {selector!r} was not found; accepted "
            "forms are a full proposal digest, a unique digest prefix (sha256: plus at "
            "least 8 hex characters), or a target ref; run `cruxible playbill proposal list`"
        )


class ProposalSelectorAmbiguousError(PlaybillError):
    """A mutable proposal selector does not name one current admission."""

    error_code = "playbill.proposal_selector_ambiguous"

    def __init__(
        self,
        selector: str,
        candidates: tuple[str, ...],
        *,
        message: str | None = None,
    ) -> None:
        self.selector = selector
        self.candidates = candidates
        self.repair_commands = ("cruxible playbill proposal list",)
        if message is not None:
            rendered = message
        elif len(candidates) == 1:
            rendered = (
                f"{self.error_code}: proposal selector {selector!r} no longer names a "
                f"current admission; historical candidate: {candidates[0]}"
            )
        else:
            rendered = (
                f"{self.error_code}: proposal selector {selector!r} names multiple current "
                f"admissions: {', '.join(candidates)}"
            )
        super().__init__(f"{rendered}; run `cruxible playbill proposal list`")


class ProposalContentUnavailable(ProposalAdmissionError):
    """Closed candidate bytes were released; the outcome record remains readable."""

    error_code = "playbill.proposal_content_unavailable"

    def __init__(self) -> None:
        super().__init__(
            f"{self.error_code}: exact proposal content is no longer retained; "
            "its recorded outcome remains available. Resubmit the authored change "
            "to create a new reviewable proposal."
        )


class ProposalReadmitRequiresResubmission(ProposalAdmissionError):
    """A generated closure must be rebuilt rather than byte-rebased."""

    error_code = "playbill.proposal.readmit_requires_resubmission"

    def __init__(self) -> None:
        super().__init__(
            f"{self.error_code}: this stale proposal is a generated dependency-closure "
            "migration; rerun claim-type migration preflight/submit at current head so "
            "the dependent inventory and pins are rebuilt"
        )


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


class ReadRefusalError(CoreError):
    """A read verb (``orient``, ``get``, ``query``) refused for a reason the caller can repair.

    A read never answers a wrong name with an empty result or a server fault. It
    refuses with a stable code, the candidates it could have meant, and the one
    operation that repairs the call. The message carries all three, so a caller
    that only sees prose still sees the repair; ``context`` carries them as data
    (``candidates``, ``repair_line`` and, for a request field, ``field_path``).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        candidates: Iterable[str] = (),
        repair: RepairOperationV1 | None = None,
        repair_line: str | None = None,
        field_path: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.error_code = code
        self.http_status = http_status
        self.candidates = tuple(candidates)
        self.repair = repair
        self.context: dict[str, Any] = dict(context or {})
        if self.candidates:
            self.context["candidates"] = list(self.candidates)
        if repair_line is not None:
            self.context["repair_line"] = repair_line
        if field_path is not None:
            self.context["field_path"] = field_path
        text = f"{code}: {message}"
        if field_path is not None:
            text += f" (at {field_path})"
        if self.candidates:
            text += f"; nearest: {', '.join(self.candidates)}"
        if repair_line is not None:
            text += f". {repair_line}"
        super().__init__(text)

    @property
    def repair_line(self) -> str | None:
        line = self.context.get("repair_line")
        return line if isinstance(line, str) else None

    @property
    def field_path(self) -> str | None:
        path = self.context.get("field_path")
        return path if isinstance(path, str) else None

    @classmethod
    def from_served(
        cls,
        *,
        code: str,
        message: str,
        http_status: int,
        context: Mapping[str, Any],
    ) -> Self:
        """Rebuild a served refusal exactly as the daemon rendered it."""

        refusal = cls.__new__(cls)
        refusal.error_code = code
        refusal.http_status = http_status
        candidates = context.get("candidates", ())
        refusal.candidates = tuple(str(item) for item in candidates) if candidates else ()
        refusal.repair = None
        refusal.context = dict(context)
        CoreError.__init__(refusal, message)
        return refusal


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
    "PlaybillJournalConflictError",
    "PlaybillJournalError",
    "PlaybillJournalIntegrityError",
    "PlaybillKeyError",
    "PlaybillReseedRequired",
    "PlaybillSinceRequestInvalid",
    "PrincipalIntegrityError",
    "ProposalActivationRequestInvalid",
    "ProposalAdmissionError",
    "ProposalReadmitRequiresResubmission",
    "ProposalContentUnavailable",
    "ProposalNotFoundError",
    "ProposalSelectorAmbiguousError",
    "ProposalWithdrawnError",
    "ProposalIntegrityError",
    "ProjectionCoordinateError",
    "ProjectionError",
    "ProjectionFormatError",
    "ProjectionIntegrityError",
    "ProjectionPublicationError",
    "ReadRefusalError",
    "ReplayCheckpointError",
    "SemanticDeltaLimitError",
    "SettlementIntegrityError",
    "SubjectFormatError",
    "SubjectNotFoundError",
]
