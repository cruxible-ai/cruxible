"""Client-owned declarations; the machine frames but never authors body prose."""

from __future__ import annotations

import hashlib
import json
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
    declares_projection_block,
    discover_projection_blocks,
    frame_projection_block,
    parse_projection_blocks,
    projection_parameter_digest,
    projection_query_semantic_result_digest,
    projection_window_intersecting,
    render_projection_opening,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import RepairOperationV1, ServedRepairV1
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_client.transport.http import CruxibleClient


class ProjectionIndependentEvidenceForbidden(PlaybillError):
    """A citation's span lies inside a projection block: the client fast path.

    Evidence never comes from a projection window, whatever the citation's role
    or origin. The daemon refuses the same span with the same code at lowering
    and at the citation gate; this raises before the wire so an author learns
    it without a round trip.
    """

    code = "playbill.projection.evidence_from_projection"

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
            f"selected bytes [{start_byte}, {end_byte}); a projection block is never "
            "evidence, so cite the Claims it reflects, or cite prose outside the block"
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
    """Refuse a span that touches any stamped block window in ``content``.

    Reads the windows with the evidence-side scanner rather than the page
    parser: a cited source is evidence, not a projection page, so it is neither
    held to the page ceilings nor refused for a marker defect of its own. A
    capture with no marker bytes costs one substring search.
    """

    window = projection_window_intersecting(content, start_byte=start_byte, end_byte=end_byte)
    if window is not None:
        raise ProjectionIndependentEvidenceForbidden(
            source_id=source_id,
            block_id=window.block_id,
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
        # A stale held list and a hand-edited body are findings this verb
        # reports and cannot repair, so they count here: the sweep an
        # activation runs must not answer clean over a page that has drifted
        # from the state it declares.
        has_refusals=any(
            item.outcome in {"refused", "unsyncable", "stale", "dirty"} for item in ordered
        ),
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


def _not_a_projection_target_item(
    *,
    root: Path,
    path: Path,
    content: bytes,
    error: Exception,
    source_id: str | None = None,
) -> PlaybillBlockSyncItemV1:
    """Note a discovered file that does not declare a projection block.

    Nothing in a workspace announces which files are projection pages, so a
    workspace-wide sync infers them from the marker bytes -- and captured prose
    ABOUT the marker grammar carries those bytes verbatim. Parsing such a file
    produced a refusal whose only repair was to hand-edit it, which is exactly
    the repair a capture of accepted bytes cannot take, and the refusal then
    turned every lawful activation that runs the same walk into a non-zero exit.
    A file with no declaration at all is not a projection target, so the
    inferred walk notes it and moves on.

    ONLY that file. A page that declares a block and declares it badly -- a
    stamped page whose closing marker was deleted, one repeating a block
    identity -- is a projection target with a defect, and skipping it would hide
    a real defect behind a clean exit code until somebody happened to name that
    path. `declares_projection_block` draws the line, and this item is reachable
    only on its false side.
    """

    relative = _relative_path(root, path)
    return PlaybillBlockSyncItemV1(
        path=relative,
        source_id=source_id,
        outcome="skipped",
        reason="source_not_projection_target",
        detail={
            "message": str(error),
            "target": f"{relative}:{_marker_line(content)}",
            "required_change": "name_the_path_explicitly_if_it_declares_a_projection_block",
        },
    )


def _discover_source(
    *,
    root: Path,
    path: Path,
    inferred: bool = False,
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
            detail={"message": str(exc)},
        )
    try:
        content = resolved.read_bytes()
    except OSError as exc:
        return None, _marker_error_item(
            root=root,
            path=resolved,
            content=content,
            error=exc,
        )
    try:
        blocks = discover_projection_blocks(content)
        source_ids = {block.source_id for block in blocks}
        if len(source_ids) != 1:
            raise ProjectionMarkerError("projection markers disagree on logical source")
        return next(iter(source_ids)), None
    except ProjectionMarkerError as exc:
        if inferred and not declares_projection_block(content):
            return None, _not_a_projection_target_item(
                root=root,
                path=resolved,
                content=content,
                error=exc,
            )
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
        source_id, error = _discover_source(root=root, path=path, inferred=True)
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
    repair: ServedRepairV1 | None
    if reason == "block_backing_retired":
        repair = RepairOperationV1(
            operation="playbill.block.sync",
            arguments={"paths": [path], "detach": True},
        )
    elif reason == "block_successor_ambiguous":
        candidates = getattr(read, "successor_candidates", ())
        values["successor_candidates"] = [
            candidate.model_dump(mode="json") for candidate in candidates
        ]
        repair = RepairOperationV1(
            operation="playbill.block.repin",
            arguments={
                "source_id": source_id,
                "block_id": block_id,
                "backing_candidates": [candidate.artifact_digest for candidate in candidates],
            },
        )
    else:
        repair = None
    mapped = {
        "block_workspace_instance_mismatch": "workspace_instance_mismatch",
        "block_backing_missing": "block_backing_missing",
        "block_backing_changed": "block_backing_changed",
        "block_backing_retired": "block_backing_retired",
        "block_successor_ambiguous": "block_successor_ambiguous",
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
            "repair": None if repair is None else repair.model_dump(mode="python"),
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
    """Report whether each declared block still reads as its stamp says.

    This verb used to converge a single-Claim block to the accepted body it was
    published from. Nothing renders now, so it writes nothing: every block is
    reported `unchanged`, `stale` (a held backing moved) or `dirty` (the prose
    moved away from the stamp), and refusals are unchanged. `--check` is
    therefore the behaviour by default and the flag changes only `--detach`,
    which is the one edit left -- stripping the marker pair of a block whose
    backing is retired or whose host this worktree has left.
    """

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
        except OSError as exc:
            items.append(_marker_error_item(root=root, path=path, content=content, error=exc))
            continue
        try:
            blocks = parse_projection_blocks(content, source_id=source_id, allow_bootstrap=True)
        except ProjectionMarkerError as exc:
            # `--all` walks the whole catalog, and a catalogued source is a
            # source, not necessarily a projection page: a captured report ABOUT
            # the marker grammar carries the marker bytes verbatim and is exact
            # accepted bytes, so "hand edit it" is the one repair it cannot
            # take. An inferred selection notes such a source and moves on; a
            # path the caller named still refuses, because there the caller
            # asserted that this file declares a block.
            #
            # The selection mode is only half the question. A catalogued page
            # that DOES declare a block and declares it badly is a projection
            # target with a defect, and it refuses here too -- otherwise a real
            # marker defect is hidden from `--all`, and from the sync every
            # activation runs as its last step, behind a zero exit code.
            if all_sources and not declares_projection_block(content):
                items.append(
                    _not_a_projection_target_item(
                        root=root,
                        path=path,
                        content=content,
                        error=exc,
                        source_id=source_id,
                    )
                )
                continue
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
                        detail={
                            "message": (
                                "unstamped draft blocks are not synchronized; the first "
                                "stamp requires explicit --claim or --query backing refs"
                            )
                        },
                    )
                )
                continue
            if not detach and block.body_digest != stamp.body_digest:
                # The prose moved away from what the stamp committed. Nothing
                # renders a block, so there is no accepted body to put back:
                # this is a finding about the page, and the repair is to read
                # the block against its backings and re-stamp what it now says.
                # `--discard-local` no longer discards anything -- it says the
                # local body is intended, and suppresses the row for that path.
                if path in discard:
                    items.append(
                        PlaybillBlockSyncItemV1(
                            path=relative,
                            source_id=source_id,
                            block_id=block.block_id,
                            outcome="unchanged",
                            detail={
                                "local_body_accepted": True,
                                "stamped_body_digest": stamp.body_digest,
                                "observed_body_digest": block.body_digest,
                            },
                        )
                    )
                    continue
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="dirty",
                        reason="block_locally_modified",
                        repair=RepairOperationV1(
                            operation="playbill.block.repin",
                            arguments={"source_id": source_id, "block_id": block.block_id},
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
                # A foreign block is the case --detach exists for. Stripping a
                # marker pair and keeping the body between them is a purely
                # local text edit: it claims no authority over the block, reads
                # nothing from the instance that published it, and asserts
                # nothing about it afterwards. Gating it behind the same
                # instance check as a sync left an operator moving a worktree
                # between hosts with one named repair -- re-attach this worktree
                # to the host that published the markers -- which is the exact
                # opposite of what they were doing, and one hand-written
                # stripper as the only way through.
                detachable = detach and read.reason in {
                    "block_backing_retired",
                    "block_workspace_instance_mismatch",
                }
                if detachable:
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
                            detail={
                                "body_digest": block.body_digest,
                                # The stamp records the coordinate it was
                                # published at and no instance id, so the
                                # foreign host is named the only way the file
                                # can name it.
                                **(
                                    {"foreign_declared_git_oid": stamp.declared_coordinate.git_oid}
                                    if read.reason == "block_workspace_instance_mismatch"
                                    else {}
                                ),
                            },
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
            if read.status == "current":
                items.append(
                    PlaybillBlockSyncItemV1(
                        path=relative,
                        source_id=source_id,
                        block_id=block.block_id,
                        outcome="unchanged",
                        detail={
                            "backing_count": len(stamp.backing),
                            "coordinate_git_oid": (
                                None if read.coordinate is None else read.coordinate.git_oid
                            ),
                        },
                    )
                )
                continue
            # A held member moved under the block. No verb converges the prose
            # -- the author wrote it -- so the row names every member that moved
            # and the repin that re-declares the list the block still means.
            items.append(
                PlaybillBlockSyncItemV1(
                    path=relative,
                    source_id=source_id,
                    block_id=block.block_id,
                    outcome="stale",
                    reason="block_backing_changed",
                    repair=RepairOperationV1(
                        operation="playbill.block.repin",
                        arguments={"source_id": source_id, "block_id": block.block_id},
                    ),
                    detail={
                        "moved_backings": [
                            backing.identity.qualified for backing in read.moved_backings
                        ],
                        "backing_count": len(stamp.backing),
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
            # Detachment is the only edit this verb makes. Nothing renders a
            # block body, so nothing rewrites one, and the only proof left to
            # take is that the markers this call stripped are gone and every
            # byte outside them is untouched.
            final_by_id = {block.block_id: block for block in final_blocks}
            if set(replacements) & set(final_by_id):
                raise ProjectionSyncError("detached projection markers remain in the source")
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
    # The page now carries a marker this instance has never heard of. A block
    # was known to the instance only if the retired publication road minted it,
    # so "is this marker sanctioned?" was answered by a string prefix and
    # `workspace detach` could not refuse on a block an agent declared. The
    # declaration is recorded after the write, because a registration for a
    # marker that never landed would be the same lie in the other direction.
    client.declare_playbill_block(instance_id, stamp.model_dump(mode="json"))
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
