"""Agent-local working set: an opt-in, NON-AUTHORITATIVE JSONL read cache.

Agents grep. This module gives them a normalized, ``rg``-able local cache of
everything they've inspected through the CLI's ``--json`` read surfaces, so
re-finding something costs a grep instead of a re-query. It is a PROTOTYPE
(promotion to a default read path is gated on the RuneBench pilot) and it is
explicitly non-authoritative: every record is revision-stamped and verifiable
with ``cruxible ws verify``.

Opt-in gates
    Capture happens only when ``CRUXIBLE_WORKING_SET=1`` (or ``true``/``yes``/
    ``on``) is set in the environment, or the read command was invoked with
    ``--ws``. When neither gate is open every function here is a no-op: no
    file is created and read commands behave byte-for-byte identically.

File layout
    ``~/.cruxible/working-set/<instance-key>/records.jsonl`` where the
    instance key is the daemon instance id in server mode and
    ``local-<sha256(resolved instance root)[:16]>`` in local mode — the same
    instance binding rule continuation tokens use (``local:<root>``), hashed
    so it is filesystem-safe. Line 1 of every file is a ``#``-prefixed header
    marking the cache non-authoritative; readers (this module included) must
    tolerate it, and ``jq``/``rg`` users skip it naturally.

Record shape (one JSON object per line)
    ``kind`` (``entity`` | ``edge``), identity fields (``entity_type`` /
    ``entity_id`` or ``relationship_type``/``from_type``/``from_id``/
    ``to_type``/``to_id``/``edge_key``), ``props`` (the compact-profile
    property slice — the same serializer as ``--profile compact``, never a new
    projection), ``lifecycle``, ``review`` (``None`` for entities — they have
    no review axis), ``read_revision`` (``None`` means the source response
    carried no revision and none could be resolved locally; ``ws verify``
    reports such records as ``unknown``), ``as_of`` (local wall-clock ISO
    timestamp at write), ``receipt_refs`` (receipt ids carried by the source
    response, usually empty or one element), and ``source_cmd`` (the CLI
    subcommand that produced the read).

Dedupe
    Appends dedupe by identity key: the record with the newest
    ``read_revision`` wins; a missing revision loses to any concrete one;
    ties are broken by the latest ``as_of``. Superseding an existing line (or
    exceeding ``COMPACT_THRESHOLD_BYTES``) triggers a dedupe-compaction: the
    whole file is rewritten atomically (temp file + rename).

Concurrency limits (honest edition)
    Plain appends use a single ``O_APPEND`` ``os.write`` per capture, so
    concurrent processes interleave whole lines rather than bytes on POSIX
    for writes up to the platform's atomic-write size; very large captures
    may still interleave, and two processes creating the file simultaneously
    can each emit a header line (readers tolerate ``#`` lines anywhere).
    Dedupe-compaction is last-writer-wins between concurrent processes: a
    rewrite can discard lines another process appended after the rewrite
    started. This is acceptable because the cache is best-effort and never
    authoritative — a lost line costs one re-query.

Safety rails
    The cache is NEVER read by any write path or by any CLI command other
    than the capture hooks and the ``cruxible ws`` group. Corrupt lines are
    skipped with a warning on stderr, never a crash. Deletion (``ws clear``)
    refuses to touch anything outside the working-set directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

import click

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.query.profiles import neighborhood_edge_payload, profile_entity_payload
from cruxible_core.temporal import format_datetime, utc_now

WORKING_SET_ENV = "CRUXIBLE_WORKING_SET"

HEADER_LINE = "# NON-AUTHORITATIVE CACHE — never a write source; verify with: cruxible ws verify"

# Dedupe-compaction threshold: files past this size are fully rewritten
# (deduped) on the next append instead of growing further.
COMPACT_THRESHOLD_BYTES = 5 * 1024 * 1024

# Instance keys become directory names; anything outside this set (path
# separators, traversal dots-only names, leading dots) is refused so the
# working set can never write or delete outside its own directory.
_INSTANCE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_ENV_TRUE = {"1", "true", "yes", "on"}

RecordIdentity = tuple[Any, ...]


def working_set_enabled(ws_flag: bool = False) -> bool:
    """Return whether working-set capture is enabled for this invocation."""
    if ws_flag:
        return True
    return os.environ.get(WORKING_SET_ENV, "").strip().lower() in _ENV_TRUE


def working_set_dir() -> Path:
    """Return the root directory that holds every per-instance records file."""
    return Path.home() / ".cruxible" / "working-set"


def local_instance_key(root: Path) -> str:
    """Instance key for CLI local mode.

    Continuation tokens bind local reads to ``local:<resolved root>``; the
    working set uses the same identity, hashed so it is a safe directory name.
    """
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()
    return f"local-{digest[:16]}"


def records_path(instance_key: str) -> Path:
    """Return the records file path for *instance_key*, validating the key."""
    if not _INSTANCE_KEY_RE.match(instance_key):
        raise ValueError(f"invalid working-set instance key: {instance_key!r}")
    return working_set_dir() / instance_key / "records.jsonl"


def _cli_root_obj() -> dict[str, Any]:
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return {}
    root = ctx.find_root()
    root.ensure_object(dict)
    obj = root.obj
    return obj if isinstance(obj, dict) else {}


def resolve_capture_context() -> tuple[str, CruxibleInstance | None] | None:
    """Resolve (instance_key, local instance) for the current CLI invocation.

    Server mode returns the daemon instance id with no local instance; local
    mode loads the on-disk instance (cheap: one metadata file read). Returns
    ``None`` when no instance can be resolved — capture then no-ops.
    """
    obj = _cli_root_obj()
    if obj.get("server_url") or obj.get("server_socket"):
        instance_id = obj.get("instance_id")
        return (str(instance_id), None) if instance_id else None
    try:
        instance = CruxibleInstance.load()
    except Exception:
        return None
    return local_instance_key(instance.get_root_path()), instance


# ---- record normalization (compact profile serializer, never a new shape) ----


def normalize_entity_record(
    payload: dict[str, Any],
    *,
    read_revision: int | None,
    as_of: str,
    receipt_refs: list[str],
    source_cmd: str,
) -> dict[str, Any]:
    """Build one entity working-set record from a serialized entity payload."""
    compact = profile_entity_payload(
        {
            "entity_type": payload.get("entity_type"),
            "entity_id": payload.get("entity_id"),
            "properties": payload.get("properties") or {},
            "metadata": payload.get("metadata") or {},
        },
        "compact",
    )
    return {
        "kind": "entity",
        "entity_type": compact.get("entity_type"),
        "entity_id": compact.get("entity_id"),
        "props": compact.get("properties") or {},
        "lifecycle": (compact.get("metadata") or {}).get("lifecycle"),
        "review": None,
        "read_revision": read_revision,
        "as_of": as_of,
        "receipt_refs": receipt_refs,
        "source_cmd": source_cmd,
    }


def normalize_edge_record(
    payload: dict[str, Any],
    *,
    read_revision: int | None,
    as_of: str,
    receipt_refs: list[str],
    source_cmd: str,
) -> dict[str, Any]:
    """Build one edge working-set record from a serialized edge payload."""
    compact = neighborhood_edge_payload(payload, "compact")
    assertion = (compact.get("metadata") or {}).get("assertion") or {}
    return {
        "kind": "edge",
        "relationship_type": compact.get("relationship_type"),
        "from_type": compact.get("from_type"),
        "from_id": compact.get("from_id"),
        "to_type": compact.get("to_type"),
        "to_id": compact.get("to_id"),
        "edge_key": compact.get("edge_key"),
        "props": compact.get("properties") or {},
        "lifecycle": assertion.get("lifecycle"),
        "review": assertion.get("review"),
        "read_revision": read_revision,
        "as_of": as_of,
        "receipt_refs": receipt_refs,
        "source_cmd": source_cmd,
    }


def record_identity(record: dict[str, Any]) -> RecordIdentity:
    """Dedupe identity key for one working-set record."""
    if record.get("kind") == "entity":
        return ("entity", record.get("entity_type"), record.get("entity_id"))
    return (
        "edge",
        record.get("relationship_type"),
        record.get("from_type"),
        record.get("from_id"),
        record.get("to_type"),
        record.get("to_id"),
        record.get("edge_key"),
    )


def record_wins(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """Newest ``read_revision`` wins; missing loses; ties -> latest ``as_of``."""
    new_rev = new.get("read_revision")
    old_rev = old.get("read_revision")
    new_rank = -1 if not isinstance(new_rev, int) else new_rev
    old_rank = -1 if not isinstance(old_rev, int) else old_rev
    if new_rank != old_rank:
        return new_rank > old_rank
    return str(new.get("as_of") or "") >= str(old.get("as_of") or "")


# ---- payload walker: find entity/edge-shaped data in emitted JSON ----


def _is_edge_shaped(node: dict[str, Any]) -> bool:
    return all(
        isinstance(node.get(key), str)
        for key in ("relationship_type", "from_type", "from_id", "to_type", "to_id")
    )


def _is_entity_shaped(node: dict[str, Any]) -> bool:
    return (
        isinstance(node.get("entity_type"), str)
        and isinstance(node.get("entity_id"), str)
        and isinstance(node.get("properties"), dict)
    )


# Keys whose values are user data / envelope internals, never nested
# entity/edge payloads. Skipping them keeps property VALUES that merely look
# entity-shaped from being captured as records.
_OPAQUE_KEYS = frozenset(
    {"properties", "metadata", "props", "values", "param_hints", "policy_summary", "receipt"}
)


def extract_read_records(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    """Extract ``(kind, payload)`` pairs for every entity/edge in a read payload.

    Understands every ``--json`` read shape routed through the profile
    serializer: query rows (entity / relationship / path / projected, with
    includes), get-entity, inspect (single-hop neighbors get their edge
    endpoints synthesized from the root + direction), bounded neighborhoods,
    list pages, and samples. ``found: false`` payloads yield nothing.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    _walk(payload, found)
    return found


def _walk(node: Any, found: list[tuple[str, dict[str, Any]]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, found)
        return
    if not isinstance(node, dict):
        return
    if node.get("found") is False:
        return
    if _is_edge_shaped(node):
        found.append(("edge", node))
        for key, value in node.items():
            if key not in _OPAQUE_KEYS:
                _walk(value, found)
        return
    if _is_entity_shaped(node):
        found.append(("entity", node))
        for key, value in node.items():
            if key in _OPAQUE_KEYS:
                continue
            if key == "neighbors" and isinstance(value, list):
                for neighbor in value:
                    _walk_single_hop_neighbor(neighbor, node, found)
                continue
            _walk(value, found)
        return
    for key, value in node.items():
        if key not in _OPAQUE_KEYS:
            _walk(value, found)


def _walk_single_hop_neighbor(
    neighbor: Any,
    root: dict[str, Any],
    found: list[tuple[str, dict[str, Any]]],
) -> None:
    """Capture one single-hop inspect neighbor row.

    The legacy inspect neighbor row carries the relationship without its
    endpoints; the root entity plus ``direction`` determine them, so the edge
    payload is synthesized here (outgoing: root -> neighbor; incoming:
    neighbor -> root) before normalization.
    """
    if not isinstance(neighbor, dict):
        return
    entity = neighbor.get("entity")
    _walk(entity, found)
    direction = neighbor.get("direction")
    if (
        not isinstance(entity, dict)
        or not _is_entity_shaped(entity)
        or not isinstance(neighbor.get("relationship_type"), str)
        or direction not in ("incoming", "outgoing")
    ):
        return
    if direction == "outgoing":
        endpoints = (root, entity)
    else:
        endpoints = (entity, root)
    found.append(
        (
            "edge",
            {
                "relationship_type": neighbor["relationship_type"],
                "from_type": endpoints[0].get("entity_type"),
                "from_id": endpoints[0].get("entity_id"),
                "to_type": endpoints[1].get("entity_type"),
                "to_id": endpoints[1].get("entity_id"),
                "edge_key": neighbor.get("edge_key"),
                "properties": neighbor.get("properties") or {},
                "metadata": neighbor.get("metadata") or {},
            },
        )
    )


# ---- tolerant reader / dedupe-aware writer ----


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read working-set records, tolerating the header and corrupt lines.

    Corrupt or non-object lines are skipped with a warning on stderr — a
    damaged cache line must never crash a read.
    """
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            _warn(f"skipping corrupt working-set line {line_number} in {path}")
            continue
        if not isinstance(record, dict):
            _warn(f"skipping non-object working-set line {line_number} in {path}")
            continue
        records.append(record)
    return records


def _dedupe(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Collapse duplicates in file order; returns (deduped, changed)."""
    by_identity: dict[RecordIdentity, int] = {}
    deduped: list[dict[str, Any]] = []
    changed = False
    for record in records:
        identity = record_identity(record)
        if identity in by_identity:
            changed = True
            index = by_identity[identity]
            if record_wins(record, deduped[index]):
                deduped[index] = record
        else:
            by_identity[identity] = len(deduped)
            deduped.append(record)
    return deduped, changed


def write_records(path: Path, records: list[dict[str, Any]]) -> None:
    """Atomically rewrite the records file (header line first)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [HEADER_LINE]
    lines.extend(json.dumps(record, default=str) for record in records)
    temp_path = path.parent / f".{path.name}.tmp.{os.getpid()}"
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_path.replace(path)


def append_records(path: Path, new_records: list[dict[str, Any]]) -> None:
    """Append records with identity dedupe.

    Fast path (new identities only): one ``O_APPEND`` write of whole lines.
    Any superseded existing line — or a file past ``COMPACT_THRESHOLD_BYTES``
    — triggers an atomic dedupe-compaction rewrite instead.
    """
    if not new_records:
        return
    fresh, _ = _dedupe(new_records)
    existing, had_duplicates = _dedupe(read_records(path))
    by_identity = {record_identity(record): index for index, record in enumerate(existing)}

    appended: list[dict[str, Any]] = []
    superseded = False
    for record in fresh:
        identity = record_identity(record)
        if identity in by_identity:
            index = by_identity[identity]
            if record_wins(record, existing[index]):
                existing[index] = record
                superseded = True
        else:
            by_identity[identity] = len(existing)
            existing.append(record)
            appended.append(record)

    file_exists = path.exists()
    oversized = file_exists and path.stat().st_size > COMPACT_THRESHOLD_BYTES
    if superseded or oversized or (file_exists and had_duplicates):
        write_records(path, existing)
        return
    if not appended:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    chunk = "".join(json.dumps(record, default=str) + "\n" for record in appended)
    if not file_exists:
        chunk = HEADER_LINE + "\n" + chunk
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, chunk.encode("utf-8"))
    finally:
        os.close(fd)


# ---- capture entry point ----


def capture_json_read(
    payload: Any,
    *,
    source_cmd: str,
    ws_flag: bool = False,
    read_revision: int | None = None,
) -> None:
    """Capture entity/edge-shaped data from an emitted ``--json`` read payload.

    A pure side effect: it runs AFTER the payload has been printed, never
    filters or mutates it, and swallows its own failures (warning on stderr).
    ``read_revision`` resolution order: the payload envelope's
    ``read_revision``, then the explicit argument (threaded from the parsed
    result object), then the local instance's current revision; ``None`` when
    all three are unavailable (recorded as-is; ``ws verify`` reports those
    records as ``unknown``).
    """
    if not working_set_enabled(ws_flag):
        return
    try:
        context = resolve_capture_context()
        if context is None:
            return
        instance_key, local_instance = context
        pairs = extract_read_records(payload)
        if not pairs:
            return
        effective_revision = read_revision
        if isinstance(payload, dict) and isinstance(payload.get("read_revision"), int):
            effective_revision = payload["read_revision"]
        if effective_revision is None and local_instance is not None:
            effective_revision = local_instance.get_read_revision()
        receipt_refs: list[str] = []
        if isinstance(payload, dict) and isinstance(payload.get("receipt_id"), str):
            receipt_refs = [payload["receipt_id"]]
        as_of = format_datetime(utc_now()) or ""
        records = []
        for kind, raw in pairs:
            if kind == "entity":
                records.append(
                    normalize_entity_record(
                        raw,
                        read_revision=effective_revision,
                        as_of=as_of,
                        receipt_refs=receipt_refs,
                        source_cmd=source_cmd,
                    )
                )
            else:
                records.append(
                    normalize_edge_record(
                        raw,
                        read_revision=effective_revision,
                        as_of=as_of,
                        receipt_refs=receipt_refs,
                        source_cmd=source_cmd,
                    )
                )
        append_records(records_path(instance_key), records)
    except Exception as exc:  # pragma: no cover - capture must never break a read
        _warn(f"working-set capture failed ({exc.__class__.__name__}: {exc})")


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def iter_record_lines(path: Path) -> Iterator[str]:
    """Yield raw record lines (header skipped) — convenience for tooling."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped


__all__ = [
    "COMPACT_THRESHOLD_BYTES",
    "HEADER_LINE",
    "WORKING_SET_ENV",
    "append_records",
    "capture_json_read",
    "extract_read_records",
    "iter_record_lines",
    "local_instance_key",
    "normalize_edge_record",
    "normalize_entity_record",
    "read_records",
    "record_identity",
    "record_wins",
    "records_path",
    "resolve_capture_context",
    "working_set_dir",
    "working_set_enabled",
    "write_records",
]
