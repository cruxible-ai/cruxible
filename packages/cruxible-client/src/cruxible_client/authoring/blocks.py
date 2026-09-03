"""Client-owned declarations; the machine frames but never authors body prose."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

from cruxible_client.authoring.context import PlaybillWorkspaceBinding
from cruxible_client.authoring.insertions import (
    PlaybillInsertionApplyError,
    replace_publication_file,
)
from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncItemV1,
    PlaybillBlockSyncReadRequestV1,
    PlaybillBlockSyncResultV1,
)
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.claims import ClaimStatement, claim_statement_digest
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_SCAN_BYTES,
    MAX_PROJECTION_SOURCE_BYTES,
    ParsedProjectionBlock,
    ProjectionBackingV1,
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerError,
    ProjectionQueryBackingV1,
    ProjectionResolvedParameterBindingV1,
    assert_projection_block_frame,
    discover_projection_blocks,
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


class ProjectionSyncError(PlaybillError):
    code = "playbill.projection.sync_refused"

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


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ProjectionSyncError(f"source path escapes workspace: {path}") from exc


def _repair_commands(*values: str) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: item.encode("utf-8")))


def _result(items: Sequence[PlaybillBlockSyncItemV1]) -> PlaybillBlockSyncResultV1:
    ordered = tuple(
        sorted(
            items,
            key=lambda item: (
                item.path.encode("utf-8"),
                (item.source_id or "").encode("utf-8"),
                (item.block_id or "").encode("utf-8"),
                item.outcome.encode("ascii"),
            ),
        )
    )
    return PlaybillBlockSyncResultV1(
        items=ordered,
        changed_file_count=len(
            {item.path for item in ordered if item.outcome in {"synced", "detached"}}
        ),
        would_change=any(
            item.outcome in {"synced", "would_sync", "detached", "would_detach"} for item in ordered
        ),
        has_refusals=any(item.outcome in {"refused", "unsyncable"} for item in ordered),
    )


def _workspace_binding(root: Path) -> PlaybillWorkspaceBinding | None:
    path = root / ".playbill" / "coverage.json"
    if not path.is_file():
        return None
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ProjectionSyncError("workspace coverage binding escapes the workspace")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return PlaybillWorkspaceBinding.model_validate(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectionSyncError(f"workspace coverage binding is invalid: {exc}") from exc


def _marker_line(content: bytes) -> int:
    for line_number, line in enumerate(content.splitlines(), start=1):
        if b"playbill:block:" in line:
            return line_number
    return 1


def _marker_error_item(
    *,
    root: Path,
    path: Path,
    content: bytes,
    error: Exception,
) -> PlaybillBlockSyncItemV1:
    relative = _relative_path(root, path)
    return PlaybillBlockSyncItemV1(
        path=relative,
        outcome="refused",
        reason="block_marker_malformed",
        detail={
            "message": str(error),
            "target": f"{relative}:{_marker_line(content)}",
            "required_change": "restore_projection_marker_grammar",
            "documentation": "docs/cli-reference.md#projection-block-markers",
        },
    )


def _discover_source(
    *, root: Path, path: Path
) -> tuple[str | None, PlaybillBlockSyncItemV1 | None]:
    content = b""
    try:
        if path.is_symlink():
            raise ProjectionSyncError("source path must not be a symbolic link")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ProjectionSyncError("source path escapes the workspace or is not a file")
    except (OSError, ProjectionSyncError) as exc:
        return None, PlaybillBlockSyncItemV1(
            path=path.name or ".",
            outcome="refused",
            reason="source_path_invalid",
            repair_commands=("cruxible playbill block sync --all",),
            detail={"message": str(exc)},
        )
    try:
        content = resolved.read_bytes()
        blocks = discover_projection_blocks(content)
        source_ids = {block.source_id for block in blocks}
        if len(source_ids) != 1:
            raise ProjectionMarkerError("projection markers disagree on logical source")
        return next(iter(source_ids)), None
    except (OSError, ProjectionMarkerError) as exc:
        return None, _marker_error_item(
            root=root,
            path=resolved,
            content=content,
            error=exc,
        )


def _discover_workspace_sources(
    root: Path,
) -> tuple[dict[Path, str], list[PlaybillBlockSyncItemV1]]:
    selected: dict[Path, str] = {}
    items: list[PlaybillBlockSyncItemV1] = []
    scanned_bytes = 0
    candidates = sorted(
        (
            path
            for path in root.rglob("*")
            if not ({".git", ".playbill"} & set(path.relative_to(root).parts))
        ),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            budgeted_size = min(size, MAX_PROJECTION_SOURCE_BYTES + 1)
            if scanned_bytes + budgeted_size > MAX_PROJECTION_SCAN_BYTES:
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=".",
                        outcome="refused",
                        reason="source_path_invalid",
                        repair_commands=("cruxible playbill block sync PATH",),
                        detail={
                            "message": "workspace marker scan exceeded its 32 MiB byte ceiling"
                        },
                    )
                )
                break
            with path.open("rb") as handle:
                content = handle.read(MAX_PROJECTION_SOURCE_BYTES + 1)
            scanned_bytes += len(content)
        except OSError:
            continue
        if b"playbill:block:" not in content:
            continue
        source_id, error = _discover_source(root=root, path=path)
        if error is not None:
            items.append(error)
        else:
            assert source_id is not None
            selected[path.resolve()] = source_id
    return selected, items


def _outside_bytes(content: bytes, spans: Sequence[tuple[int, int]]) -> bytes:
    chunks: list[bytes] = []
    cursor = 0
    for start, end in sorted(spans):
        chunks.append(content[cursor:start])
        cursor = end
    chunks.append(content[cursor:])
    return b"".join(chunks)


def _sync_item_from_read_refusal(
    *,
    path: str,
    source_id: str,
    block_id: str,
    read: object,
) -> PlaybillBlockSyncItemV1:
    reason = getattr(read, "reason", None)
    detail = getattr(read, "detail", None)
    values: dict[str, object] = {"message": detail or "block sync read refused"}
    repairs: tuple[str, ...]
    if reason == "block_backing_retired":
        repairs = _repair_commands(f"cruxible playbill block sync --detach {shlex.quote(path)}")
    elif reason == "block_successor_ambiguous":
        candidates = getattr(read, "successor_candidates", ())
        values["successor_candidates"] = [
            candidate.model_dump(mode="json") for candidate in candidates
        ]
        repairs = _repair_commands(
            *(
                "cruxible playbill block repin "
                f"{shlex.quote(source_id)} {shlex.quote(block_id)} "
                f"--backing {candidate.artifact_digest}"
                for candidate in candidates
            )
        )
    else:
        repairs = _repair_commands("cruxible playbill authoring create --example claim-self-source")
    mapped = {
        "block_workspace_instance_mismatch": "workspace_instance_mismatch",
        "block_backing_missing": "block_backing_missing",
        "block_backing_changed": "block_backing_changed",
        "block_not_publication_origin": "block_not_publication_origin",
        "block_publication_registry_unavailable": "block_publication_registry_unavailable",
        "block_backing_retired": "block_backing_retired",
        "block_successor_ambiguous": "block_successor_ambiguous",
        "block_successor_body_missing": "block_successor_body_missing",
        "block_successor_body_ambiguous": "block_successor_body_ambiguous",
    }
    reason_key = reason if isinstance(reason, str) else ""
    local_reason = mapped.get(reason_key, "block_backing_changed")
    return PlaybillBlockSyncItemV1.model_validate(
        {
            "path": path,
            "source_id": source_id,
            "block_id": block_id,
            "outcome": "refused" if getattr(read, "status", None) == "refused" else "unsyncable",
            "reason": local_reason,
            "repair_commands": repairs,
            "detail": values,
        }
    )


def sync_projection_blocks(
    client: CruxibleClient,
    instance_id: str,
    *,
    workspace: str | Path,
    paths: Sequence[str | Path] = (),
    all_sources: bool = False,
    check: bool = False,
    detach_paths: Sequence[str | Path] = (),
    discard_local_paths: Sequence[str | Path] = (),
) -> PlaybillBlockSyncResultV1:
    """Synchronize safe publication blocks with one atomic replacement per file."""

    root = Path(workspace).expanduser().resolve()
    try:
        binding = _workspace_binding(root)
    except ProjectionSyncError as exc:
        return _result(
            (
                PlaybillBlockSyncItemV1(
                    path=".playbill/coverage.json",
                    outcome="refused",
                    reason="workspace_binding_invalid",
                    repair_commands=("cruxible playbill host create --workspace . --replace",),
                    detail={"message": str(exc)},
                ),
            )
        )
    if binding is None or not binding.attached:
        return _result(
            (
                PlaybillBlockSyncItemV1(
                    path=".",
                    outcome="refused",
                    reason="workspace_not_attached",
                    repair_commands=("cruxible playbill host create --workspace .",),
                    detail={"binding": ".playbill/coverage.json"},
                ),
            )
        )
    if binding.instance_id != instance_id:
        return _result(
            (
                PlaybillBlockSyncItemV1(
                    path=".",
                    outcome="refused",
                    reason="workspace_instance_mismatch",
                    repair_commands=("cruxible playbill host create --workspace . --replace",),
                    detail={
                        "workspace_instance_id": binding.instance_id,
                        "selected_instance_id": instance_id,
                    },
                ),
            )
        )
    if all_sources and (paths or detach_paths):
        raise ProjectionSyncError("--all cannot be combined with explicit or detached paths")
    if paths and detach_paths:
        raise ProjectionSyncError("ordinary sync paths cannot be combined with --detach")
    selected: dict[Path, str] = {}
    items: list[PlaybillBlockSyncItemV1] = []
    requested = tuple(detach_paths or paths)
    catalog_paths = tuple(
        path
        for path in (root / ".playbill" / "sources.yaml", root / "sources.yaml")
        if path.is_file()
    )
    sources: WorkspaceSources | None = None
    if catalog_paths:
        try:
            sources = WorkspaceSources(root)
        except (ValueError, PlaybillError) as exc:
            return _result(
                (
                    PlaybillBlockSyncItemV1(
                        path=".playbill/sources.yaml",
                        outcome="refused",
                        reason="workspace_source_catalog_invalid",
                        repair_commands=("cruxible playbill block sync PATH",),
                        detail={"message": str(exc)},
                    ),
                )
            )
    if all_sources and sources is None:
        selected, items = _discover_workspace_sources(root)
    elif all_sources:
        assert sources is not None
        for entry in sources.document_entries:
            try:
                selected[sources.path_for_source(entry.name)] = entry.name
            except (ValueError, PlaybillError) as exc:
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=entry.locator,
                        source_id=entry.name,
                        outcome="refused",
                        reason="source_path_invalid",
                        repair_commands=("cruxible playbill block sync --all",),
                        detail={"message": str(exc)},
                    )
                )
    else:
        if not requested:
            raise ProjectionSyncError("block sync requires --all or at least one path")
        for requested_path in requested:
            if sources is None:
                unresolved = Path(requested_path)
                path = (unresolved if unresolved.is_absolute() else root / unresolved).expanduser()
                source_id, error = _discover_source(root=root, path=path)
                if error is not None:
                    items.append(error)
                    continue
                assert source_id is not None
                selected[path.resolve()] = source_id
            else:
                try:
                    source = sources.select(requested_path)
                except ValueError as exc:
                    display = str(requested_path)
                    items.append(
                        PlaybillBlockSyncItemV1(
                            path=display,
                            outcome="refused",
                            reason="source_path_invalid",
                            repair_commands=("cruxible playbill block sync --all",),
                            detail={"message": str(exc)},
                        )
                    )
                    continue
                selected[source.path] = source.source_id
    discard = {
        (Path(path) if Path(path).is_absolute() else root / path).expanduser().resolve()
        for path in discard_local_paths
    }
    detach = bool(detach_paths)
    for path, source_id in sorted(selected.items(), key=lambda item: item[1].encode("utf-8")):
        relative = _relative_path(root, path)
        content = b""
        try:
            content = path.read_bytes()
            blocks = parse_projection_blocks(content, source_id=source_id, allow_bootstrap=True)
        except (OSError, ProjectionMarkerError) as exc:
            items.append(_marker_error_item(root=root, path=path, content=content, error=exc))
            continue
        replacements: dict[str, bytes] = {}
        changed_item_indexes: list[int] = []
        original_spans: list[tuple[int, int]] = []
        for block in blocks:
            stamp = block.stamp
            if stamp is None:
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="skipped",
                        reason="block_unstamped",
                        repair_commands=(),
                        detail={
                            "message": (
                                "unstamped draft blocks are not synchronized; the first "
                                "stamp requires explicit --claim or --query backing refs"
                            )
                        },
                    )
                )
                continue
            if len(stamp.backing) != 1:
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="unsyncable",
                        reason="block_multi_backing",
                        repair_commands=(
                            "cruxible playbill authoring create --example claim-self-source",
                        ),
                        detail={"backing_count": len(stamp.backing)},
                    )
                )
                continue
            if not isinstance(stamp.backing[0], ProjectionClaimBackingV1):
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="unsyncable",
                        reason="block_query_backing",
                        repair_commands=(
                            "cruxible playbill authoring create --example claim-self-source",
                        ),
                        detail={"backing": stamp.backing[0].identity.qualified},
                    )
                )
                continue
            if not detach and block.body_digest != stamp.body_digest and path not in discard:
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="refused",
                        reason="block_locally_modified",
                        repair_commands=_repair_commands(
                            "cruxible playbill authoring create --example claim-self-source",
                            "cruxible playbill block sync "
                            f"{shlex.quote(relative)} --discard-local {shlex.quote(relative)}",
                        ),
                        detail={
                            "last_synced_body_digest": stamp.body_digest,
                            "observed_body_digest": block.body_digest,
                        },
                    )
                )
                continue
            read = client.read_playbill_block_sync_backing(
                instance_id,
                request=PlaybillBlockSyncReadRequestV1(stamp=stamp),
            )
            if read.status not in {"current", "successor"}:
                if detach and read.reason == "block_backing_retired":
                    replacement = content[block.body_start : block.body_end]
                    replacements[block.block_id] = replacement
                    original_spans.append((block.opening_start, block.closing_end))
                    changed_item_indexes.append(len(items))
                    items.append(
                        PlaybillBlockSyncItemV1(
                            path=relative,
                            source_id=source_id,
                            block_id=block.block_id,
                            outcome="would_detach" if check else "detached",
                            detail={"body_digest": block.body_digest},
                        )
                    )
                else:
                    items.append(
                        _sync_item_from_read_refusal(
                            path=relative,
                            source_id=source_id,
                            block_id=block.block_id,
                            read=read,
                        )
                    )
                continue
            if detach:
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="unchanged",
                        detail={"message": "live blocks are not detached"},
                    )
                )
                continue
            assert read.coordinate is not None
            assert read.generation is not None
            assert read.backing is not None
            assert read.body is not None
            assert read.body_digest is not None
            next_stamp = stamp.model_copy(
                update={
                    "declared_generation": read.generation,
                    "declared_coordinate": read.coordinate,
                    "backing": (read.backing,),
                    "body_digest": read.body_digest,
                }
            )
            try:
                framed = frame_projection_block(stamp=next_stamp, body=read.body)
            except ProjectionMarkerError as exc:
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="refused",
                        reason="block_frame_invalid",
                        repair_commands=(
                            "cruxible playbill authoring create --example claim-self-source",
                        ),
                        detail={"message": str(exc)},
                    )
                )
                continue
            if content[block.opening_start : block.closing_end] == framed:
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="unchanged",
                        detail={"artifact_digest": read.artifact_digest},
                    )
                )
                continue
            replacements[block.block_id] = framed
            original_spans.append((block.opening_start, block.closing_end))
            changed_item_indexes.append(len(items))
            items.append(
                PlaybillBlockSyncItemV1(
                    path=relative,
                    source_id=source_id,
                    block_id=block.block_id,
                    outcome="would_sync" if check else "synced",
                    detail={
                        "artifact_digest": read.artifact_digest,
                        "body_digest": read.body_digest,
                    },
                )
            )
        if not replacements:
            continue
        replacement = content
        for block in reversed(blocks):
            block_replacement = replacements.get(block.block_id)
            if block_replacement is not None:
                replacement = (
                    replacement[: block.opening_start]
                    + block_replacement
                    + replacement[block.closing_end :]
                )
        final_spans: list[tuple[int, int]] = []
        offset_delta = 0
        for block in blocks:
            block_replacement = replacements.get(block.block_id)
            if block_replacement is None:
                continue
            final_start = block.opening_start + offset_delta
            final_spans.append((final_start, final_start + len(block_replacement)))
            offset_delta += len(block_replacement) - (block.closing_end - block.opening_start)
        try:
            before_outside = _digest(_outside_bytes(content, original_spans))
            after_outside = _digest(_outside_bytes(replacement, final_spans))
            if before_outside != after_outside:
                raise ProjectionSyncError("bytes outside synchronized markers changed")
            final_blocks = parse_projection_blocks(
                replacement, source_id=source_id, allow_bootstrap=True
            )
            final_by_id = {block.block_id: block for block in final_blocks}
            if detach:
                if set(replacements) & set(final_by_id):
                    raise ProjectionSyncError("detached projection markers remain in the source")
            else:
                for block_id in replacements:
                    final = final_by_id[block_id]
                    assert final.stamp is not None
                    assert_projection_block_frame(
                        replacement,
                        source_id=source_id,
                        block_id=block_id,
                        stamp=final.stamp,
                        body_digest=final.body_digest,
                        allow_bootstrap=True,
                    )
            for index in changed_item_indexes:
                items[index] = items[index].model_copy(
                    update={
                        "detail": {
                            **items[index].detail,
                            "outside_digest_before": before_outside,
                            "outside_digest_after": after_outside,
                        }
                    }
                )
        except (KeyError, ProjectionMarkerError, ProjectionSyncError) as exc:
            for index in changed_item_indexes:
                items[index] = PlaybillBlockSyncItemV1(
                    path=relative,
                    source_id=source_id,
                    block_id=items[index].block_id,
                    outcome="refused",
                    reason="block_frame_invalid",
                    repair_commands=("cruxible playbill block sync --all",),
                    detail={"message": str(exc)},
                )
            continue
        if check:
            continue
        try:
            replace_publication_file(path, expected=content, replacement=replacement)
        except PlaybillInsertionApplyError as exc:
            for index in changed_item_indexes:
                items[index] = PlaybillBlockSyncItemV1(
                    path=relative,
                    source_id=source_id,
                    block_id=items[index].block_id,
                    outcome="refused",
                    reason="block_concurrent_edit",
                    repair_commands=("cruxible playbill block sync --all",),
                    detail={"message": str(exc)},
                )
    return _result(items)


def repin_projection_block(
    client: CruxibleClient,
    instance_id: str,
    *,
    workspace: str | Path,
    source_id: str,
    block_id: str,
    claims: Sequence[str] = (),
    queries: Sequence[tuple[str, Mapping[str, object]]] = (),
    backing_digest: str | None = None,
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
    if backing_digest is not None and (claim_refs or query_refs):
        raise ProjectionRepinError("--backing cannot be combined with Claim or Query refs")
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

    if backing_digest is not None:
        if block.stamp is None:
            raise ProjectionRepinError("--backing requires an existing stamped block")
        selected = client.read_playbill_block_sync_backing(
            instance_id,
            request=PlaybillBlockSyncReadRequestV1(
                stamp=block.stamp,
                preferred_successor_digest=backing_digest,
            ),
        )
        if selected.status not in {"current", "successor"} or selected.backing is None:
            raise ProjectionRepinError(
                selected.detail or "the requested backing digest is not a live successor"
            )
        assert selected.coordinate is not None
        assert selected.generation is not None
        active = selected.coordinate
        generation = selected.generation
        backing: list[ProjectionBackingV1] = [selected.backing]
    else:
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
        generation_value = orientation.orientation.get("generation")
        if (
            not isinstance(generation_value, int)
            or isinstance(generation_value, bool)
            or generation_value < 0
        ):
            raise ProjectionRepinError("orientation did not disclose the accepted generation")
        generation = generation_value

        backing = [
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
            allow_bootstrap=True,
        )
    except ProjectionMarkerError as exc:
        raise ProjectionRepinError("replacement does not reproduce the declared block") from exc
    try:
        replace_publication_file(path, expected=content, replacement=replacement)
    except PlaybillInsertionApplyError as exc:
        raise ProjectionRepinError(str(exc)) from exc
    return stamp


__all__ = [
    "ParsedProjectionBlock",
    "ProjectionIndependentEvidenceForbidden",
    "ProjectionMarkerError",
    "ProjectionRepinError",
    "ProjectionSyncError",
    "assert_independent_projection_evidence",
    "frame_projection_block",
    "parse_projection_blocks",
    "render_projection_opening",
    "repin_projection_block",
    "sync_projection_blocks",
]
