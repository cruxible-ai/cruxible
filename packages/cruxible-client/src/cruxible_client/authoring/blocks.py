"""Client-owned declarations; the machine frames but never authors body prose."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.claims import ClaimStatement, claim_statement_digest
from cruxible_client.contracts.declared_blocks import (
    ParsedProjectionBlock,
    ProjectionBackingV1,
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerError,
    ProjectionQueryBackingV1,
    ProjectionResolvedParameterBindingV1,
    assert_projection_block_frame,
    frame_projection_block,
    parse_projection_blocks,
    projection_parameter_digest,
    projection_query_semantic_result_digest,
    render_projection_opening,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_client.transport.http import CruxibleClient


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


def assert_independent_projection_evidence(
    *,
    source_id: str,
    content: bytes,
    start_byte: int,
    end_byte: int,
) -> None:
    for block in parse_projection_blocks(content, source_id=source_id, allow_bootstrap=True):
        if block.stamp is None:
            continue
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
    try:
        assert_projection_block_frame(
            replacement,
            source_id=source_id,
            block_id=block_id,
            stamp=stamp,
            body_digest=stamp.body_digest,
        )
    except ProjectionMarkerError as exc:
        raise ProjectionRepinError("replacement does not reproduce the declared block") from exc
    _replace_if_unchanged(path, expected=content, replacement=replacement)
    return stamp


__all__ = [
    "ParsedProjectionBlock",
    "ProjectionIndependentEvidenceForbidden",
    "ProjectionMarkerError",
    "ProjectionRepinError",
    "assert_independent_projection_evidence",
    "frame_projection_block",
    "parse_projection_blocks",
    "render_projection_opening",
    "repin_projection_block",
]
