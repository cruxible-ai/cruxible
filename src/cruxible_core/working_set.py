"""Working-set core: record format, normalization, keys, writer, and reader.

This is the transport-neutral core of the agent-local working set — an
opt-in, NON-AUTHORITATIVE JSONL read cache. Two capture surfaces consume it:

- the CLI ``--json`` read commands (:mod:`cruxible_core.cli.working_set`
  holds the CLI opt-in gate, context resolution, and capture hook; the
  ``cruxible ws`` command group is the management surface), and
- the MCP server's read tool handlers (:mod:`cruxible_core.mcp.working_set`,
  opt-in via ``CRUXIBLE_WORKING_SET_DIR``).

Both surfaces are CLIENTS of the daemon: capture happens in the co-located
agent process and the daemon stays blind to it. This module never imports
from ``cruxible_core.cli`` or ``cruxible_core.mcp``.

File layout
    ``<working-set root>/<instance-key>/records.jsonl``. The root defaults to
    ``~/.cruxible/working-set`` and can be redirected with the
    ``CRUXIBLE_WORKING_SET_DIR`` environment variable (precedence:
    explicit env > default home dir; the ``cruxible ws`` group resolves paths
    the same way). The instance key is the daemon instance id in server mode
    and ``local-<sha256(resolved instance root)[:16]>`` in local mode — the
    same instance binding rule continuation tokens use (``local:<root>``),
    hashed so it is filesystem-safe. Line 1 of every file is a ``#``-prefixed
    header marking the cache non-authoritative; readers (this module
    included) must tolerate it, and ``jq``/``rg`` users skip it naturally.

Credential scope (server mode)
    When a bearer credential is configured, the server-mode instance key gains
    a ``-cred-<scope>`` suffix so two different credentials used on one host
    never share (or leak into) each other's records. The scope is derived from
    the credential WITHOUT storing any token material: ``scope =
    sha256(salt || token)[:12]`` where ``salt`` is a random per-root value
    created once at ``<working-set root>/.scope-salt`` (mode 0600). The
    salt never leaves the machine and the hash is truncated, so the scope is
    neither reversible nor correlatable across hosts. Tokenless server mode
    maps to the daemon's single local-operator identity and local mode is
    already partitioned per OS user by ``~`` — both are single-credential
    contexts, so they take no suffix.

Record shape (one JSON object per line)
    ``kind`` (``entity`` | ``edge``), identity fields (``entity_type`` /
    ``entity_id`` or ``relationship_type``/``from_type``/``from_id``/
    ``to_type``/``to_id``/``edge_key``), ``props`` (the compact-profile
    property slice — the same serializer as ``--profile compact``, never a new
    projection), ``lifecycle``, ``review`` (``None`` for entities — they have
    no review axis), ``read_revision`` (``None`` means the source response
    carried no revision and none could be resolved locally; ``ws verify``
    reports such records as ``unknown``), ``config_digest`` (the active config
    digest at capture time — local mode uses the lock digest continuation
    tokens bind to, server mode the daemon's recorded active config digest;
    ``None`` when unresolvable), ``as_of`` (local wall-clock ISO timestamp at
    write), ``receipt_refs`` (receipt ids carried by the source response,
    usually empty or one element), and ``source_cmd`` (the CLI subcommand or
    MCP tool that produced the read). A config reload does not bump
    ``read_revision``, so ``ws verify`` compares ``config_digest`` too: a
    mismatch is ``stale``.

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
    than the capture hooks and the ``cruxible ws`` group. Working-set
    directories (the configured root AND each instance directory) are
    created mode 0700 and records files mode 0600 — and all of them are
    idempotently re-tightened on every write touch, so pre-existing
    lax-mode caches heal on next use; ancestors above the configured root
    are never chmod'd. Every verb — capture, verify, refresh, clear —
    validates the FULL path chain (root, instance dir, records file, and
    the scope-salt file where applicable) with ``lstat``/``O_NOFOLLOW``
    discipline BEFORE its first read, stat, write, or unlink: a symlink at
    any level is refused outright. The reader validates every line against the record
    shape: corrupt or wrong-shaped lines are skipped with a warning on
    stderr and counted, never a crash — and never classified as fresh.
    Deletion (``ws clear``) refuses to touch anything outside the
    working-set directory.

Tamper honesty
    The cache files are SAME-USER-WRITABLE BY DESIGN: any process running as
    the same OS user (including other agents) can rewrite records, and this
    module cannot detect that. The 0700/0600 permission hygiene and symlink
    refusal reduce accidents and cross-user exposure — they are not a
    defense against a same-user adversary. Tamper-evidence would require a
    daemon-signed record mechanism, which is a possible future addition,
    not something these files provide today. Treat every record as a hint
    to re-verify (``cruxible ws verify``), never as proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from cruxible_core.query.profiles import neighborhood_edge_payload, profile_entity_payload
from cruxible_core.server.config import get_runtime_bearer_token
from cruxible_core.temporal import format_datetime, utc_now

WORKING_SET_DIR_ENV = "CRUXIBLE_WORKING_SET_DIR"

HEADER_LINE = "# NON-AUTHORITATIVE CACHE — never a write source; verify with: cruxible ws verify"

# Dedupe-compaction threshold: files past this size are fully rewritten
# (deduped) on the next append instead of growing further.
COMPACT_THRESHOLD_BYTES = 5 * 1024 * 1024

# Instance keys become directory names; anything outside this set (path
# separators, traversal dots-only names, leading dots) is refused so the
# working set can never write or delete outside its own directory.
_INSTANCE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

RecordIdentity = tuple[Any, ...]


class WorkingSetPathError(RuntimeError):
    """A working-set write was refused for path-safety reasons (symlinks)."""


def working_set_dir() -> Path:
    """Return the root directory that holds every per-instance records file.

    Precedence: an explicit ``CRUXIBLE_WORKING_SET_DIR`` environment
    override wins; otherwise the default ``~/.cruxible/working-set``. The
    ``cruxible ws`` command group resolves paths through this same function,
    so it operates on whichever root is active.
    """
    override = os.environ.get(WORKING_SET_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cruxible" / "working-set"


def local_instance_key(root: Path) -> str:
    """Instance key for local (non-server) mode.

    Continuation tokens bind local reads to ``local:<resolved root>``; the
    working set uses the same identity, hashed so it is a safe directory name.
    Local mode needs no credential scope: the cache lives under ``~``, so the
    OS user IS the (single) credential.
    """
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()
    return f"local-{digest[:16]}"


_SCOPE_SALT_FILENAME = ".scope-salt"


def _credential_scope_salt() -> bytes:
    """Load (or create, 0600) the random per-root credential-scope salt.

    Validates the root + salt-file symlink chain BEFORE the existence check
    and read — a symlinked root must not leak or source salt material.
    """
    salt_path = working_set_dir() / _SCOPE_SALT_FILENAME
    refuse_symlinks(salt_path)
    if salt_path.exists():
        return salt_path.read_bytes()
    salt = os.urandom(32)
    _prepare_write_dirs(salt_path)
    fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    return salt


def credential_scope() -> str | None:
    """Non-secret scope tag for the active server-mode credential.

    Derivation: ``sha256(salt || token)[:12]`` with a random per-root salt
    persisted at ``<working-set dir>/.scope-salt`` — never raw token material
    and never an unsalted hash, so the tag cannot be reversed or correlated
    across machines. ``None`` when no bearer credential is configured
    (tokenless server mode is the single local-operator identity).
    """
    token = get_runtime_bearer_token()
    if not token:
        return None
    digest = hashlib.sha256(_credential_scope_salt() + token.encode("utf-8")).hexdigest()
    return digest[:12]


def server_instance_key(instance_id: str) -> str:
    """Instance key for server mode: the daemon instance id, credential-scoped.

    Different credentials against one daemon must never share working-set
    records; the ``-cred-<scope>`` suffix partitions them (see
    :func:`credential_scope` for the non-secret derivation).
    """
    scope = credential_scope()
    return f"{instance_id}-cred-{scope}" if scope else instance_id


def records_path(instance_key: str) -> Path:
    """Return the records file path for *instance_key*, validating the key."""
    if not _INSTANCE_KEY_RE.match(instance_key):
        raise ValueError(f"invalid working-set instance key: {instance_key!r}")
    return working_set_dir() / instance_key / "records.jsonl"


# ---- directory/file hygiene and symlink refusal ----


def _working_set_levels(path: Path) -> tuple[Path, ...]:
    """Every filesystem level from the configured working-set root down to *path*.

    For a records path this is ``(root, instance dir, records file)``; for the
    scope-salt file ``(root, salt file)``. A *path* outside the configured
    root (never produced by this module's own path builders; possible only
    for a caller-supplied path) degrades to ``(parent, path)`` — the levels
    that can still be checked without touching unrelated ancestors.
    """
    root = working_set_dir()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return (path.parent, path)
    levels = [root]
    current = root
    for part in relative.parts:
        current = current / part
        levels.append(current)
    return tuple(levels)


def refuse_symlinks(path: Path) -> None:
    """Refuse symlinks at EVERY level from the working-set root down to *path*.

    The single shared path-validation gate: ``lstat`` discipline on the
    configured root, the instance directory, and the records file (and the
    scope-salt file where applicable), run BEFORE any read/stat/write/unlink
    on any verb. It guards reads too — a symlinked root or instance
    directory would redirect even a ``ws verify`` file read outside the
    working set. Raises :class:`WorkingSetPathError`; capture hooks
    downgrade it to a stderr warning (the read result itself is never
    affected), ``ws`` verbs surface it as a usage error.
    """
    for level in _working_set_levels(path):
        if level.is_symlink():
            raise WorkingSetPathError(
                f"working-set path level is a symlink; refusing to touch: {level}"
            )


def secure_records_path(instance_key: str) -> Path:
    """Validated records path for *instance_key*: the shared verb entry point.

    Validates the key shape (:func:`records_path`) and then runs the full
    symlink-chain refusal (:func:`refuse_symlinks`) so every consumer —
    capture, ``ws status``/``verify``/``refresh``/``clear`` — validates the
    identical chain before its first filesystem access.
    """
    path = records_path(instance_key)
    refuse_symlinks(path)
    return path


def _prepare_write_dirs(path: Path) -> None:
    """Create missing directories and tighten the working-set-owned levels.

    Idempotent on every write touch: the configured root and the instance
    directory are chmod'd 0700 (best-effort) even when they already existed,
    so pre-existing lax-mode caches heal on next use. Ancestors ABOVE the
    configured root are created when missing (default modes) but never
    chmod'd — a user-supplied env root's parents are not ours to manage.
    Callers must run :func:`refuse_symlinks` first.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    levels = _working_set_levels(path)
    root = working_set_dir()
    if levels and levels[0] != root:
        return  # outside the configured root: nothing here is ours to chmod
    for directory in levels[:-1]:  # every level but the file itself
        try:
            os.chmod(directory, 0o700)
        except OSError:  # pragma: no cover - best-effort hygiene
            pass


# ---- record normalization (compact profile serializer, never a new shape) ----


def normalize_entity_record(
    payload: dict[str, Any],
    *,
    read_revision: int | None,
    as_of: str,
    receipt_refs: list[str],
    source_cmd: str,
    config_digest: str | None = None,
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
        "config_digest": config_digest,
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
    config_digest: str | None = None,
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
        "config_digest": config_digest,
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


# ---- typed record validation (used by the tolerant reader) ----

_ENTITY_IDENTITY_FIELDS = ("entity_type", "entity_id")
_EDGE_IDENTITY_FIELDS = ("relationship_type", "from_type", "from_id", "to_type", "to_id")


def _is_optional_int(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def validate_record(record: Any) -> str | None:
    """Validate one parsed line against the record schema; ``None`` when valid.

    Required: ``kind`` plus the identity fields for that kind, and a
    ``props`` object. Typed-when-present: ``edge_key`` (int), ``read_revision``
    (int), ``config_digest`` (str), ``as_of`` (str), ``receipt_refs`` (list),
    ``source_cmd`` (str) — each also accepts ``None``/absent so older records
    (e.g. pre-``config_digest``) stay readable and verify as ``unknown``.
    Returns a short reason string for invalid records; the reader skips and
    counts them, and ``ws verify`` never classifies them as anything but
    invalid.
    """
    if not isinstance(record, dict):
        return "not a JSON object"
    kind = record.get("kind")
    if kind not in ("entity", "edge"):
        return f"unknown kind {kind!r}"
    identity_fields = _ENTITY_IDENTITY_FIELDS if kind == "entity" else _EDGE_IDENTITY_FIELDS
    for field_name in identity_fields:
        value = record.get(field_name)
        if not isinstance(value, str) or not value:
            return f"missing or non-string identity field {field_name!r}"
    if kind == "edge" and not _is_optional_int(record.get("edge_key")):
        return "edge_key must be an integer or null"
    if not isinstance(record.get("props"), dict):
        return "props must be an object"
    if not _is_optional_int(record.get("read_revision")):
        return "read_revision must be an integer or null"
    if not isinstance(record.get("config_digest"), (str, type(None))):
        return "config_digest must be a string or null"
    if not isinstance(record.get("as_of", ""), str):
        return "as_of must be a string"
    if not isinstance(record.get("receipt_refs", []), list):
        return "receipt_refs must be a list"
    if not isinstance(record.get("source_cmd", ""), str):
        return "source_cmd must be a string"
    return None


# ---- tolerant reader / dedupe-aware writer ----


@dataclass
class RecordReadResult:
    """Validated records plus the count of skipped malformed/invalid lines."""

    records: list[dict[str, Any]] = field(default_factory=list)
    invalid_lines: int = 0


def read_records_detailed(path: Path) -> RecordReadResult:
    """Read and validate working-set records, tolerating header/corrupt lines.

    Corrupt, non-object, or schema-invalid lines are skipped with a warning
    on stderr and counted in ``invalid_lines`` — a damaged cache line must
    never crash a read and must never be classified as a (fresh) record.
    """
    if not path.exists():
        return RecordReadResult()
    result = RecordReadResult()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            working_set_warning(f"skipping corrupt working-set line {line_number} in {path}")
            result.invalid_lines += 1
            continue
        reason = validate_record(record)
        if reason is not None:
            working_set_warning(
                f"skipping invalid working-set line {line_number} in {path} ({reason})"
            )
            result.invalid_lines += 1
            continue
        result.records.append(record)
    return result


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read working-set records (validated); see :func:`read_records_detailed`."""
    return read_records_detailed(path).records


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
    """Atomically rewrite the records file (header line first).

    Refuses a symlink at any chain level (:func:`refuse_symlinks`) BEFORE
    creating or touching anything; the temp file is created 0600 so the
    renamed result keeps private permissions, and the root + instance
    directories are idempotently tightened to 0700 on every touch.
    """
    refuse_symlinks(path)
    _prepare_write_dirs(path)
    lines = [HEADER_LINE]
    lines.extend(json.dumps(record, default=str) for record in records)
    temp_path = path.parent / f".{path.name}.tmp.{os.getpid()}"
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    temp_path.replace(path)


def append_records(path: Path, new_records: list[dict[str, Any]]) -> None:
    """Append records with identity dedupe.

    Fast path (new identities only): one ``O_APPEND`` write of whole lines.
    Any superseded existing line — or a file past ``COMPACT_THRESHOLD_BYTES``
    — triggers an atomic dedupe-compaction rewrite instead. A symlink at any
    chain level (root, instance dir, records file) is refused BEFORE the
    internal read (:func:`refuse_symlinks` + ``O_NOFOLLOW``).
    """
    if not new_records:
        return
    refuse_symlinks(path)
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
    _prepare_write_dirs(path)
    chunk = "".join(json.dumps(record, default=str) + "\n" for record in appended)
    if not file_exists:
        chunk = HEADER_LINE + "\n" + chunk
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        # Idempotent tightening: pre-existing lax-mode files become 0600 on
        # the next capture touch, not only at creation.
        os.fchmod(fd, 0o600)
        os.write(fd, chunk.encode("utf-8"))
    finally:
        os.close(fd)


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

    Understands every serialized read shape routed through the profile
    serializer: query rows (entity / relationship / path / projected, with
    includes), graph-layout nodes/edges, get-entity, inspect (single-hop
    neighbors get their edge endpoints synthesized from the root +
    direction), bounded neighborhoods, list pages, and samples.
    ``found: false`` payloads yield nothing.
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


# ---- shared capture core (normalize + stamp + append) ----


def capture_read_payload(
    payload: Any,
    *,
    instance_key: str,
    source_cmd: str,
    read_revision: int | None = None,
    fallback_revision: int | None = None,
    config_digest: str | None = None,
) -> None:
    """Normalize and append every entity/edge in *payload* for *instance_key*.

    The shared core of both capture surfaces (CLI ``--json`` reads and MCP
    read tools). ``read_revision`` resolution order: the payload envelope's
    ``read_revision``, then the explicit ``read_revision`` argument, then
    ``fallback_revision``; ``None`` when all are unavailable (recorded as-is;
    ``ws verify`` reports such records as ``unknown``). Raises on failure —
    callers gate opt-in and downgrade errors to stderr warnings.
    """
    pairs = extract_read_records(payload)
    if not pairs:
        return
    effective_revision = read_revision
    if isinstance(payload, dict) and isinstance(payload.get("read_revision"), int):
        effective_revision = payload["read_revision"]
    if effective_revision is None:
        effective_revision = fallback_revision
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
                    config_digest=config_digest,
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
                    config_digest=config_digest,
                )
            )
    append_records(records_path(instance_key), records)


def working_set_warning(message: str) -> None:
    """Emit one working-set warning to stderr (shared by every capture layer)."""
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
    "WORKING_SET_DIR_ENV",
    "RecordReadResult",
    "WorkingSetPathError",
    "append_records",
    "capture_read_payload",
    "credential_scope",
    "extract_read_records",
    "iter_record_lines",
    "local_instance_key",
    "normalize_edge_record",
    "normalize_entity_record",
    "read_records",
    "read_records_detailed",
    "record_identity",
    "record_wins",
    "records_path",
    "refuse_symlinks",
    "secure_records_path",
    "server_instance_key",
    "validate_record",
    "working_set_dir",
    "working_set_warning",
    "write_records",
]
