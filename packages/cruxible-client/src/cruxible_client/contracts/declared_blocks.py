"""Shared, strictly canonical contracts for locally declared projection blocks."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.grammar import QueryValueTypeV1
from cruxible_client.contracts.temporal import ensure_utc

PROJECTION_MARKER_GRAMMAR: Literal["playbill-projection-marker-grammar-v1"] = (
    "playbill-projection-marker-grammar-v1"
)
PROJECTION_QUERY_SEMANTIC_RESULT_DOMAIN = "playbill-projection-query-semantic-result-v1"
PROJECTION_QUERY_PARAMETER_DOMAIN = "playbill-query-parameters-v1"
MAX_PROJECTION_SOURCE_BYTES = 4 * 1024 * 1024
MAX_PROJECTION_BLOCKS_PER_SOURCE = 128
MAX_PROJECTION_STAMP_BYTES = 16 * 1024
MAX_PROJECTION_BACKINGS_PER_BLOCK = 64
MAX_PROJECTION_SCAN_BYTES = 32 * 1024 * 1024
MAX_PROJECTION_CARDS_PER_SOURCE = 256
MAX_PROJECTION_COVERAGE_BINDINGS = 1024

_BLOCK_ID = rb"[a-z][a-z0-9_.-]{0,63}"
_STAMPED_OPEN = re.compile(rb"<!-- playbill:block:(" + _BLOCK_ID + rb"):([A-Za-z0-9_-]+) -->\n")
_BOOTSTRAP_OPEN = re.compile(rb"<!-- playbill:block:(" + _BLOCK_ID + rb") -->\n")
_CLOSE = re.compile(rb"<!-- /playbill:block:(" + _BLOCK_ID + rb") -->\n")
_FENCE_OPEN = re.compile(rb" {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$")


class _StrictDeclaredBlockModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillPresentationPolicyV1(_StrictDeclaredBlockModel):
    """Local-only suppression policy for presentation diagnostics."""

    tag: Literal["playbill-presentation-policy-v1"] = "playbill-presentation-policy-v1"
    archival_source_ids: tuple[str, ...] = ()

    @field_validator("archival_source_ids")
    @classmethod
    def _source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        source_pattern = r"^[a-z][a-z0-9_.-]{0,127}$"
        if any(re.fullmatch(source_pattern, item) is None for item in value):
            raise ValueError("presentation policy contains an invalid source ID")
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("archival source IDs must be UTF-8-byte-sorted and unique")
        return value


class PlaybillProjectionAdvisoryPolicyV1(_StrictDeclaredBlockModel):
    """Per-artifact-kind switches for local projection advisories."""

    claim: bool = False
    procedure: bool = True


class PlaybillPresentationPolicyV2(_StrictDeclaredBlockModel):
    """Current local-only presentation policy; V1 remains readable."""

    tag: Literal["playbill-presentation-policy-v2"] = "playbill-presentation-policy-v2"
    archival_source_ids: tuple[str, ...] = ()
    projection_advisories: PlaybillProjectionAdvisoryPolicyV1 = PlaybillProjectionAdvisoryPolicyV1()

    @field_validator("archival_source_ids")
    @classmethod
    def _source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return PlaybillPresentationPolicyV1._source_ids(value)


PlaybillPresentationPolicyAny: TypeAlias = (
    PlaybillPresentationPolicyV1 | PlaybillPresentationPolicyV2
)


def upgrade_playbill_presentation_policy(
    policy: PlaybillPresentationPolicyAny,
) -> PlaybillPresentationPolicyV2:
    if isinstance(policy, PlaybillPresentationPolicyV2):
        return policy
    return PlaybillPresentationPolicyV2(archival_source_ids=policy.archival_source_ids)


PlaybillPresentationPolicyNoteV1: TypeAlias = Literal[
    "presentation_policy_malformed",
    "presentation_policy_path_escape",
    "presentation_policy_unknown_source_id",
    "presentation_policy_unreadable",
]


class PlaybillProjectionCoverageBindingV1(_StrictDeclaredBlockModel):
    """One client-observed mapping from governed identity to local projection path."""

    artifact: ArtifactIdentity
    workspace_path: str = Field(min_length=1, max_length=4096)
    evidence_kind: Literal["claim_marker", "procedure_catalog"]

    @model_validator(mode="after")
    def _shape(self) -> "PlaybillProjectionCoverageBindingV1":
        if self.artifact.kind not in {"Claim", "Procedure"}:
            raise ValueError("projection coverage may identify only a Claim or Procedure")
        expected = "claim_marker" if self.artifact.kind == "Claim" else "procedure_catalog"
        if self.evidence_kind != expected:
            raise ValueError("projection coverage evidence kind disagrees with artifact kind")
        return self


class PlaybillProjectionCoverageObservationV1(_StrictDeclaredBlockModel):
    """Bounded local projection evidence at one exact accepted coordinate."""

    tag: Literal["playbill-projection-coverage-observation-v1"] = (
        "playbill-projection-coverage-observation-v1"
    )
    coordinate: AcceptedCoordinate
    complete_kinds: tuple[Literal["Claim", "Procedure"], ...]
    bindings: tuple[PlaybillProjectionCoverageBindingV1, ...] = Field(
        max_length=MAX_PROJECTION_COVERAGE_BINDINGS
    )

    @field_validator("complete_kinds")
    @classmethod
    def _complete_kinds(
        cls, value: tuple[Literal["Claim", "Procedure"], ...]
    ) -> tuple[Literal["Claim", "Procedure"], ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("complete projection kinds must be sorted and unique")
        return value

    @field_validator("bindings")
    @classmethod
    def _bindings(
        cls, value: tuple[PlaybillProjectionCoverageBindingV1, ...]
    ) -> tuple[PlaybillProjectionCoverageBindingV1, ...]:
        keys = tuple(
            (item.artifact.qualified, item.workspace_path, item.evidence_kind) for item in value
        )
        if keys != tuple(
            sorted(set(keys), key=lambda item: tuple(v.encode("utf-8") for v in item))
        ):
            raise ValueError("projection coverage bindings must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _binding_kinds(self) -> "PlaybillProjectionCoverageObservationV1":
        if any(item.artifact.kind not in self.complete_kinds for item in self.bindings):
            raise ValueError("projection bindings require complete observation of their kind")
        return self


class PlaybillReviewWorkspaceObservationV1(_StrictDeclaredBlockModel):
    """Review-scoped subset of local projection facts; no daemon path access."""

    tag: Literal["playbill-review-workspace-observation-v1"] = (
        "playbill-review-workspace-observation-v1"
    )
    presentation_policy: PlaybillPresentationPolicyAny | None = None
    presentation_policy_notes: tuple[PlaybillPresentationPolicyNoteV1, ...] = ()
    projection_coverage: PlaybillProjectionCoverageObservationV1 | None = None

    @field_validator("presentation_policy_notes")
    @classmethod
    def _notes(
        cls, value: tuple[PlaybillPresentationPolicyNoteV1, ...]
    ) -> tuple[PlaybillPresentationPolicyNoteV1, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("presentation-policy notes must be sorted and unique")
        return value


class ProjectionClaimBackingV1(_StrictDeclaredBlockModel):
    tag: Literal["playbill-projection-claim-backing-v1"] = "playbill-projection-claim-backing-v1"
    identity: ArtifactIdentity
    statement_digest: str

    @field_validator("statement_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _identity(self) -> ProjectionClaimBackingV1:
        if self.identity.kind != "Claim":
            raise ValueError("a projection Claim backing must identify a Claim")
        return self


class ProjectionResolvedParameterBindingV1(_StrictDeclaredBlockModel):
    """The exact existing query-parameter-binding wire spelling, shared by both sides."""

    tag: Literal["playbill-query-parameter-binding-v1"] = "playbill-query-parameter-binding-v1"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value_type: QueryValueTypeV1
    value: object = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


def projection_parameter_digest(
    parameters: tuple[ProjectionResolvedParameterBindingV1, ...],
) -> str:
    return typed_digest(
        Sha256Value,
        PROJECTION_QUERY_PARAMETER_DOMAIN,
        {"parameters": [item.model_dump(mode="json") for item in parameters]},
    ).tagged


class ProjectionQueryBackingV1(_StrictDeclaredBlockModel):
    tag: Literal["playbill-projection-query-backing-v1"] = "playbill-projection-query-backing-v1"
    identity: ArtifactIdentity
    definition_digest: str
    resolved_parameter_bindings: tuple[ProjectionResolvedParameterBindingV1, ...] = ()
    canonical_param_digest: str
    declared_evaluation_time: datetime
    semantic_result_digest: str

    @field_validator("definition_digest", "canonical_param_digest", "semantic_result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("declared_evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("a projection query backing requires an absolute evaluation time")
        return ensure_utc(value)

    @model_validator(mode="after")
    def _bindings(self) -> ProjectionQueryBackingV1:
        if self.identity.kind != "QueryDefinition":
            raise ValueError("a projection query backing must identify a QueryDefinition")
        names = tuple(item.name for item in self.resolved_parameter_bindings)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("projection query parameter bindings must be sorted and unique")
        if (
            projection_parameter_digest(self.resolved_parameter_bindings)
            != self.canonical_param_digest
        ):
            raise ValueError("projection query parameter digest does not reproduce its bindings")
        return self


ProjectionBackingV1: TypeAlias = Annotated[
    ProjectionClaimBackingV1 | ProjectionQueryBackingV1,
    Field(discriminator="tag"),
]


class ProjectionBlockStampV1(_StrictDeclaredBlockModel):
    tag: Literal["playbill-projection-stamp-v1"] = "playbill-projection-stamp-v1"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    block_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    declared_generation: int = Field(ge=0)
    declared_coordinate: AcceptedCoordinate
    backing: tuple[ProjectionBackingV1, ...] = Field(
        min_length=1,
        max_length=MAX_PROJECTION_BACKINGS_PER_BLOCK,
    )
    body_digest: str
    grammar_version: Literal["playbill-projection-marker-grammar-v1"] = PROJECTION_MARKER_GRAMMAR

    @field_validator("body_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("backing")
    @classmethod
    def _backing(cls, value: tuple[ProjectionBackingV1, ...]) -> tuple[ProjectionBackingV1, ...]:
        identities = tuple(item.identity.qualified for item in value)
        if identities != tuple(sorted(set(identities), key=lambda item: item.encode("utf-8"))):
            raise ValueError("projection block backings must be sorted and unique by identity")
        return value


class ProjectionMarkerSummaryV1(_StrictDeclaredBlockModel):
    stamp: ProjectionBlockStampV1
    observed_body_digest: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)

    @field_validator("observed_body_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _window(self) -> ProjectionMarkerSummaryV1:
        if self.end_byte <= self.start_byte:
            raise ValueError("projection marker summary byte range must be increasing")
        return self


class ProjectionMarkerError(PlaybillError):
    code = "playbill.projection.marker_invalid"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class ProjectionBootstrapUnstampedError(ProjectionMarkerError):
    code = "playbill.projection.bootstrap_unstamped"


@dataclass(frozen=True)
class ParsedProjectionBlock:
    source_id: str
    block_id: str
    stamp: ProjectionBlockStampV1 | None
    opening_start: int
    opening_end: int
    body_start: int
    body_end: int
    closing_end: int
    body_digest: str

    def summary(self) -> ProjectionMarkerSummaryV1:
        if self.stamp is None:
            raise ProjectionBootstrapUnstampedError(
                "an unstamped bootstrap block is not a declaration"
            )
        return ProjectionMarkerSummaryV1(
            stamp=self.stamp,
            observed_body_digest=self.body_digest,
            start_byte=self.opening_start,
            end_byte=self.closing_end,
        )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"projection stamp repeats JSON object key {key!r}")
        result[key] = value
    return result


def _decode_projection_stamp(
    encoded: bytes,
    *,
    source_id: str,
    block_id: str,
) -> ProjectionBlockStampV1:
    if len(encoded) > (MAX_PROJECTION_STAMP_BYTES * 4 + 2) // 3:
        raise ProjectionMarkerError("projection stamp exceeds its decoded byte ceiling")
    try:
        padding = b"=" * (-len(encoded) % 4)
        content = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProjectionMarkerError("projection stamp is not canonical base64url") from exc
    if len(content) > MAX_PROJECTION_STAMP_BYTES:
        raise ProjectionMarkerError("projection stamp exceeds its decoded byte ceiling")
    if base64.urlsafe_b64encode(content).rstrip(b"=") != encoded:
        raise ProjectionMarkerError("projection stamp base64url spelling is not minimal")
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_json_object)
        if canonical_bytes(value) != content:
            raise ValueError("projection stamp does not reproduce canonical JSON bytes")
        stamp = ProjectionBlockStampV1.model_validate(value)
    except (UnicodeError, ValueError, ValidationError, PlaybillError) as exc:
        raise ProjectionMarkerError(f"projection stamp is malformed: {exc}") from exc
    if stamp.source_id != source_id or stamp.block_id != block_id:
        raise ProjectionMarkerError("projection stamp source or block differs from its marker")
    return stamp


def render_projection_opening(stamp: ProjectionBlockStampV1) -> bytes:
    content = canonical_bytes(stamp.model_dump(mode="json"))
    if len(content) > MAX_PROJECTION_STAMP_BYTES:
        raise ProjectionMarkerError("projection stamp exceeds its decoded byte ceiling")
    encoded = base64.urlsafe_b64encode(content).rstrip(b"=")
    return b"<!-- playbill:block:" + stamp.block_id.encode("ascii") + b":" + encoded + b" -->\n"


def render_projection_closing(block_id: str) -> bytes:
    if re.fullmatch(_BLOCK_ID, block_id.encode("ascii", errors="strict")) is None:
        raise ProjectionMarkerError("projection block ID is malformed")
    return b"<!-- /playbill:block:" + block_id.encode("ascii") + b" -->\n"


def parse_projection_blocks(
    content: bytes,
    *,
    source_id: str,
    allow_bootstrap: bool = False,
) -> tuple[ParsedProjectionBlock, ...]:
    """Parse one complete source, refusing every ambiguous declaration boundary."""

    if len(content) > MAX_PROJECTION_SOURCE_BYTES:
        raise ProjectionMarkerError("projection source exceeds its 4 MiB byte ceiling")
    try:
        content.decode("utf-8")
    except UnicodeError as exc:
        raise ProjectionMarkerError("projection source is not valid UTF-8") from exc

    fence_character: int | None = None
    fence_length = 0
    active: tuple[str, ProjectionBlockStampV1 | None, int, int] | None = None
    seen: set[str] = set()
    blocks: list[ParsedProjectionBlock] = []
    offset = 0
    for line in content.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        fence = _FENCE_OPEN.fullmatch(line)
        if fence_character is not None:
            if fence is not None:
                run = fence.group(1)
                if (
                    run[0] == fence_character
                    and len(run) >= fence_length
                    and not fence.group(2).strip()
                ):
                    fence_character = None
                    fence_length = 0
            continue
        if fence is not None:
            run = fence.group(1)
            if run[0] != ord("`") or b"`" not in fence.group(2):
                fence_character = run[0]
                fence_length = len(run)
                continue

        candidate = line.lstrip(b" ")
        if not (
            candidate.startswith(b"<!-- playbill:block:")
            or candidate.startswith(b"<!-- /playbill:block:")
        ):
            continue
        if line != candidate:
            raise ProjectionMarkerError("projection marker must begin at column zero")
        if line.endswith(b"\r\n"):
            raise ProjectionMarkerError("projection marker must use an LF-only line ending")
        stamped = _STAMPED_OPEN.fullmatch(line)
        bootstrap = _BOOTSTRAP_OPEN.fullmatch(line)
        closing = _CLOSE.fullmatch(line)
        if stamped is None and bootstrap is None and closing is None:
            raise ProjectionMarkerError("projection marker has malformed grammar")
        if closing is not None:
            block_id = closing.group(1).decode("ascii")
            if active is None or active[0] != block_id:
                raise ProjectionMarkerError("projection marker closes an absent or different block")
            active_id, stamp, opening_start, body_start = active
            body = content[body_start:line_start]
            blocks.append(
                ParsedProjectionBlock(
                    source_id=source_id,
                    block_id=active_id,
                    stamp=stamp,
                    opening_start=opening_start,
                    opening_end=body_start,
                    body_start=body_start,
                    body_end=line_start,
                    closing_end=offset,
                    body_digest="sha256:" + hashlib.sha256(body).hexdigest(),
                )
            )
            active = None
            continue
        if active is not None:
            raise ProjectionMarkerError("projection blocks cannot nest or overlap")
        opening = stamped if stamped is not None else bootstrap
        assert opening is not None
        block_id = opening.group(1).decode("ascii")
        if block_id in seen:
            raise ProjectionMarkerError(f"projection source repeats block identity {block_id!r}")
        if len(seen) >= MAX_PROJECTION_BLOCKS_PER_SOURCE:
            raise ProjectionMarkerError("projection source exceeds its 128-block ceiling")
        seen.add(block_id)
        if stamped is not None:
            stamp = _decode_projection_stamp(
                stamped.group(2), source_id=source_id, block_id=block_id
            )
        elif allow_bootstrap:
            stamp = None
        else:
            raise ProjectionBootstrapUnstampedError(
                "an unstamped bootstrap block is not a declaration"
            )
        active = (block_id, stamp, line_start, offset)
    if active is not None:
        raise ProjectionMarkerError("projection block opening has no matching closing marker")
    return tuple(blocks)


def frame_projection_block(*, stamp: ProjectionBlockStampV1, body: bytes) -> bytes:
    """Mechanically frame accepted bytes and prove the one frozen marker grammar."""

    try:
        body.decode("utf-8")
    except UnicodeError as exc:
        raise ProjectionMarkerError("projection body is not valid UTF-8") from exc
    if not body.endswith(b"\n"):
        raise ProjectionMarkerError("projection body must end with LF")
    framed = render_projection_opening(stamp) + body + render_projection_closing(stamp.block_id)
    blocks = parse_projection_blocks(framed, source_id=stamp.source_id)
    if len(blocks) != 1 or blocks[0].stamp != stamp or blocks[0].body_digest != stamp.body_digest:
        raise ProjectionMarkerError("framed projection body is ambiguous under the marker grammar")
    return framed


def assert_projection_block_frame(
    content: bytes,
    *,
    source_id: str,
    block_id: str,
    stamp: ProjectionBlockStampV1,
    body_digest: str,
    start_byte: int | None = None,
    end_byte: int | None = None,
) -> ParsedProjectionBlock:
    """Return the one exact block or refuse a malformed sanctioned write."""

    matches = tuple(
        block
        for block in parse_projection_blocks(content, source_id=source_id)
        if block.block_id == block_id
    )
    if len(matches) != 1:
        raise ProjectionMarkerError("sanctioned write does not contain exactly one target block")
    block = matches[0]
    if block.stamp != stamp or block.body_digest != body_digest:
        raise ProjectionMarkerError("sanctioned write does not reproduce its committed block")
    if start_byte is not None and block.opening_start != start_byte:
        raise ProjectionMarkerError("sanctioned write moved the committed block start")
    if end_byte is not None and block.closing_end != end_byte:
        raise ProjectionMarkerError("sanctioned write moved the committed block end")
    return block


def projection_query_semantic_result_digest(result: object) -> str:
    """Commit result meaning only, excluding coordinate, clock, receipt, and prose."""

    payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    if not isinstance(payload, dict):
        raise ValueError("a projection query semantic result must be an object")
    fields = (
        "rows",
        "conflicts",
        "result_shape",
        "result_cardinality",
        "result_binding",
        "dedupe",
    )
    if any(field not in payload for field in fields):
        raise ValueError("a projection query semantic result omits a required result field")
    return typed_digest(
        Sha256Value,
        PROJECTION_QUERY_SEMANTIC_RESULT_DOMAIN,
        {field: payload[field] for field in fields},
    ).tagged


__all__ = [
    "MAX_PROJECTION_BACKINGS_PER_BLOCK",
    "MAX_PROJECTION_BLOCKS_PER_SOURCE",
    "MAX_PROJECTION_CARDS_PER_SOURCE",
    "MAX_PROJECTION_COVERAGE_BINDINGS",
    "MAX_PROJECTION_SCAN_BYTES",
    "MAX_PROJECTION_SOURCE_BYTES",
    "MAX_PROJECTION_STAMP_BYTES",
    "PROJECTION_MARKER_GRAMMAR",
    "PROJECTION_QUERY_PARAMETER_DOMAIN",
    "PROJECTION_QUERY_SEMANTIC_RESULT_DOMAIN",
    "PlaybillPresentationPolicyAny",
    "PlaybillPresentationPolicyV1",
    "PlaybillPresentationPolicyV2",
    "PlaybillProjectionAdvisoryPolicyV1",
    "PlaybillProjectionCoverageBindingV1",
    "PlaybillProjectionCoverageObservationV1",
    "PlaybillReviewWorkspaceObservationV1",
    "ParsedProjectionBlock",
    "ProjectionBackingV1",
    "ProjectionBlockStampV1",
    "ProjectionBootstrapUnstampedError",
    "ProjectionClaimBackingV1",
    "ProjectionMarkerSummaryV1",
    "ProjectionMarkerError",
    "ProjectionQueryBackingV1",
    "ProjectionResolvedParameterBindingV1",
    "assert_projection_block_frame",
    "frame_projection_block",
    "parse_projection_blocks",
    "projection_parameter_digest",
    "projection_query_semantic_result_digest",
    "render_projection_closing",
    "render_projection_opening",
    "upgrade_playbill_presentation_policy",
]
