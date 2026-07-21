"""CLI ``ws`` group: manage the agent-local working set (opt-in prototype).

Only this group and the capture hooks (CLI read commands and the MCP read
tools) ever touch the working-set files; no write path or other CLI command
reads them. See :mod:`cruxible_core.working_set` for the cache contract —
including the tamper-honesty caveat (same-user processes CAN rewrite the
cache; hygiene reduces accidents, not adversaries). Path resolution honors
``CRUXIBLE_WORKING_SET_DIR`` (precedence: explicit env > the default
``~/.cruxible/working-set``), so these verbs manage an MCP-rooted cache too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.commands._common import (
    _emit_json,
    _get_client,
    _guard_local_read_fallback,
    _require_instance_id,
)
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import service_get_entity, service_inspect_entity
from cruxible_core.service.types import InspectNeighborhoodResult
from cruxible_core.temporal import format_datetime, utc_now
from cruxible_core.workflow.compiler import compute_lock_config_digest
from cruxible_core.working_set import (
    WorkingSetPathError,
    local_instance_key,
    normalize_edge_record,
    normalize_entity_record,
    read_records_detailed,
    record_identity,
    records_path,
    secure_records_path,
    server_instance_key,
    working_set_dir,
    write_records,
)

# Edge budget used when re-fetching an owning entity's neighborhood during
# refresh: the read-surface hard cap, so a refresh misses an edge only when
# the owner genuinely has more than this many edges of one relationship type.
_REFRESH_MAX_EDGES = 1000


@dataclass
class _WsContext:
    """Resolved transport + identity for one ``ws`` invocation."""

    instance_key: str
    client: CruxibleClient | None = None
    instance_id: str | None = None
    instance: CruxibleInstance | None = None


def _ws_context() -> _WsContext:
    client = _get_client()
    if client is not None:
        instance_id = _require_instance_id()
        return _WsContext(
            instance_key=server_instance_key(instance_id),
            client=client,
            instance_id=instance_id,
        )
    _guard_local_read_fallback()
    instance = CruxibleInstance.load()
    return _WsContext(
        instance_key=local_instance_key(instance.get_root_path()),
        instance=instance,
    )


def _ws_paths() -> tuple[_WsContext, Path]:
    """Resolve the verb context AND its fully validated records path.

    Every filesystem-touching ``ws`` verb goes through this ONE helper before
    its first read/stat/write/unlink: :func:`secure_records_path` refuses a
    symlink at any chain level (root, instance dir, records file — and the
    scope-salt file, checked inside the server-mode key derivation).
    Validation failures surface as usage errors.
    """
    try:
        context = _ws_context()
        return context, secure_records_path(context.instance_key)
    except (ValueError, WorkingSetPathError) as exc:
        raise click.UsageError(str(exc)) from exc


def _current_read_revision(context: _WsContext) -> int | None:
    """Fetch the CURRENT instance read revision (stats endpoint / local instance)."""
    if context.client is not None and context.instance_id is not None:
        return context.client.stats(context.instance_id).read_revision
    assert context.instance is not None
    return context.instance.get_read_revision()


def _current_config_digest(context: _WsContext) -> str | None:
    """Fetch the CURRENT active config digest — capture's stamping source.

    Local mode computes the same lock digest continuation tokens bind to;
    server mode reads the daemon's recorded active config digest. ``None``
    when unresolvable (records then verify as ``unknown`` on the config axis).
    """
    try:
        if context.client is not None and context.instance_id is not None:
            provenance = context.client.config_status(context.instance_id).provenance
            return provenance.active_config_digest if provenance is not None else None
        assert context.instance is not None
        return compute_lock_config_digest(context.instance.load_config())
    except Exception:
        return None


def _classify(
    records: list[dict[str, Any]],
    current_revision: int | None,
    current_config_digest: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split records into (fresh, stale, unknown) against the current state.

    ``fresh`` means the cached revision equals the current one AND the cached
    config digest concretely matches the current one (a config reload does
    not bump ``read_revision``, so old-schema records would otherwise verify
    fresh forever); any concrete mismatch on either axis is ``stale``. A
    record missing its revision or config digest — or an unresolvable
    CURRENT config digest, which makes the config axis unverifiable — is
    ``unknown``: honest freshness means a record is never reported fresh
    unless both axes were actually compared. Unknown records are re-fetched
    by refresh but never a verify failure. (Schema-invalid lines never reach
    this function: the reader already skipped and counted them.)
    """
    fresh: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for record in records:
        revision = record.get("read_revision")
        record_digest = record.get("config_digest")
        if not isinstance(revision, int):
            unknown.append(record)
        elif current_revision is None or revision != current_revision:
            stale.append(record)
        elif not isinstance(record_digest, str) or current_config_digest is None:
            unknown.append(record)
        elif record_digest == current_config_digest:
            fresh.append(record)
        else:
            stale.append(record)
    return fresh, stale, unknown


def _identity_label(record: dict[str, Any]) -> str:
    if record.get("kind") == "entity":
        return f"entity {record.get('entity_type')}/{record.get('entity_id')}"
    edge_key = record.get("edge_key")
    suffix = f"#{edge_key}" if edge_key is not None else ""
    return (
        f"edge {record.get('relationship_type')} "
        f"{record.get('from_type')}/{record.get('from_id')} -> "
        f"{record.get('to_type')}/{record.get('to_id')}{suffix}"
    )


@click.group("ws")
def ws_group() -> None:
    """Agent-local working set: opt-in, NON-AUTHORITATIVE read cache.

    Enable capture with CRUXIBLE_WORKING_SET=1 or per-command --ws on JSON
    reads; MCP read tools capture when CRUXIBLE_WORKING_SET_DIR is set
    (that variable also redirects the cache root these verbs use). Records
    are revision-stamped; verify before trusting them. Cache files are
    same-user-writable by design — hygiene reduces accidents, not
    adversaries.
    """


@ws_group.command("path")
@handle_errors
def ws_path_cmd() -> None:
    """Print the records file path for the current context (for rg/jq)."""
    context = _ws_context()
    click.echo(str(records_path(context.instance_key)))


@ws_group.command("status")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def ws_status_cmd(output_json: bool) -> None:
    """Show record counts, file size, and cached-vs-current revision spread."""
    context, path = _ws_paths()
    read_result = read_records_detailed(path)
    records = read_result.records
    current_revision = _current_read_revision(context)

    kind_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    revisions = [
        record["read_revision"]
        for record in records
        if isinstance(record.get("read_revision"), int)
    ]
    for record in records:
        kind = str(record.get("kind"))
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        type_name = str(
            record.get("entity_type")
            if record.get("kind") == "entity"
            else record.get("relationship_type")
        )
        type_counts[type_name] = type_counts.get(type_name, 0) + 1

    payload = {
        "instance_key": context.instance_key,
        "path": str(path),
        "exists": path.exists(),
        "file_size_bytes": path.stat().st_size if path.exists() else 0,
        "record_count": len(records),
        "invalid_lines": read_result.invalid_lines,
        "kind_counts": kind_counts,
        "type_counts": type_counts,
        "current_read_revision": current_revision,
        "newest_cached_revision": max(revisions) if revisions else None,
        "oldest_cached_revision": min(revisions) if revisions else None,
    }
    if output_json:
        _emit_json(payload)
        return
    click.echo(f"Instance key: {payload['instance_key']}")
    click.echo(f"Records file: {payload['path']}")
    if not payload["exists"]:
        click.echo("No working-set records captured yet.")
        return
    click.echo(f"File size: {payload['file_size_bytes']} bytes")
    click.echo(f"Records: {payload['record_count']}")
    if read_result.invalid_lines:
        click.echo(f"Invalid lines skipped: {read_result.invalid_lines}")
    for kind, count in sorted(kind_counts.items()):
        click.echo(f"  {kind}: {count}")
    if type_counts:
        click.echo("By type:")
        for type_name, count in sorted(type_counts.items()):
            click.echo(f"  {type_name}: {count}")
    click.echo(
        f"Read revision: current={payload['current_read_revision']} "
        f"newest_cached={payload['newest_cached_revision']} "
        f"oldest_cached={payload['oldest_cached_revision']}"
    )


@ws_group.command("verify")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def ws_verify_cmd(output_json: bool) -> None:
    """Verify cached records against the current instance read revision.

    Reports fresh (revision AND config digest match), stale (either differs),
    unknown (no revision/digest recorded — never fresh), and invalid
    (malformed lines skipped by the reader — never fresh). Exit contract:
    0 when every record is fresh or unknown AND no lines are invalid; 1 when
    anything is stale OR any invalid lines are present (a tampered/corrupt
    cache must be loud in scripts). Unknown alone never fails.
    """
    context, path = _ws_paths()
    read_result = read_records_detailed(path)
    records = read_result.records
    current_revision = _current_read_revision(context)
    current_config_digest = _current_config_digest(context)
    fresh, stale, unknown = _classify(records, current_revision, current_config_digest)

    if output_json:
        _emit_json(
            {
                "instance_key": context.instance_key,
                "current_read_revision": current_revision,
                "current_config_digest": current_config_digest,
                "total": len(records),
                "fresh": len(fresh),
                "stale": len(stale),
                "unknown": len(unknown),
                "invalid": read_result.invalid_lines,
                "stale_records": [_identity_label(record) for record in stale],
            }
        )
    else:
        click.echo(f"Instance key: {context.instance_key}")
        click.echo(f"Current read revision: {current_revision}")
        click.echo(
            f"Records: {len(records)} (fresh={len(fresh)} stale={len(stale)} "
            f"unknown={len(unknown)} invalid={read_result.invalid_lines})"
        )
        for record in stale:
            revision = record.get("read_revision")
            if (
                current_config_digest is not None
                and isinstance(record.get("config_digest"), str)
                and record.get("config_digest") != current_config_digest
                and revision == current_revision
            ):
                click.echo(f"  stale: {_identity_label(record)} (config changed)")
            else:
                click.echo(f"  stale: {_identity_label(record)} (revision {revision})")
    if stale or read_result.invalid_lines:
        raise SystemExit(1)


@dataclass
class _RefreshReport:
    refreshed: int = 0
    removed: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def _fetch_entity_record(
    context: _WsContext,
    record: dict[str, Any],
    as_of: str,
    config_digest: str | None,
) -> dict[str, Any] | None:
    """Re-fetch one entity record (compact profile). ``None`` => entity gone."""
    entity_type = str(record.get("entity_type"))
    entity_id = str(record.get("entity_id"))
    if context.client is not None and context.instance_id is not None:
        result = context.client.get_entity(context.instance_id, entity_type, entity_id)
        if not result.found:
            return None
        payload = {
            "entity_type": result.entity_type,
            "entity_id": result.entity_id,
            "properties": result.properties,
            "metadata": result.metadata,
        }
        revision = result.read_revision
    else:
        assert context.instance is not None
        entity = service_get_entity(context.instance, entity_type, entity_id)
        if entity is None:
            return None
        payload = {
            "entity_type": entity.entity_type,
            "entity_id": entity.entity_id,
            "properties": dict(entity.properties),
            "metadata": entity.metadata.to_metadata_dict(),
        }
        revision = context.instance.get_read_revision()
    return normalize_entity_record(
        payload,
        read_revision=revision,
        as_of=as_of,
        receipt_refs=[],
        source_cmd="ws refresh",
        config_digest=config_digest,
    )


def _budget_truncated(truncation_reasons: list[str]) -> bool:
    """Whether a scan stopped for BUDGET reasons (node/edge caps).

    Only budget truncation can hide an edge that still exists. Depth
    truncation merely marks the horizon beyond the depth-1 scope — every
    requested edge of the owner was still enumerated — so a depth-only (or
    un-truncated) read stays authoritative for edge presence.
    """
    return any(reason in ("node_budget", "edge_budget") for reason in truncation_reasons)


def _fetch_owner_neighborhood(
    context: _WsContext,
    from_type: str,
    from_id: str,
    relationship_type: str,
) -> tuple[bool, bool, list[dict[str, Any]], int | None]:
    """Fetch the owning entity's outgoing edges of one relationship type.

    Returns (owner_found, budget_truncated, edge_payloads, read_revision).
    ``budget_truncated`` is True only for node/edge-budget truncation — the
    one case where the scan may have MISSED a surviving edge; depth-horizon
    truncation cannot hide an outgoing edge of the depth-1 owner.
    """
    if context.client is not None and context.instance_id is not None:
        result = context.client.inspect_entity(
            context.instance_id,
            from_type,
            from_id,
            direction="outgoing",
            depth=1,
            relationship_types=[relationship_type],
            max_edges=_REFRESH_MAX_EDGES,
        )
        assert isinstance(result, contracts.InspectNeighborhoodResult)
        edges = [edge.model_dump(mode="python") for edge in result.edges]
        return (
            result.found,
            _budget_truncated(list(result.truncation_reasons)),
            edges,
            result.read_revision,
        )
    assert context.instance is not None
    local_result = service_inspect_entity(
        context.instance,
        from_type,
        from_id,
        direction="outgoing",
        depth=1,
        relationship_types=[relationship_type],
        max_edges=_REFRESH_MAX_EDGES,
    )
    assert isinstance(local_result, InspectNeighborhoodResult)
    edges = [
        {
            "relationship_type": edge.relationship_type,
            "from_type": edge.from_type,
            "from_id": edge.from_id,
            "to_type": edge.to_type,
            "to_id": edge.to_id,
            "edge_key": edge.edge_key,
            "properties": edge.properties,
            "metadata": edge.metadata,
        }
        for edge in local_result.edges
    ]
    return (
        local_result.found,
        _budget_truncated(list(local_result.truncation_reasons)),
        edges,
        context.instance.get_read_revision(),
    )


def _refresh_edge_records(
    context: _WsContext,
    stale_edges: list[dict[str, Any]],
    as_of: str,
    report: _RefreshReport,
    config_digest: str | None,
) -> list[dict[str, Any]]:
    """Re-fetch stale edge records via the owning entity's inspect."""
    refreshed: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in stale_edges:
        owner = (
            str(record.get("from_type")),
            str(record.get("from_id")),
            str(record.get("relationship_type")),
        )
        grouped.setdefault(owner, []).append(record)

    for (from_type, from_id, relationship_type), records in grouped.items():
        try:
            found, budget_truncated, edges, revision = _fetch_owner_neighborhood(
                context, from_type, from_id, relationship_type
            )
        except Exception as exc:
            report.failed += len(records)
            report.notes.append(
                f"failed: edges of {from_type}/{from_id} via {relationship_type} "
                f"({exc.__class__.__name__}: {exc})"
            )
            refreshed.extend(records)
            continue
        if not found:
            report.removed += len(records)
            for record in records:
                report.notes.append(f"removed: {_identity_label(record)} (owning entity gone)")
            continue
        for record in records:
            match = next(
                (
                    edge
                    for edge in edges
                    if edge.get("to_type") == record.get("to_type")
                    and edge.get("to_id") == record.get("to_id")
                    and (
                        record.get("edge_key") is None
                        or edge.get("edge_key") == record.get("edge_key")
                    )
                ),
                None,
            )
            if match is not None:
                refreshed.append(
                    normalize_edge_record(
                        match,
                        read_revision=revision,
                        as_of=as_of,
                        receipt_refs=[],
                        source_cmd="ws refresh",
                        config_digest=config_digest,
                    )
                )
                report.refreshed += 1
            elif budget_truncated:
                # Only a budget-truncated scan may have missed a surviving
                # edge; a filter-complete or depth-only-truncated read is
                # authoritative — the edge is genuinely gone.
                report.failed += 1
                report.notes.append(
                    f"failed: {_identity_label(record)} "
                    "(neighborhood budget-truncated; could not confirm)"
                )
                refreshed.append(record)
            else:
                report.removed += 1
                report.notes.append(f"removed: {_identity_label(record)} (edge gone)")
    return refreshed


@ws_group.command("refresh")
@handle_errors
def ws_refresh_cmd() -> None:
    """Re-fetch stale/unknown records; drop deleted ones; leave fresh untouched.

    Entities are re-read via the compact get-entity read; edges via the
    owning entity's bounded neighborhood inspect. Records whose target is
    gone are dropped with a note. The file is rewritten atomically.
    """
    context, path = _ws_paths()
    records = read_records_detailed(path).records
    if not records:
        click.echo("No working-set records to refresh.")
        return
    current_revision = _current_read_revision(context)
    current_config_digest = _current_config_digest(context)
    fresh, stale, unknown = _classify(records, current_revision, current_config_digest)
    to_refresh = stale + unknown
    as_of = format_datetime(utc_now()) or ""
    report = _RefreshReport()

    refreshed_by_identity: dict[tuple[Any, ...], dict[str, Any] | None] = {}
    stale_edge_records = [r for r in to_refresh if r.get("kind") == "edge"]
    for record in to_refresh:
        if record.get("kind") != "entity":
            continue
        try:
            new_record = _fetch_entity_record(context, record, as_of, current_config_digest)
        except Exception as exc:
            report.failed += 1
            report.notes.append(
                f"failed: {_identity_label(record)} ({exc.__class__.__name__}: {exc})"
            )
            refreshed_by_identity[record_identity(record)] = record
            continue
        if new_record is None:
            report.removed += 1
            report.notes.append(f"removed: {_identity_label(record)} (entity gone)")
            refreshed_by_identity[record_identity(record)] = None
        else:
            report.refreshed += 1
            refreshed_by_identity[record_identity(record)] = new_record
    for record in _refresh_edge_records(
        context, stale_edge_records, as_of, report, current_config_digest
    ):
        refreshed_by_identity[record_identity(record)] = record
    removed_edge_identities = {record_identity(r) for r in stale_edge_records} - set(
        refreshed_by_identity
    )
    for identity in removed_edge_identities:
        refreshed_by_identity[identity] = None

    rewritten: list[dict[str, Any]] = []
    for record in records:
        identity = record_identity(record)
        if identity in refreshed_by_identity:
            replacement = refreshed_by_identity[identity]
            if replacement is not None:
                rewritten.append(replacement)
            # None => dropped (target gone); the note was already recorded.
        else:
            rewritten.append(record)
    try:
        write_records(path, rewritten)
    except WorkingSetPathError as exc:
        raise click.UsageError(str(exc)) from exc

    click.echo(
        f"Refreshed {report.refreshed}, removed {report.removed}, "
        f"failed {report.failed} (fresh untouched: {len(fresh)})."
    )
    for note in report.notes:
        click.echo(f"  {note}")


@ws_group.command("clear")
@handle_errors
def ws_clear_cmd() -> None:
    """Delete the current context's records file (working-set dir only)."""
    # _ws_paths refuses a symlink at ANY chain level (root, instance dir,
    # records file) before the containment check and unlink below.
    _context, path = _ws_paths()
    root = working_set_dir().resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise click.UsageError(f"Refusing to delete outside the working-set directory: {resolved}")
    if not resolved.exists():
        click.echo("No working-set records file to clear.")
        return
    resolved.unlink()
    click.echo(f"Cleared {resolved}")


__all__ = ["ws_group"]
