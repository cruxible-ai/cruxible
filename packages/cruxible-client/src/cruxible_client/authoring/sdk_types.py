"""Public values for the synchronous Cruxible SDK.

Knowledge is governed state; code is how agents author changes to it.  These
types carry decisions and coordinate assertions, never authority of their own.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar, Literal, Protocol, runtime_checkable

from cruxible_client._error_base import CoreError
from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.capture_reads import CaptureRead
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc


class RefKind(str, Enum):
    SUBJECT = "subject"
    CLAIM_TYPE = "claim_type"
    CLAIM = "claim"
    PROCEDURE = "procedure"
    QUERY = "query"
    SOURCE = "source"
    DOCUMENT = "document"
    CAPTURE_CONTRACT = "capture_contract"
    PROPOSAL = "proposal"
    LINE = "line"
    CAPTURE = "capture"
    RESOLUTION_CONTRACT = "resolution_contract"
    MANDATE = "mandate"
    PROCEDURE_RUN = "procedure_run"
    TRIGGER = "trigger"
    BLUEPRINT = "blueprint"
    PRINCIPAL = "principal"
    APPROVAL_POLICY = "approval_policy"
    PROCEDURE_RUNTIME_POLICY = "procedure_runtime_policy"
    SOURCE_ACQUISITION_POLICY = "source_acquisition_policy"
    PROVIDER_INTERFACE = "provider_interface"


@runtime_checkable
class TypedRef(Protocol):
    @property
    def kind(self) -> RefKind: ...

    @property
    def address(self) -> str: ...

    @property
    def coordinate(self) -> AcceptedCoordinate: ...


class _ShortRefRepr:
    """``SubjectRef('sec.package/click' @ 0123456789ab)``: the address, and where it was read.

    A full coordinate is four digests; the git oid's first 12 hex name the
    accepted generation as ``get`` and ``orient`` print it.
    """

    address: str
    coordinate: AcceptedCoordinate

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.address!r} @ {self.coordinate.git_oid[:12]})"


@dataclass(frozen=True, repr=False)
class SubjectRef(_ShortRefRepr):
    """One accepted Subject at the coordinate it was read at.

    Pass it as ``subject=``, as a Subject-valued ``value=``, or to
    ``cx.get(ref)``. Next: ``cx.get(ref)`` for its fields and verdict flags.
    """

    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.SUBJECT


@dataclass(frozen=True, repr=False)
class ClaimTypeRef(_ShortRefRepr):
    """One accepted predicate at the coordinate it was read at.

    Pass it as ``predicate=`` or a write ``field``. Next: ``cx.get(ref)`` for
    its structure and meaning, or ``cx.query(kind, select=[...])`` for its values.
    """

    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.CLAIM_TYPE


@dataclass(frozen=True, repr=False)
class PendingSubjectRef(SubjectRef):
    """A Subject the same changeset defines, referenced before acceptance.

    A `SubjectRef` asserts "this address existed at this coordinate", and
    preflight verifies exactly that. A Subject its own set is defining did not
    exist there, so this ref deliberately mints no reference expectation: the
    set lowers the definition before the Claims that read it, and the daemon
    resolves the address inside the generation instead of against the base.
    """


@dataclass(frozen=True, repr=False)
class ClaimRef(_ShortRefRepr):
    """One accepted Claim at the coordinate it was read at.

    Next: ``cx.get(ref)`` for its value and verdict, ``cx.get(ref,
    detail="evidence")`` for what backs it, or ``cx.retire(ref, ...)`` to end it.
    """

    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.CLAIM


@dataclass(frozen=True, repr=False)
class PendingClaimTypeRef(ClaimTypeRef):
    """A ClaimType the same changeset defines, referenced before acceptance.

    Carries the object kind it was defined with so a Claim in the same set can
    be lowered without reading a ClaimType that is not accepted yet.
    """

    object_kind: str


@dataclass(frozen=True, repr=False)
class ProcedureRef(_ShortRefRepr):
    """One accepted Procedure at the coordinate it was read at.

    Next: ``cx.get(ref)`` for its inputs, readiness and track record, or
    ``cx.accepted_procedure(ref).run(...)`` to run it.
    """

    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.PROCEDURE


@dataclass(frozen=True, repr=False)
class QueryRef(_ShortRefRepr):
    """One accepted named query at the coordinate it was read at.

    Next: ``cx.query(name=ref, params={...})`` to run it, or ``cx.get(ref)``
    for its parameters.
    """

    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.QUERY


@dataclass(frozen=True, repr=False)
class SourceRef(_ShortRefRepr):
    """One catalogued workspace source at the coordinate it was read at.

    Next: ``cx.get(ref)`` for its Document's card, or its catalog entry when the
    entry is evidence-only.
    """

    address: str
    coordinate: AcceptedCoordinate
    kind: ClassVar[RefKind] = RefKind.SOURCE


@dataclass(frozen=True)
class CaptureRef:
    """Opaque accepted Capture plus its contract and citation-role provenance.

    Pass it as ``supported_by=`` to cite it. Next: ``cx.get(ref.handle)`` for
    the Capture card, or ``cx.capture(ref.capture_digest)`` for its material.
    """

    capture_digest: str
    contract_address: str
    coordinate: AcceptedCoordinate
    citation_role: Literal["evidence", "copy", "legacy"]

    @property
    def handle(self) -> str:
        """The ``CAP-<12 hex>`` handle every verb accepts. Next: ``cx.get(ref.handle)``."""

        return "CAP-" + self.capture_digest.partition(":")[2][:12]

    def __repr__(self) -> str:
        return (
            f"CaptureRef({self.handle} {self.citation_role} of {self.contract_address!r}"
            f" @ {self.coordinate.git_oid[:12]})"
        )


@dataclass(frozen=True)
class CaptureView:
    """Verified evidence metadata and explicitly available retained material."""

    result: CaptureRead

    @property
    def ref(self) -> CaptureRef:
        if (
            self.result.status != "verified"
            or self.result.contract_address is None
            or self.result.citation_role is None
        ):
            raise ValueError("Capture is unavailable; no verified reference can be issued")
        return CaptureRef(
            capture_digest=self.result.capture_digest,
            contract_address=self.result.contract_address,
            coordinate=self.result.coordinate,
            citation_role=self.result.citation_role,
        )

    @property
    def content(self) -> bytes:
        material = self.result.material
        if (
            material is None
            or material.status != "verified"
            or material.body_access is None
            or material.body_access.body_base64 is None
        ):
            raise ValueError("Capture content is unavailable; inspect result.material for details")
        return base64.b64decode(material.body_access.body_base64, validate=True)

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding)

    def json(self) -> object:
        return json.loads(self.content)


@dataclass(frozen=True)
class LiteralValue:
    """One literal object typed to the exact ClaimType that admits it.

    A bare `"high"` is admissible under every string-valued predicate, so the
    SDK cannot tell a severity from a status until the daemon reads it. This
    carries the predicate it was minted under, which is what lets the value be
    refused against the wrong ClaimType before the wire rather than after.
    """

    predicate: str
    value: CanonicalValue
    coordinate: AcceptedCoordinate


@dataclass(frozen=True)
class ExactContent:
    """The exact bytes a Claim's object IS, rather than a value that names them.

    Rulings and method laws are `exact_content` Claims: the object is not a
    literal the ClaimType admits and not an address, it is the text as written.
    The wire has carried that shape all along; the SDK could not spell it, so
    an author with 85 of them built them by hand through the compiled payload
    path instead.

    `content` takes `str` or `bytes` and keeps bytes: a Claim's object is
    exactly the bytes the daemon digests, and text is UTF-8 on the way in so
    that "as written" and "as digested" are the same thing.

    There is deliberately no media type. The accepted artifact carries a digest
    and a span and has nowhere to put one, so a media type accepted here would
    be dropped between this object and the ledger -- which is worse than not
    offering it, because the caller would believe it had been recorded.
    """

    content: bytes

    def __init__(self, content: bytes | str) -> None:
        if isinstance(content, str):
            object.__setattr__(self, "content", content.encode("utf-8"))
        elif isinstance(content, bytes):
            object.__setattr__(self, "content", content)
        else:
            raise TypeError("exact content must be str or bytes")


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


class Audience(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    BOTH = "both"


class ActivationPolicy(str, Enum):
    DRAIN = "drain"
    ABORT = "abort"
    SNAPSHOT = "snapshot"
    EPOCH_CHECK = "epoch-check"


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


class SdkError(CoreError, ValueError):
    code = "cruxible.sdk.refused"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class CapabilityNotServed(SdkError):
    def __init__(self, *, code: str, capability: str, repair: str) -> None:
        self.code = code
        self.capability = capability
        self.repair = repair
        super().__init__(f"{capability} is not served. Repair: {repair}")


class ReferenceKindError(SdkError):
    code = "cruxible.sdk.reference_kind_mismatch"


class AbsentSubject(SdkError):
    """No Subject of this kind carries this ID at the world's coordinate."""

    code = "cruxible.sdk.subject_absent_in_world"

    def __init__(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        coordinate: AcceptedCoordinate,
    ) -> None:
        self.subject_kind = subject_kind
        self.subject_id = subject_id
        self.coordinate = coordinate
        super().__init__(
            f"no accepted Subject {subject_kind}/{subject_id} exists at coordinate "
            f"{coordinate.git_oid}. Repair: define it in this changeset with "
            f"world.{subject_kind}.define({subject_id!r}), or search for the "
            "address the daemon actually accepted."
        )


class LiteralValueTypeError(SdkError):
    """A typed literal minted under one ClaimType was passed to another."""

    code = "cruxible.sdk.literal_value_claim_type_mismatch"

    def __init__(self, *, minted_under: str, passed_to: str) -> None:
        self.minted_under = minted_under
        self.passed_to = passed_to
        super().__init__(
            f"this value was minted under ClaimType {minted_under!r} and cannot state a "
            f"Claim under ClaimType {passed_to!r}. Repair: take the value from "
            f"the predicate you are stating -- world.{passed_to}."
        )


class ExactContentTypeError(SdkError):
    """Exact bytes were passed to a ClaimType whose object is not exact content."""

    code = "cruxible.sdk.exact_content_claim_type_mismatch"

    def __init__(self, *, predicate: str, object_kind: str) -> None:
        self.predicate = predicate
        self.object_kind = object_kind
        super().__init__(
            f"ClaimType {predicate!r} states a {object_kind} object, so it cannot carry the "
            "exact bytes of a body. Repair: state this under a predicate whose object_kind "
            f"is exact_content, or pass the {object_kind} value {predicate!r} admits."
        )


class ClaimRoleNotPermittedError(SdkError):
    """A Claim role its ClaimType does not permit, refused before anything is sent."""

    code = "cruxible.sdk.claim_role_not_permitted"

    def __init__(
        self,
        *,
        predicate: str,
        role: str,
        permitted_roles: tuple[str, ...],
        call_site: CallSite | None = None,
    ) -> None:
        self.predicate = predicate
        self.role = role
        self.permitted_roles = permitted_roles
        self.call_site = call_site
        location = (
            "" if call_site is None else f" (role= at {call_site.logical_file}:{call_site.line})"
        )
        super().__init__(
            f"ClaimType {predicate!r} does not permit role {role!r}{location}. "
            f"Repair: pass one of its permitted roles: {', '.join(permitted_roles)}."
        )


class LiteralSchemaError(SdkError):
    """A value refused by its ClaimType's declared literal schema."""

    code = "cruxible.sdk.literal_schema_violation"

    def __init__(self, *, predicate: str, reason: str) -> None:
        self.predicate = predicate
        self.reason = reason
        super().__init__(f"ClaimType {predicate!r} does not admit this value: {reason}")


class SourceSelectionError(SdkError):
    code = "cruxible.sdk.source_selection_refused"


class IncompatibleDaemonVersion(SdkError):
    code = "cruxible.sdk.daemon_version_incompatible"

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
        self.client_snapshot_digest = expected_snapshot_digest
        self.daemon_snapshot_digest = actual_snapshot_digest
        super().__init__(
            "Client and daemon authoring contracts are incompatible: "
            f"client_version={client_version}, daemon_version={daemon_version}, "
            f"client_snapshot_digest={expected_snapshot_digest}, "
            f"daemon_snapshot_digest={actual_snapshot_digest}. "
            "Repair: upgrade the client or daemon so both use the same authoring "
            "contract snapshot."
        )


__all__ = [
    "AbsentSubject",
    "AccessProfile",
    "ActivationPolicy",
    "Audience",
    "CallSite",
    "CanonicalValue",
    "CapabilityNotServed",
    "Cardinality",
    "ClaimObjectKind",
    "ClaimRef",
    "ClaimRole",
    "ClaimTypeRef",
    "Diagnostic",
    "Disposition",
    "Duration",
    "EffectivePeriod",
    "ExactContent",
    "ExactContentTypeError",
    "IncompatibleDaemonVersion",
    "LiteralSchemaError",
    "LiteralValue",
    "LiteralValueTypeError",
    "PendingClaimTypeRef",
    "PendingSubjectRef",
    "SdkError",
    "ProcedureRef",
    "QueryRef",
    "RefKind",
    "CaptureRef",
    "CaptureView",
    "ReferenceKindError",
    "ReferentSensitivity",
    "SourceMapEntry",
    "SourceRef",
    "SourceSelectionError",
    "SubjectRef",
    "TypedRef",
]
