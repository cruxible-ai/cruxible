"""Client-owned Markdown declarations; the machine never writes block prose."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast

from pydantic import ValidationError

from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes, normalize_canonical
from cruxible_client.contracts.claims import ClaimStatement, claim_statement_digest
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_BLOCKS_PER_SOURCE,
    MAX_PROJECTION_SOURCE_BYTES,
    MAX_PROJECTION_STAMP_BYTES,
    ProjectionBackingV1,
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerSummaryV1,
    ProjectionQueryBackingV1,
    ProjectionResolvedParameterBindingV1,
    projection_parameter_digest,
    projection_query_semantic_result_digest,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_client.transport.http import CruxibleClient

_BLOCK_ID = rb"[a-z][a-z0-9_.-]{0,63}"
_STAMPED_OPEN = re.compile(rb"<!-- playbill:block:(" + _BLOCK_ID + rb"):([A-Za-z0-9_-]+) -->\n")
_BOOTSTRAP_OPEN = re.compile(rb"<!-- playbill:block:(" + _BLOCK_ID + rb") -->\n")
_CLOSE = re.compile(rb"<!-- /playbill:block:(" + _BLOCK_ID + rb") -->\n")
_FENCE_OPEN = re.compile(rb" {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$")


class ProjectionMarkerError(PlaybillError):
    code = "playbill.projection.marker_invalid"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class ProjectionIndependentEvidenceForbidden(PlaybillError):
    code = "playbill.projection.independent_evidence_forbidden"

    def __init__(
        self,
        *,
        source_id: str,
        block_id: str,
        start_byte: int,
        end_byte: int,
    ) -> None:
        self.source_id = source_id
        self.block_id = block_id
        self.start_byte = start_byte
        self.end_byte = end_byte
        super().__init__(
            f"{self.code}: source {source_id!r} block {block_id!r} intersects "
            f"selected bytes [{start_byte}, {end_byte}); cite the underlying claim, "
            "or author an explicit copy citation"
        )


class ProjectionRepinError(PlaybillError):
    code = "playbill.projection.repin_refused"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


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
            raise ProjectionMarkerError("an unstamped bootstrap block is not a declaration")
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


def _decode_stamp(encoded: bytes, *, source_id: str, block_id: str) -> ProjectionBlockStampV1:
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
            stamp = _decode_stamp(stamped.group(2), source_id=source_id, block_id=block_id)
        elif allow_bootstrap:
            stamp = None
        else:
            raise ProjectionMarkerError("an unstamped bootstrap block is not a declaration")
        active = (block_id, stamp, line_start, offset)
    if active is not None:
        raise ProjectionMarkerError("projection block opening has no matching closing marker")
    return tuple(blocks)


def assert_independent_projection_evidence(
    *,
    source_id: str,
    content: bytes,
    start_byte: int,
    end_byte: int,
) -> None:
    for block in parse_projection_blocks(content, source_id=source_id):
        if start_byte < block.closing_end and end_byte > block.opening_start:
            raise ProjectionIndependentEvidenceForbidden(
                source_id=source_id,
                block_id=block.block_id,
                start_byte=start_byte,
                end_byte=end_byte,
            )


def _claim_backing(
    client: CruxibleClient,
    instance_id: str,
    *,
    name: str,
    coordinate: AcceptedCoordinate,
    evaluation_time: str,
) -> ProjectionClaimBackingV1:
    bare = name.removeprefix("Claim:")
    view = client.get_playbill_claim(
        instance_id,
        bare,
        at=coordinate.model_dump(mode="json"),
        evaluation_time=evaluation_time,
    )
    if AcceptedCoordinate.model_validate(view.coordinate.model_dump(mode="json")) != coordinate:
        raise ProjectionRepinError("Claim backing returned a different accepted coordinate")
    statement = next(
        (
            item.get("value")
            for item in view.facts
            if item.get("schema_id") == "playbill.claim.statement"
        ),
        None,
    )
    lifecycle = next(
        (
            item.get("value")
            for item in view.facts
            if item.get("schema_id") == "playbill.claim.lifecycle"
        ),
        None,
    )
    if not isinstance(statement, dict) or not isinstance(lifecycle, dict):
        raise ProjectionRepinError(
            "Claim backing did not disclose a complete statement and lifecycle"
        )
    state = lifecycle.get("lifecycle")
    if not isinstance(state, Mapping) or state.get("state") != "live":
        raise ProjectionRepinError("a projection backing must identify a live Claim")
    if view.envelope.get("identity") != f"Claim:{bare}":
        raise ProjectionRepinError("Claim backing identity differs from the requested Claim")
    return ProjectionClaimBackingV1(
        identity=ArtifactIdentity(kind="Claim", name=bare),
        statement_digest=claim_statement_digest(ClaimStatement.model_validate(statement)).tagged,
    )


def _query_backing(
    client: CruxibleClient,
    instance_id: str,
    *,
    name: str,
    parameters: Mapping[str, object],
    coordinate: AcceptedCoordinate,
    evaluation_time: datetime,
) -> ProjectionQueryBackingV1:
    bare = name.removeprefix("QueryDefinition:")
    normalized = normalize_canonical(dict(parameters))
    assert isinstance(normalized, dict)
    evaluated = client.run_playbill_query(
        instance_id,
        bare,
        at=coordinate.model_dump(mode="json"),
        evaluation_time=format_datetime(evaluation_time),
        parameters=normalized,
    )
    if (
        AcceptedCoordinate.model_validate(evaluated.coordinate.model_dump(mode="json"))
        != coordinate
    ):
        raise ProjectionRepinError("query backing returned a different accepted coordinate")
    result = evaluated.result
    if result.get("verdict") != "completed":
        raise ProjectionRepinError("a refused query cannot back a declared projection block")
    truncation = result.get("truncation")
    if not isinstance(truncation, Mapping) or truncation.get("clipped_budgets"):
        raise ProjectionRepinError("a truncated query cannot back a declared projection block")
    supplied = result.get("parameters")
    if not isinstance(supplied, list):
        raise ProjectionRepinError("query backing did not disclose resolved parameter bindings")
    bindings = tuple(ProjectionResolvedParameterBindingV1.model_validate(item) for item in supplied)
    return ProjectionQueryBackingV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name=bare),
        definition_digest=evaluated.definition_digest,
        resolved_parameter_bindings=bindings,
        canonical_param_digest=projection_parameter_digest(bindings),
        declared_evaluation_time=evaluation_time,
        semantic_result_digest=projection_query_semantic_result_digest(result),
    )


def _replace_if_unchanged(path: Path, *, expected: bytes, replacement: bytes) -> None:
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(replacement)
            output.flush()
            os.fsync(output.fileno())
        if path.read_bytes() != expected:
            raise ProjectionRepinError(
                "source bytes changed before the whole-file compare-and-swap"
            )
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ProjectionRepinError(f"source could not be replaced atomically: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def repin_projection_block(
    client: CruxibleClient,
    instance_id: str,
    *,
    workspace: str | Path,
    source_id: str,
    block_id: str,
    claims: Sequence[str] = (),
    queries: Sequence[tuple[str, Mapping[str, object]]] = (),
    evaluation_time: datetime,
    coordinate: AcceptedCoordinate | None = None,
) -> ProjectionBlockStampV1:
    """Stamp one agent-authored block by replacing its opening line and nothing else."""

    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ProjectionRepinError("projection repin requires an absolute evaluation time")
    instant = ensure_utc(evaluation_time)
    formatted = cast(str, format_datetime(instant))
    sources = WorkspaceSources(Path(workspace))
    path = sources.path_for_source(source_id)
    content = path.read_bytes()
    blocks = parse_projection_blocks(content, source_id=source_id, allow_bootstrap=True)
    block = next((item for item in blocks if item.block_id == block_id), None)
    if block is None:
        raise ProjectionRepinError(f"source {source_id!r} has no block {block_id!r}")

    claim_refs = tuple(claims)
    query_refs = tuple(queries)
    if not claim_refs and not query_refs:
        if block.stamp is None:
            raise ProjectionRepinError("the first block declaration requires explicit backing refs")
        claim_refs = tuple(
            item.identity.name
            for item in block.stamp.backing
            if isinstance(item, ProjectionClaimBackingV1)
        )
        query_refs = tuple(
            (
                item.identity.name,
                {binding.name: binding.value for binding in item.resolved_parameter_bindings},
            )
            for item in block.stamp.backing
            if isinstance(item, ProjectionQueryBackingV1)
        )

    orientation = client.search_playbill(
        instance_id,
        mode="orient",
        at=None if coordinate is None else coordinate.model_dump(mode="json"),
        evaluation_time=formatted,
    )
    active = AcceptedCoordinate.model_validate(orientation.coordinate.model_dump(mode="json"))
    if coordinate is not None and active != coordinate:
        raise ProjectionRepinError("orientation returned a different accepted coordinate")
    if orientation.orientation is None:
        raise ProjectionRepinError("orientation did not disclose the accepted generation")
    generation = orientation.orientation.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ProjectionRepinError("orientation did not disclose the accepted generation")

    backing: list[ProjectionBackingV1] = [
        _claim_backing(
            client,
            instance_id,
            name=name,
            coordinate=active,
            evaluation_time=formatted,
        )
        for name in claim_refs
    ]
    backing.extend(
        _query_backing(
            client,
            instance_id,
            name=name,
            parameters=parameters,
            coordinate=active,
            evaluation_time=instant,
        )
        for name, parameters in query_refs
    )
    stamp = ProjectionBlockStampV1(
        source_id=source_id,
        block_id=block_id,
        declared_generation=generation,
        declared_coordinate=active,
        backing=tuple(sorted(backing, key=lambda item: item.identity.qualified.encode("utf-8"))),
        body_digest=block.body_digest,
    )
    replacement = (
        content[: block.opening_start]
        + render_projection_opening(stamp)
        + content[block.opening_end :]
    )
    _replace_if_unchanged(path, expected=content, replacement=replacement)
    return stamp


__all__ = [
    "ParsedProjectionBlock",
    "ProjectionIndependentEvidenceForbidden",
    "ProjectionMarkerError",
    "ProjectionRepinError",
    "assert_independent_projection_evidence",
    "parse_projection_blocks",
    "render_projection_opening",
    "repin_projection_block",
]
