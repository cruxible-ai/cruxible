"""Version-two physical publication mechanics for the shared typed directory."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from cruxible_client.contracts.canonical import ArtifactCodec, file_digest, is_candidate_card_path
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.compiler.projection_artifacts import ParsedProjectionTree
from cruxible_core.indexes.projection import AssemblerRequest
from cruxible_core.indexes.sqlite_v1 import _TABLE_SPECS
from cruxible_core.indexes.typed_state import (
    OWNER_CODECS,
    insert_owners,
    principal_registry,
    schema_sql,
)

_METADATA_TABLES = ("compiler_coordinates", "generation_metadata", "assembler_metadata")
# Extensible fixture output remains a true extension boundary, not a duplicate
# copy of the built-in records now owned by typed tables and exact Git sources.
_EXTENSION_TABLES = (
    "semantic_facts",
    "projection_fact_schemas",
    "presentation_facts",
    "presentation_fact_schemas",
)


def complete_schema_sql() -> str:
    specs = [spec for spec in _TABLE_SPECS if spec.name in (*_METADATA_TABLES, *_EXTENSION_TABLES)]
    statements = [schema_sql()]
    for spec in specs:
        statements.append(spec.create_sql + ";")
        statements.extend(
            f"CREATE INDEX {name} ON {spec.name} ({columns});" for name, columns in spec.indexes
        )
    return "\n".join(statements)


def schema_objects(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]


def verify_schema(connection: sqlite3.Connection) -> None:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(complete_schema_sql())
        if schema_objects(connection) != schema_objects(reference):
            raise ProjectionIntegrityError(
                "typed projection SQLite schema differs from its registry"
            )
    finally:
        reference.close()


def logical_export(connection: sqlite3.Connection) -> dict[str, object]:
    verify_schema(connection)
    tables = []
    for object_type, name, _table, sql in schema_objects(connection):
        if object_type != "table" or name in ("generation_metadata", "assembler_metadata"):
            continue
        info = connection.execute(f"PRAGMA table_info({name})").fetchall()
        keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        rows = connection.execute(f"SELECT * FROM {name} ORDER BY {','.join(keys)}").fetchall()
        tables.append({"name": name, "sql": sql, "rows": [list(row) for row in rows]})
    return {
        "storage_schema_version": 2,
        "schema": [list(row) for row in schema_objects(connection)],
        "tables": tables,
    }


def row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        str(name): connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    }


def insert_members(
    connection: sqlite3.Connection, blobs: Mapping[str, bytes], object_format: str
) -> None:
    for path, content in sorted(blobs.items()):
        if is_candidate_card_path(path):
            continue
        digest = hashlib.new(
            object_format, b"blob " + str(len(content)).encode("ascii") + b"\0" + content
        ).hexdigest()
        connection.execute(
            "INSERT INTO members VALUES (?,?,?,?)",
            (path, digest, file_digest(content).tagged, len(content)),
        )


def replace_rows(
    connection: sqlite3.Connection,
    *,
    request: AssemblerRequest,
    parsed: ParsedProjectionTree,
    sources: Mapping[str, bytes],
    codec: ArtifactCodec,
    changed_paths: Iterable[str] = (),
    resolve_digest: Callable[[str], Iterable[str]] | None = None,
) -> None:
    changed = tuple(sorted(set(changed_paths)))
    for path in changed:
        for identity, kind in connection.execute(
            "SELECT identity,kind FROM artifact_lookup WHERE path=?", (path,)
        ).fetchall():
            connection.execute("DELETE FROM pins WHERE source_identity=?", (identity,))
            if kind == "exhaust-promotion":
                connection.execute(
                    "DELETE FROM promotion_subjects WHERE promotion_identity=?", (identity,)
                )
        for owner in OWNER_CODECS:
            connection.execute(f"DELETE FROM {owner.table} WHERE path=?", (path,))
        connection.execute("DELETE FROM principals WHERE path=?", (path,))
        connection.execute("DELETE FROM members WHERE path=?", (path,))
    insert_members(connection, sources, request.git_object_format)
    insert_owners(
        connection, parsed=parsed, blobs=sources, codec=codec, resolve_digest=resolve_digest
    )
    # Full registry validation is O(principals), explicitly distinct from indexed
    # active-actor checks. This publication is a derivative, never a trust root.
    if connection.execute("SELECT 1 FROM principals LIMIT 1").fetchone():
        principal_registry(connection, request.semantic_root)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ProjectionIntegrityError("typed projection has invalid ownership references")


def initialize(
    path: Path,
    *,
    request: AssemblerRequest,
    parsed: ParsedProjectionTree,
    sources: Mapping[str, bytes],
    codec: ArtifactCodec,
    assembler_implementation: str,
    resolve_digest: Callable[[str], Iterable[str]] | None = None,
) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA user_version=2")
        connection.executescript(complete_schema_sql())
        replace_rows(
            connection,
            request=request,
            parsed=parsed,
            sources=sources,
            codec=codec,
            resolve_digest=resolve_digest,
        )
        connection.execute(
            "INSERT INTO compiler_coordinates VALUES (1,?,?)",
            (request.schema_version, request.compiler_digest),
        )
        connection.execute(
            "INSERT INTO assembler_metadata VALUES (1,?,?)",
            (assembler_implementation, request.contract_version),
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
        connection.commit()
        verify_schema(connection)
        return row_counts(connection)
    finally:
        connection.close()
