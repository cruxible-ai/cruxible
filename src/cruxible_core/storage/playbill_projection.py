"""Concrete immutable SQLite storage for Playbill projection contracts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cruxible_core.playbill.canonical import (
    LogicalDigest,
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_core.playbill.errors import ProjectionIntegrityError
from cruxible_core.playbill.projection import (
    PROJECTION_SCHEMA_VERSION,
    AcceptedProjectionCoordinate,
    AssemblerRequest,
    ProjectionManifest,
    ProjectionOrphan,
    projection_manifest_name,
    render_projection_manifest,
)
from cruxible_core.playbill.projection_artifacts import ParsedProjectionTree
from cruxible_core.playbill.projection_extensions import ProjectionExtensionRegistry
from cruxible_core.playbill.types import CompilerCoordinate

_PIECE_RE = re.compile(r"^piece-[0-9a-f]{64}-[0-9]{4}\.sqlite$")
_MANIFEST_RE = re.compile(r"^projection-[0-9a-f]{64}\.json$")


@dataclass(frozen=True)
class _TableSpec:
    name: str
    create_sql: str
    columns: tuple[tuple[str, str, bool], ...]
    primary_key: tuple[str, ...]
    constraints: tuple[str, ...]
    indexes: tuple[tuple[str, str], ...]
    logical: bool


_TABLE_SPECS = (
    _TableSpec(
        name="artifact_envelopes",
        create_sql=(
            "CREATE TABLE artifact_envelopes ("
            "identity TEXT PRIMARY KEY, kind TEXT NOT NULL, format_tag TEXT NOT NULL, "
            "path TEXT NOT NULL UNIQUE, artifact_digest TEXT NOT NULL, "
            "predecessor_digest TEXT, revision INTEGER NOT NULL CHECK(revision >= 1)) STRICT"
        ),
        columns=(
            ("identity", "TEXT", False),
            ("kind", "TEXT", False),
            ("format_tag", "TEXT", False),
            ("path", "TEXT", False),
            ("artifact_digest", "TEXT", False),
            ("predecessor_digest", "TEXT", True),
            ("revision", "INTEGER", False),
        ),
        primary_key=("identity",),
        constraints=("check(revision>=1)", "unique(path)"),
        indexes=(("idx_artifact_envelopes_kind", "kind,identity"),),
        logical=True,
    ),
    _TableSpec(
        name="live_identities",
        create_sql=(
            "CREATE TABLE live_identities (identity TEXT PRIMARY KEY, "
            "artifact_digest TEXT NOT NULL, path TEXT NOT NULL) STRICT"
        ),
        columns=(
            ("identity", "TEXT", False),
            ("artifact_digest", "TEXT", False),
            ("path", "TEXT", False),
        ),
        primary_key=("identity",),
        constraints=(),
        indexes=(),
        logical=True,
    ),
    _TableSpec(
        name="pins",
        create_sql=(
            "CREATE TABLE pins (source_identity TEXT NOT NULL, target_identity TEXT NOT NULL, "
            "target_digest TEXT NOT NULL, PRIMARY KEY(source_identity,target_identity)) STRICT"
        ),
        columns=(
            ("source_identity", "TEXT", False),
            ("target_identity", "TEXT", False),
            ("target_digest", "TEXT", False),
        ),
        primary_key=("source_identity", "target_identity"),
        constraints=("unique(source_identity,target_identity)",),
        indexes=(("idx_pins_target", "target_identity,source_identity"),),
        logical=True,
    ),
    _TableSpec(
        name="projection_fact_schemas",
        create_sql=(
            "CREATE TABLE projection_fact_schemas (schema_id TEXT NOT NULL, "
            "schema_version INTEGER NOT NULL CHECK(schema_version >= 1), "
            "constraints_json TEXT NOT NULL, PRIMARY KEY(schema_id,schema_version)) STRICT"
        ),
        columns=(
            ("schema_id", "TEXT", False),
            ("schema_version", "INTEGER", False),
            ("constraints_json", "TEXT", False),
        ),
        primary_key=("schema_id", "schema_version"),
        constraints=("check(schema_version>=1)",),
        indexes=(),
        logical=True,
    ),
    _TableSpec(
        name="semantic_facts",
        create_sql=(
            "CREATE TABLE semantic_facts (schema_id TEXT NOT NULL, "
            "schema_version INTEGER NOT NULL, "
            "subject_identity TEXT NOT NULL, fact_key TEXT NOT NULL, value_json TEXT NOT NULL, "
            "PRIMARY KEY(schema_id,schema_version,subject_identity,fact_key)) STRICT"
        ),
        columns=(
            ("schema_id", "TEXT", False),
            ("schema_version", "INTEGER", False),
            ("subject_identity", "TEXT", False),
            ("fact_key", "TEXT", False),
            ("value_json", "TEXT", False),
        ),
        primary_key=("schema_id", "schema_version", "subject_identity", "fact_key"),
        constraints=("unique(schema_id,schema_version,subject_identity,fact_key)",),
        indexes=(("idx_semantic_facts_subject", "subject_identity,schema_id,fact_key"),),
        logical=True,
    ),
    _TableSpec(
        name="compiler_coordinates",
        create_sql=(
            "CREATE TABLE compiler_coordinates (singleton INTEGER PRIMARY KEY "
            "CHECK(singleton = 1), "
            "implementation TEXT NOT NULL, schema_version INTEGER NOT NULL, "
            "compiler_digest TEXT NOT NULL) STRICT"
        ),
        columns=(
            ("singleton", "INTEGER", False),
            ("implementation", "TEXT", False),
            ("schema_version", "INTEGER", False),
            ("compiler_digest", "TEXT", False),
        ),
        primary_key=("singleton",),
        constraints=("check(singleton=1)",),
        indexes=(),
        logical=True,
    ),
    _TableSpec(
        name="generation_metadata",
        create_sql=(
            "CREATE TABLE generation_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
            "instance_id TEXT NOT NULL, git_object_format TEXT NOT NULL, git_oid TEXT NOT NULL, "
            "semantic_root TEXT NOT NULL, generation_root TEXT NOT NULL) STRICT"
        ),
        columns=(
            ("singleton", "INTEGER", False),
            ("instance_id", "TEXT", False),
            ("git_object_format", "TEXT", False),
            ("git_oid", "TEXT", False),
            ("semantic_root", "TEXT", False),
            ("generation_root", "TEXT", False),
        ),
        primary_key=("singleton",),
        constraints=("check(singleton=1)",),
        indexes=(),
        logical=False,
    ),
    _TableSpec(
        name="presentation_fact_schemas",
        create_sql=(
            "CREATE TABLE presentation_fact_schemas (schema_id TEXT NOT NULL, "
            "schema_version INTEGER NOT NULL, constraints_json TEXT NOT NULL, "
            "PRIMARY KEY(schema_id,schema_version)) STRICT"
        ),
        columns=(
            ("schema_id", "TEXT", False),
            ("schema_version", "INTEGER", False),
            ("constraints_json", "TEXT", False),
        ),
        primary_key=("schema_id", "schema_version"),
        constraints=(),
        indexes=(),
        logical=False,
    ),
    _TableSpec(
        name="presentation_facts",
        create_sql=(
            "CREATE TABLE presentation_facts (schema_id TEXT NOT NULL, "
            "schema_version INTEGER NOT NULL, "
            "subject_identity TEXT NOT NULL, fact_key TEXT NOT NULL, value_json TEXT NOT NULL, "
            "PRIMARY KEY(schema_id,schema_version,subject_identity,fact_key)) STRICT"
        ),
        columns=(
            ("schema_id", "TEXT", False),
            ("schema_version", "INTEGER", False),
            ("subject_identity", "TEXT", False),
            ("fact_key", "TEXT", False),
            ("value_json", "TEXT", False),
        ),
        primary_key=("schema_id", "schema_version", "subject_identity", "fact_key"),
        constraints=(),
        indexes=(),
        logical=False,
    ),
)


def _canonical_json_text(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def initialize_projection_database(
    path: Path,
    *,
    request: AssemblerRequest,
    parsed: ParsedProjectionTree,
    registry: ProjectionExtensionRegistry,
    compiler: CompilerCoordinate,
) -> dict[str, int]:
    """Create and populate the complete PB-B one-piece SQLite projection."""

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA user_version={PROJECTION_SCHEMA_VERSION}")
        for spec in _TABLE_SPECS:
            connection.execute(spec.create_sql)
            for index_name, columns in spec.indexes:
                connection.execute(f"CREATE INDEX {index_name} ON {spec.name} ({columns})")

        connection.executemany(
            "INSERT INTO artifact_envelopes VALUES (?,?,?,?,?,?,?)",
            [
                (
                    row.identity,
                    row.kind,
                    row.format_tag,
                    row.path,
                    row.artifact_digest,
                    row.predecessor_digest,
                    row.revision,
                )
                for row in parsed.envelopes
            ],
        )
        connection.executemany(
            "INSERT INTO live_identities VALUES (?,?,?)",
            [(row.identity, row.artifact_digest, row.path) for row in parsed.envelopes],
        )
        connection.executemany(
            "INSERT INTO pins VALUES (?,?,?)",
            [(row.source_identity, row.target_identity, row.target_digest) for row in parsed.pins],
        )
        semantic_declarations = registry.declarations("semantic")
        presentation_declarations = registry.declarations("presentation")
        connection.executemany(
            "INSERT INTO projection_fact_schemas VALUES (?,?,?)",
            [
                (
                    declaration.schema_id,
                    declaration.schema_version,
                    _canonical_json_text(list(declaration.constraints)),
                )
                for declaration in semantic_declarations
            ],
        )
        connection.executemany(
            "INSERT INTO semantic_facts VALUES (?,?,?,?,?)",
            [
                (
                    fact.schema_id,
                    fact.schema_version,
                    fact.subject_identity,
                    fact.fact_key,
                    _canonical_json_text(fact.value),
                )
                for fact in parsed.semantic_facts
            ],
        )
        connection.execute(
            "INSERT INTO compiler_coordinates VALUES (1,?,?,?)",
            (compiler.implementation, request.schema_version, request.compiler_digest),
        )
        connection.execute(
            "INSERT INTO generation_metadata VALUES (1,?,?,?,?,?)",
            (
                request.instance_id,
                request.git_object_format,
                request.git_oid,
                request.semantic_root,
                request.generation_root,
            ),
        )
        connection.executemany(
            "INSERT INTO presentation_fact_schemas VALUES (?,?,?)",
            [
                (
                    declaration.schema_id,
                    declaration.schema_version,
                    _canonical_json_text(list(declaration.constraints)),
                )
                for declaration in presentation_declarations
            ],
        )
        connection.executemany(
            "INSERT INTO presentation_facts VALUES (?,?,?,?,?)",
            [
                (
                    fact.schema_id,
                    fact.schema_version,
                    fact.subject_identity,
                    fact.fact_key,
                    _canonical_json_text(fact.value),
                )
                for fact in parsed.presentation_facts
            ],
        )
        connection.commit()
        _verify_projection_schema(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise ProjectionIntegrityError("new projection failed SQLite integrity_check")
        counts = {
            spec.name: cast(
                int,
                connection.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0],
            )
            for spec in _TABLE_SPECS
        }
        return dict(sorted(counts.items(), key=lambda item: item[0].encode("utf-8")))
    except sqlite3.DatabaseError as exc:
        raise ProjectionIntegrityError("failed to construct the SQLite projection") from exc
    finally:
        connection.close()


def _verify_projection_schema(connection: sqlite3.Connection) -> None:
    version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
    if version != PROJECTION_SCHEMA_VERSION:
        raise ProjectionIntegrityError("projection SQLite schema version is unsupported")
    expected: dict[tuple[str, str], str] = {}
    for spec in _TABLE_SPECS:
        expected[("table", spec.name)] = spec.create_sql
        for index_name, columns in spec.indexes:
            expected[("index", index_name)] = (
                f"CREATE INDEX {index_name} ON {spec.name} ({columns})"
            )
    actual = {
        (cast(str, object_type), cast(str, name)): cast(str, sql)
        for object_type, name, sql in connection.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if actual != expected:
        raise ProjectionIntegrityError("projection SQLite schema differs from the PB-B registry")


def canonical_logical_export(path: Path) -> dict[str, object]:
    """Export logical tables independent of page layout and binding metadata."""

    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            _verify_projection_schema(connection)
            tables: list[dict[str, object]] = []
            for spec in sorted(_TABLE_SPECS, key=lambda item: item.name.encode("utf-8")):
                if not spec.logical:
                    continue
                order = ",".join(spec.primary_key)
                rows = connection.execute(f"SELECT * FROM {spec.name} ORDER BY {order}").fetchall()
                tables.append(
                    {
                        "name": spec.name,
                        "columns": [
                            {"name": name, "type": sql_type, "nullable": nullable}
                            for name, sql_type, nullable in spec.columns
                        ],
                        "primary_key": list(spec.primary_key),
                        "constraints": list(spec.constraints),
                        "indexes": [
                            {"name": name, "columns": columns.split(",")}
                            for name, columns in spec.indexes
                        ],
                        "rows": [list(row) for row in rows],
                    }
                )
            return {
                "schema_version": PROJECTION_SCHEMA_VERSION,
                "tables": tables,
            }
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        raise ProjectionIntegrityError(
            "projection cannot produce a canonical logical export"
        ) from exc


def projection_logical_digest(path: Path) -> LogicalDigest:
    return typed_digest(
        LogicalDigest,
        "playbill-projection-logical-v1",
        canonical_logical_export(path),
    )


def physical_file_digest(path: Path) -> Sha256Value:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProjectionIntegrityError("projection piece cannot be read") from exc
    return Sha256Value(digest.hexdigest())


def load_projection_manifest(path: Path) -> ProjectionManifest:
    if path.is_symlink() or not path.is_file():
        raise ProjectionIntegrityError("projection manifest must be a regular file")
    try:
        raw = path.read_bytes()
        manifest = ProjectionManifest.model_validate_json(raw)
    except Exception as exc:
        raise ProjectionIntegrityError("projection manifest is missing or malformed") from exc
    if render_projection_manifest(manifest) != raw:
        raise ProjectionIntegrityError("projection manifest is not canonical")
    if path.name != projection_manifest_name(manifest):
        raise ProjectionIntegrityError("projection manifest name does not match its coordinates")
    return manifest


def _manifest_matches_coordinate(
    manifest: ProjectionManifest,
    expected: AcceptedProjectionCoordinate,
) -> bool:
    return (
        manifest.instance_id == expected.instance_id
        and manifest.git_object_format == expected.git_object_format
        and manifest.git_oid == expected.git_oid
        and manifest.semantic_root == expected.semantic_root
        and manifest.generation_root == expected.generation_root
        and manifest.compiler_digest == expected.compiler.rule_digest
        and manifest.schema_version == expected.compiler.schema_version
    )


class ProjectionHandle:
    """An immutable read handle whose complete build was verified exactly once."""

    def __init__(
        self,
        *,
        manifest_path: Path,
        manifest: ProjectionManifest,
        piece_paths: tuple[Path, ...],
        connection: sqlite3.Connection,
    ) -> None:
        self.manifest_path = manifest_path
        self.manifest = manifest
        self.piece_paths = piece_paths
        self._connection = connection
        self._closed = False

    @property
    def index_path(self) -> Path:
        if len(self.piece_paths) != 1:
            raise ProjectionIntegrityError("PB-B can query exactly one physical piece")
        return self.piece_paths[0]

    def fixture(self, identity: str) -> dict[str, object] | None:
        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        envelope = self._connection.execute(
            "SELECT * FROM artifact_envelopes WHERE identity = ? AND kind = 'fixture'",
            (identity,),
        ).fetchone()
        if envelope is None:
            return None
        facts = self._connection.execute(
            "SELECT schema_id,schema_version,fact_key,value_json FROM semantic_facts "
            "WHERE subject_identity = ? ORDER BY schema_id,schema_version,fact_key",
            (identity,),
        ).fetchall()
        return {
            "envelope": dict(envelope),
            "facts": [
                {
                    "schema_id": row["schema_id"],
                    "schema_version": row["schema_version"],
                    "fact_key": row["fact_key"],
                    "value": json.loads(row["value_json"]),
                }
                for row in facts
            ],
        }

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "ProjectionHandle":
        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def bind_projection(
    manifest_path: Path,
    *,
    expected: AcceptedProjectionCoordinate,
) -> ProjectionHandle:
    """Verify one complete manifest and return a handle that does no per-read rehash."""

    manifest = load_projection_manifest(manifest_path)
    if not _manifest_matches_coordinate(manifest, expected):
        raise ProjectionIntegrityError("projection manifest differs from the accepted coordinate")
    if len(manifest.pieces) != 1:
        raise ProjectionIntegrityError("PB-B supports one-piece serving only")

    pieces: list[Path] = []
    for piece in manifest.pieces:
        path = manifest_path.parent / piece.name
        if path.is_symlink() or not path.is_file():
            raise ProjectionIntegrityError(f"projection piece is missing: {piece.name}")
        resolved = path.resolve(strict=True)
        if resolved.parent != manifest_path.parent.resolve(strict=True):
            raise ProjectionIntegrityError("projection piece escapes its publication directory")
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != piece.byte_length:
            raise ProjectionIntegrityError(f"projection piece size mismatch: {piece.name}")
        if physical_file_digest(resolved).tagged != piece.physical_digest:
            raise ProjectionIntegrityError(f"projection piece digest mismatch: {piece.name}")
        pieces.append(resolved)

    index_path = pieces[0]
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{index_path.as_uri()}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        _verify_projection_schema(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        metadata_row = connection.execute(
            "SELECT instance_id,git_object_format,git_oid,semantic_root,generation_root "
            "FROM generation_metadata WHERE singleton = 1"
        ).fetchone()
        compiler_row = connection.execute(
            "SELECT implementation,schema_version,compiler_digest "
            "FROM compiler_coordinates WHERE singleton = 1"
        ).fetchone()
        counts = {
            spec.name: cast(
                int,
                connection.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0],
            )
            for spec in _TABLE_SPECS
        }
        expected_metadata = (
            manifest.instance_id,
            manifest.git_object_format,
            manifest.git_oid,
            manifest.semantic_root,
            manifest.generation_root,
        )
        expected_compiler = (
            expected.compiler.implementation,
            expected.compiler.schema_version,
            expected.compiler.rule_digest,
        )
        integrity_value = tuple(integrity) if integrity is not None else None
        binding_metadata = tuple(metadata_row) if metadata_row is not None else None
        compiler = tuple(compiler_row) if compiler_row is not None else None
        if (
            integrity_value != ("ok",)
            or binding_metadata != expected_metadata
            or compiler != expected_compiler
        ):
            raise ProjectionIntegrityError("projection internal binding metadata is inconsistent")
        sorted_counts = dict(sorted(counts.items(), key=lambda item: item[0].encode("utf-8")))
        if sorted_counts != manifest.row_counts:
            raise ProjectionIntegrityError("projection row counts differ from the manifest")
        if projection_logical_digest(index_path).tagged != manifest.logical_digest:
            raise ProjectionIntegrityError("projection canonical logical digest mismatch")
        return ProjectionHandle(
            manifest_path=manifest_path.resolve(strict=True),
            manifest=manifest,
            piece_paths=tuple(pieces),
            connection=connection,
        )
    except sqlite3.DatabaseError as exc:
        if connection is not None:
            connection.close()
        raise ProjectionIntegrityError(
            "projection piece is not a valid PB-B SQLite database"
        ) from exc
    except BaseException:
        if connection is not None:
            connection.close()
        raise


def detect_projection_orphans(publication_directory: Path) -> tuple[ProjectionOrphan, ...]:
    """Report deterministic cleanup candidates without deleting any bytes."""

    if not publication_directory.is_dir():
        raise ProjectionIntegrityError("projection publication directory is absent")
    orphans: list[ProjectionOrphan] = []
    referenced: set[str] = set()
    manifests = sorted(
        (path for path in publication_directory.iterdir() if _MANIFEST_RE.fullmatch(path.name)),
        key=lambda path: path.name.encode("utf-8"),
    )
    for path in manifests:
        try:
            manifest = load_projection_manifest(path)
        except ProjectionIntegrityError as exc:
            orphans.append(
                ProjectionOrphan(kind="malformed-manifest", path=str(path), detail=str(exc))
            )
            continue
        for piece in manifest.pieces:
            referenced.add(piece.name)
            piece_path = publication_directory / piece.name
            if not piece_path.is_file() or piece_path.is_symlink():
                orphans.append(
                    ProjectionOrphan(
                        kind="missing-piece",
                        path=str(piece_path),
                        detail=f"referenced by {path.name}",
                    )
                )
    for path in sorted(publication_directory.iterdir(), key=lambda item: item.name.encode("utf-8")):
        if path.name.startswith(".stage-"):
            orphans.append(
                ProjectionOrphan(
                    kind="staging-build",
                    path=str(path),
                    detail="private staging output was not fully retired",
                )
            )
        elif _PIECE_RE.fullmatch(path.name) and path.name not in referenced:
            orphans.append(
                ProjectionOrphan(
                    kind="unreferenced-piece",
                    path=str(path),
                    detail="immutable piece is not referenced by a valid manifest",
                )
            )
    return tuple(
        sorted(
            orphans,
            key=lambda item: (item.kind.encode("utf-8"), item.path.encode("utf-8")),
        )
    )


__all__ = [
    "ProjectionHandle",
    "bind_projection",
    "canonical_logical_export",
    "detect_projection_orphans",
    "initialize_projection_database",
    "load_projection_manifest",
    "physical_file_digest",
    "projection_logical_digest",
]
