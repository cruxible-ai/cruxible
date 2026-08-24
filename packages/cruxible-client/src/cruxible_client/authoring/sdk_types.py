"""Public values for the synchronous Playbill SDK.

Knowledge is governed state; code is how agents author changes to it.  These
types carry decisions and coordinate assertions, never authority of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar, Protocol, runtime_checkable

from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc


class RefKind(str, Enum):
    SUBJECT = "subject"
    CLAIM_TYPE = "claim_type"
    CLAIM = "claim"
    PROCEDURE = "procedure"
    QUERY = "query"
    SOURCE = "source"
    SLOT = "slot"


@runtime_checkable
class TypedRef(Protocol):
    kind: RefKind
    address: str
    coordinate: AcceptedCoordinate


@dataclass(frozen=True)
class SubjectRef:
    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.SUBJECT


@dataclass(frozen=True)
class ClaimTypeRef:
    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.CLAIM_TYPE


@dataclass(frozen=True)
class ClaimRef:
    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.CLAIM


@dataclass(frozen=True)
class ProcedureRef:
    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.PROCEDURE


@dataclass(frozen=True)
class QueryRef:
    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.QUERY


@dataclass(frozen=True)
class SourceRef:
    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.SOURCE


@dataclass(frozen=True)
class SlotRef:
    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.SLOT


@dataclass(frozen=True)
class Duration:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise ValueError("duration must be a nonnegative integer number of microseconds")

    @classmethod
    def days(cls, *, count: int) -> Duration:
        return cls._scaled(count, 86_400_000_000)

    @classmethod
    def hours(cls, *, count: int) -> Duration:
        return cls._scaled(count, 3_600_000_000)

    @classmethod
    def microseconds(cls, *, count: int) -> Duration:
        return cls._scaled(count, 1)

    @classmethod
    def _scaled(cls, count: int, factor: int) -> Duration:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("duration count must be a nonnegative integer")
        return cls(value=count * factor)

    def model_dump(self) -> dict[str, object]:
        return {"tag": "playbill-duration-v1", "microseconds": self.value}


class ClaimRole(str, Enum):
    NORMATIVE = "normative"
    OBSERVATION = "observation"
    ENVIRONMENT_BINDING = "environment_binding"
    DERIVATION = "derivation"


class Disposition(str, Enum):
    NOT_TESTED = "not_tested"
    SUPPORT = "support"
    CONTRADICT = "contradict"
    UNSURE = "unsure"


class BriefKind(str, Enum):
    BRIEF = "brief"
    GUIDANCE = "guidance"
    FAQ = "faq"


class Audience(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    BOTH = "both"


class ActivationPolicy(str, Enum):
    DRAIN = "drain"
    ABORT = "abort"
    SNAPSHOT = "snapshot"
    EPOCH_CHECK = "epoch-check"


class InsertionOperation(str, Enum):
    BEFORE = "insert_before"
    AFTER = "insert_after"
    REPLACE = "replace_window"
    APPEND = "append"


class ClaimObjectKind(str, Enum):
    LITERAL = "literal"
    SUBJECT = "subject"
    EXACT_CONTENT = "exact_content"


class Cardinality(str, Enum):
    ONE = "one"
    MANY = "many"


class ReferentSensitivity(str, Enum):
    IDENTITY = "identity"
    SHELL = "shell"


@dataclass(frozen=True)
class EffectivePeriod:
    starts_at: datetime | None
    ends_at: datetime | None

    def __post_init__(self) -> None:
        if self.starts_at is not None:
            object.__setattr__(self, "starts_at", ensure_utc(self.starts_at))
        if self.ends_at is not None:
            object.__setattr__(self, "ends_at", ensure_utc(self.ends_at))
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at <= self.starts_at
        ):
            raise ValueError("effective period must be increasing")


@dataclass(frozen=True)
class AccessProfile:
    profile_id: str
    permitted_access_classes: tuple[str, ...]
    disclose_restricted_existence: bool

    def __post_init__(self) -> None:
        if not self.profile_id or self.profile_id != self.profile_id.strip():
            raise ValueError("access profile ID must be nonblank canonical text")
        if tuple(sorted(set(self.permitted_access_classes))) != self.permitted_access_classes:
            raise ValueError("access classes must be sorted and unique")

    def model_dump(self) -> dict[str, object]:
        return {
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": self.profile_id,
            "permitted_access_classes": list(self.permitted_access_classes),
            "disclose_restricted_existence": self.disclose_restricted_existence,
        }


@dataclass(frozen=True)
class BriefClaimExpectation:
    subject: str | SubjectRef | None
    claim_type: str | ClaimTypeRef | None


@dataclass(frozen=True)
class BriefQueryRender:
    parameters: dict[str, CanonicalValue]
    render_field: str


@dataclass(frozen=True)
class CallSite:
    logical_file: str
    line: int
    column: int | None
    expression: str | None


@dataclass(frozen=True)
class SourceMapEntry:
    builder_path: str
    emitted_paths: tuple[str, ...]
    call_site: CallSite


@dataclass(frozen=True)
class Diagnostic:
    code: str
    stage: str
    offending_element: str
    message: str
    repair: tuple[object, ...]
    owner: str | None
    disposition: str | None
    call_site: CallSite | None


@dataclass(frozen=True)
class DerivationSpec:
    name: str


class PlaybillSdkError(ValueError):
    code = "playbill.sdk.refused"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class CapabilityNotServed(PlaybillSdkError):
    def __init__(self, *, code: str, capability: str, repair: str) -> None:
        self.code = code
        self.capability = capability
        self.repair = repair
        super().__init__(f"{capability} is not served. Repair: {repair}")


class ReferenceKindError(PlaybillSdkError):
    code = "playbill.sdk.reference_kind_mismatch"


class SourceSelectionError(PlaybillSdkError):
    code = "playbill.sdk.source_selection_refused"


class IncompatibleDaemonVersion(PlaybillSdkError):
    code = "playbill.sdk.daemon_version_incompatible"

    def __init__(
        self,
        *,
        client_version: str,
        daemon_version: str,
        expected_snapshot_digest: str,
        actual_snapshot_digest: str,
    ) -> None:
        self.client_version = client_version
        self.daemon_version = daemon_version
        self.expected_snapshot_digest = expected_snapshot_digest
        self.actual_snapshot_digest = actual_snapshot_digest
        super().__init__(
            f"client={client_version}, daemon={daemon_version}, "
            f"expected_snapshot={expected_snapshot_digest}, "
            f"actual_snapshot={actual_snapshot_digest}"
        )


__all__ = [
    "AccessProfile",
    "ActivationPolicy",
    "Audience",
    "BriefClaimExpectation",
    "BriefKind",
    "BriefQueryRender",
    "CallSite",
    "CanonicalValue",
    "CapabilityNotServed",
    "Cardinality",
    "ClaimObjectKind",
    "ClaimRef",
    "ClaimRole",
    "ClaimTypeRef",
    "DerivationSpec",
    "Diagnostic",
    "Disposition",
    "Duration",
    "EffectivePeriod",
    "IncompatibleDaemonVersion",
    "InsertionOperation",
    "PlaybillSdkError",
    "ProcedureRef",
    "QueryRef",
    "RefKind",
    "ReferenceKindError",
    "ReferentSensitivity",
    "SlotRef",
    "SourceMapEntry",
    "SourceRef",
    "SourceSelectionError",
    "SubjectRef",
    "TypedRef",
]
