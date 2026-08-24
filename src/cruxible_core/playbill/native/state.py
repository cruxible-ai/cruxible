"""What the lens renders *from*: accepted state, as an explicit input record.

The lens takes accepted state as a value rather than reaching for an instance,
and that is a design decision rather than a convenience. Render determinism is
the §11.9.6 law that everything else rests on, and a function of two values is
checkable by calling it twice; a function of a live instance is checkable only
by trusting that nothing underneath it moved. Nothing in this module opens a
repository, a projection, or a socket.

Every record here is a projection the served read surface already produces:
Claims, Subjects, ClaimTypes, QueryDefinitions, and Documents arrive as the
envelopes and facts those reads return, and the coverage boundary arrives as the
floor export's own §11.6.3 boundary. That is what lets one builder serve both
the CLI over a daemon and a headless caller over a local instance without two
implementations that can disagree.

Governance comes from accepted state, not from a fresh evaluation
----------------------------------------------------------------
A Claim's verdict is read from its accepted ``playbill.claim.current_verdict``
projection, which carries **its own** evaluation time. The lens renders that
time beside the verdict rather than implying the verdict was computed at the
render's read time, so "verdict at render: supported, evaluated at T,
generation G" states three separate facts and conflates none of them. It also
means rendering never needs to adjudicate anything, which keeps the single
verdict path in :mod:`cruxible_client.contracts.claim_verdicts` the only place a
verdict is ever computed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultV1
from cruxible_client.contracts.claims import ClaimArtifact
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.coverage.contracts import (
    CoverageManifestProfileV1,
    LogicalSourceIdentityV1,
    logical_sources_sorted,
)
from cruxible_core.playbill.native.grammar import NativeRenderError
from cruxible_core.playbill.projection import AcceptedCoordinate

CLAIM_STATEMENT_SCHEMA = "playbill.claim.statement"
CLAIM_BACKING_SCHEMA = "playbill.claim.backing"
CLAIM_LIFECYCLE_SCHEMA = "playbill.claim.lifecycle"
CLAIM_VERDICT_SCHEMA = "playbill.claim.current_verdict"

NativeArtifactKind = Literal["Subject", "ClaimType", "QueryDefinition", "Document"]


class _StrictStateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeCoverageBoundaryV1(_StrictStateModel):
    """The §11.6.3 boundary the render inherits, taken from the floor export.

    A render does not compute a coverage boundary of its own -- there is one
    coverage family and one evidence index, and re-deriving the boundary here
    would be the second index §11.9 forbids. The floor export already publishes
    it at the same accepted coordinate, so the render carries that boundary
    forward into its manifest unchanged.
    """

    tag: Literal["playbill-native-coverage-boundary-v1"] = "playbill-native-coverage-boundary-v1"
    index_digest: str
    access_profile_id: str
    completeness: Literal["complete", "partial"]
    truncation_reason_codes: tuple[str, ...] = ()
    scope: tuple[LogicalSourceIdentityV1, ...] = ()

    @field_validator("index_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("truncation_reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("boundary truncation reason codes must be sorted and unique")
        return value

    @field_validator("scope")
    @classmethod
    def _scope(
        cls, value: tuple[LogicalSourceIdentityV1, ...]
    ) -> tuple[LogicalSourceIdentityV1, ...]:
        if value != logical_sources_sorted(value):
            raise ValueError("boundary scope sources must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _completeness_is_explained(self) -> "NativeCoverageBoundaryV1":
        if (self.completeness == "partial") != bool(self.truncation_reason_codes):
            raise ValueError("boundary completeness must agree with its truncation reasons")
        return self


class NativeArtifactRecordV1(_StrictStateModel):
    """One accepted non-Claim artifact, as the lens needs to see it."""

    tag: Literal["playbill-native-artifact-record-v1"] = "playbill-native-artifact-record-v1"
    kind: NativeArtifactKind
    path: str
    identity: str
    artifact_digest: str
    envelope: dict[str, Any] = Field(default_factory=dict)

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @property
    def address(self) -> SemanticAddress:
        return SemanticAddress.whole_artifact(self.path)


class NativeClaimRecordV1(_StrictStateModel):
    """One accepted Claim, with the accepted verdict projection beside it.

    ``verdict`` is optional because a Claim's accepted verdict projection is a
    fact about the generation that accepted it, and a caller reading a surface
    that does not project one has an honest absence rather than a licence to
    compute one. The lens renders the absence as an absence.
    """

    tag: Literal["playbill-native-claim-record-v1"] = "playbill-native-claim-record-v1"
    path: str
    artifact_digest: str
    claim: ClaimArtifact
    verdict: ClaimVerdictResultV1 | None = None

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @property
    def address(self) -> SemanticAddress:
        return SemanticAddress.claim_statement(self.path)


class NativeAcceptedStateV1(_StrictStateModel):
    """Everything at one accepted coordinate that the default lens renders."""

    tag: Literal["playbill-native-accepted-state-v1"] = "playbill-native-accepted-state-v1"
    instance_id: str
    at: AcceptedCoordinate
    boundary: NativeCoverageBoundaryV1
    subjects: tuple[NativeArtifactRecordV1, ...] = ()
    claim_types: tuple[NativeArtifactRecordV1, ...] = ()
    query_definitions: tuple[NativeArtifactRecordV1, ...] = ()
    documents: tuple[NativeArtifactRecordV1, ...] = ()
    claims: tuple[NativeClaimRecordV1, ...] = ()

    @field_validator("subjects", "claim_types", "query_definitions", "documents")
    @classmethod
    def _artifacts(
        cls, value: tuple[NativeArtifactRecordV1, ...]
    ) -> tuple[NativeArtifactRecordV1, ...]:
        paths = tuple(item.path.encode("utf-8") for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("accepted artifact records must be byte-sorted and unique by path")
        return value

    @field_validator("claims")
    @classmethod
    def _claims(cls, value: tuple[NativeClaimRecordV1, ...]) -> tuple[NativeClaimRecordV1, ...]:
        paths = tuple(item.path.encode("utf-8") for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("accepted Claim records must be byte-sorted and unique by path")
        return value

    def claims_for_subject(self, subject_path: str) -> tuple[NativeClaimRecordV1, ...]:
        return tuple(
            item
            for item in self.claims
            if item.claim.statement.subject.artifact_path == subject_path
        )

    @property
    def subject_paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.subjects)


# -- building the state from served projections ---------------------------


def _fact(facts: Sequence[Mapping[str, Any]], schema_id: str) -> Any:
    for fact in facts:
        if fact.get("schema_id") == schema_id:
            return fact.get("value")
    return None


def claim_from_projection(
    envelope: Mapping[str, Any],
    facts: Sequence[Mapping[str, Any]],
) -> ClaimArtifact:
    """Rebuild one accepted Claim artifact from its projection envelope and facts.

    The projection splits an artifact into an envelope and typed facts; this
    reassembles exactly the canonical artifact and refuses anything partial,
    because a Claim missing its statement, backing, or lifecycle is not a Claim
    that can be rendered as one.
    """

    identity = envelope.get("identity")
    statement = _fact(facts, CLAIM_STATEMENT_SCHEMA)
    backing = _fact(facts, CLAIM_BACKING_SCHEMA)
    lifecycle = _fact(facts, CLAIM_LIFECYCLE_SCHEMA)
    if not (
        isinstance(identity, str)
        and isinstance(statement, dict)
        and isinstance(backing, dict)
        and isinstance(lifecycle, dict)
    ):
        raise NativeRenderError("Claim projection lacks its complete canonical artifact")
    try:
        return ClaimArtifact.model_validate(
            {
                "identity": {"kind": "Claim", "name": identity.removeprefix("Claim:")},
                "statement": statement,
                "backing": backing,
                "authority": lifecycle.get("authority"),
                "pins": lifecycle.get("pins"),
                "lifecycle": lifecycle.get("lifecycle"),
            }
        )
    except ValueError as exc:
        raise NativeRenderError(f"Claim projection does not reassemble: {exc}") from exc


def verdict_from_projection(facts: Sequence[Mapping[str, Any]]) -> ClaimVerdictResultV1 | None:
    """Read the accepted verdict projection, or report its honest absence."""

    value = _fact(facts, CLAIM_VERDICT_SCHEMA)
    if not isinstance(value, dict):
        return None
    try:
        return ClaimVerdictResultV1.model_validate(value)
    except ValueError as exc:
        raise NativeRenderError(f"accepted Claim verdict projection is malformed: {exc}") from exc


def claim_record_from_projection(
    envelope: Mapping[str, Any],
    facts: Sequence[Mapping[str, Any]],
) -> NativeClaimRecordV1:
    """Turn one projected Claim read into the record the lens renders."""

    path = envelope.get("path")
    digest = envelope.get("artifact_digest")
    if not isinstance(path, str) or not isinstance(digest, str):
        raise NativeRenderError("Claim projection envelope names no path or artifact digest")
    return NativeClaimRecordV1(
        path=path,
        artifact_digest=digest,
        claim=claim_from_projection(envelope, facts),
        verdict=verdict_from_projection(facts),
    )


def artifact_record_from_projection(
    kind: NativeArtifactKind,
    envelope: Mapping[str, Any],
    *,
    path: str | None = None,
    identity: str | None = None,
    artifact_digest: str | None = None,
) -> NativeArtifactRecordV1:
    """Turn one projected non-Claim read into the record the lens renders.

    Two read shapes exist on the served surface: Subjects and Documents carry
    their path and digest *inside* the projection envelope, while ClaimTypes and
    QueryDefinitions carry them beside a whole canonical artifact envelope. Both
    reduce to the same record, so the caller passes whichever it holds.
    """

    resolved_path = path if path is not None else envelope.get("path")
    resolved_identity = identity if identity is not None else envelope.get("identity")
    resolved_digest = (
        artifact_digest if artifact_digest is not None else envelope.get("artifact_digest")
    )
    if not (
        isinstance(resolved_path, str)
        and isinstance(resolved_identity, str)
        and isinstance(resolved_digest, str)
    ):
        raise NativeRenderError(f"{kind} projection names no path, identity, or artifact digest")
    return NativeArtifactRecordV1(
        kind=kind,
        path=resolved_path,
        identity=resolved_identity,
        artifact_digest=resolved_digest,
        envelope=dict(envelope),
    )


def native_boundary_from_floor(manifest: Mapping[str, Any]) -> NativeCoverageBoundaryV1:
    """Carry the floor export's §11.6.3 boundary into the render unchanged."""

    try:
        return NativeCoverageBoundaryV1(
            index_digest=str(manifest["index_digest"]),
            access_profile_id=str(manifest["access_profile_id"]),
            completeness=manifest["completeness"],
            truncation_reason_codes=tuple(manifest.get("truncation_reason_codes") or ()),
            scope=tuple(
                LogicalSourceIdentityV1.model_validate(item) for item in manifest.get("scope") or ()
            ),
        )
    except (KeyError, ValueError) as exc:
        raise NativeRenderError(f"floor coverage boundary is not readable: {exc}") from exc


def native_boundary_from_manifest(
    manifest: CoverageManifestProfileV1,
) -> NativeCoverageBoundaryV1:
    """Recover the boundary a committed render was computed over, from itself.

    The render manifest is a §11.6.3 profile, so it already carries every field
    of the boundary it inherited. Recovering it here rather than re-exporting the
    floor is what lets a compile reconstruct its baseline at the generation the
    tree was rendered from even after the head has moved: a fresh floor export
    answers about the head, which is the wrong question to ask of a baseline.
    """

    return NativeCoverageBoundaryV1(
        index_digest=manifest.index_digest,
        access_profile_id=manifest.access_profile_id,
        completeness=manifest.completeness,
        truncation_reason_codes=manifest.truncation_reason_codes,
        scope=manifest.scope,
    )


def build_native_state(
    *,
    instance_id: str,
    at: AcceptedCoordinate,
    boundary: NativeCoverageBoundaryV1,
    subjects: Iterable[NativeArtifactRecordV1] = (),
    claim_types: Iterable[NativeArtifactRecordV1] = (),
    query_definitions: Iterable[NativeArtifactRecordV1] = (),
    documents: Iterable[NativeArtifactRecordV1] = (),
    claims: Iterable[NativeClaimRecordV1] = (),
) -> NativeAcceptedStateV1:
    """Assemble the render input, ordering every collection canonically."""

    def ordered(
        values: Iterable[NativeArtifactRecordV1],
    ) -> tuple[NativeArtifactRecordV1, ...]:
        return tuple(sorted(values, key=lambda item: item.path.encode("utf-8")))

    return NativeAcceptedStateV1(
        instance_id=instance_id,
        at=at,
        boundary=boundary,
        subjects=ordered(subjects),
        claim_types=ordered(claim_types),
        query_definitions=ordered(query_definitions),
        documents=ordered(documents),
        claims=tuple(sorted(claims, key=lambda item: item.path.encode("utf-8"))),
    )


__all__ = [
    "CLAIM_BACKING_SCHEMA",
    "CLAIM_LIFECYCLE_SCHEMA",
    "CLAIM_STATEMENT_SCHEMA",
    "CLAIM_VERDICT_SCHEMA",
    "NativeAcceptedStateV1",
    "NativeArtifactKind",
    "NativeArtifactRecordV1",
    "NativeClaimRecordV1",
    "NativeCoverageBoundaryV1",
    "artifact_record_from_projection",
    "build_native_state",
    "claim_from_projection",
    "claim_record_from_projection",
    "native_boundary_from_floor",
    "native_boundary_from_manifest",
    "verdict_from_projection",
]
