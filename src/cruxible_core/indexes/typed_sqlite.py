"""Version-two physical publication mechanics for the shared typed directory."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from cruxible_client.contracts.canonical import ArtifactCodec, file_digest, is_candidate_card_path
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_client.contracts.projection_extensions import ProjectionExtensionRegistry
from cruxible_core.compiler.projection_artifacts import ParsedProjectionTree
from cruxible_core.indexes.evidence.citation_sql import (
    SCHEMA_SQL,
    populate_citations,
    remove_owner_citations,
)
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


def cold_claim_digest_resolver(
    sources: Mapping[str, bytes],
    *,
    repository: Any,
    head_oid: str,
    codec: ArtifactCodec,
    coordinates: Mapping[int, Any],
) -> Callable[[str], tuple[str, ...]]:
    """Resolve full-rebuild inputs from exact retained occurrences, even if deleted.

    The served/delta path supplies C's cutoff-bound lookup. This source-only
    oracle deliberately pays historical traversal and holds no lineage index.
    Legacy file commitments are checked as files before parsing their original
    Claim format; they are never reinterpreted as artifact commitments.
    """
    from cruxible_client.contracts.claims import claim_artifact_digest, parse_claim
    from cruxible_core.proposals.settlement import parse_change_set_record

    records = tuple(
        sorted(
            (
                parse_change_set_record(content, path=path)
                for path, content in sources.items()
                if path.startswith("changesets/")
            ),
            key=lambda record: record.sequence,
        )
    )
    latest = records[-1].sequence if records else 0

    def resolve(digest: str) -> tuple[str, ...]:
        identities: set[str] = set()
        for record in records:
            for member in record.members:
                if member.artifact_kind != "claim":
                    continue
                exact = getattr(member, "candidate_artifact_digest", None)
                legacy = getattr(member, "artifact_digest", None)
                if exact != digest and (exact is not None or legacy is None):
                    continue
                coordinate = coordinates.get(record.sequence)
                oid = None if coordinate is None else coordinate.git_oid
                if oid is None:
                    oid = head_oid
                    for _ in range(latest - record.sequence):
                        oid = repository.parent_of(oid)
                content = repository.blob_at(oid, member.path)
                if content is None:
                    raise ProjectionIntegrityError("historical Claim occurrence source is absent")
                if exact is None and file_digest(content).tagged != legacy:
                    raise ProjectionIntegrityError(
                        "legacy Claim occurrence input differs from retained commitment"
                    )
                claim = parse_claim(content, path=member.path, codec=codec)
                actual = claim_artifact_digest(claim).tagged
                if exact is not None and actual != exact:
                    raise ProjectionIntegrityError(
                        "historical Claim occurrence differs from retained artifact commitment"
                    )
                if actual == digest:
                    identities.add(claim.identity.qualified)
        return tuple(sorted(identities))

    return resolve


def parse_static_owners(sources: Mapping[str, bytes], *, accepted: Any) -> ParsedProjectionTree:
    """Parse exact owner contracts without making CAS availability an index authority."""
    from dataclasses import replace

    from cruxible_client.contracts.documents import document_digest, parse_document
    from cruxible_core.compiler.compiler import (
        artifact_codec_for_compiler,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.compiler.projection_artifacts import (
        ArtifactEnvelopeRow,
        FixtureArtifact,
        PinRow,
        parse_projection_tree,
    )

    kinds = artifact_kinds_for_compiler(accepted.compiler)
    codec = artifact_codec_for_compiler(accepted.compiler)
    source_kinds = {
        path: None if is_candidate_card_path(path) else kinds.resolve_path(path) for path in sources
    }
    documents = {path: body for path, body in sources.items() if source_kinds[path] == "document"}
    fixtures = {path: body for path, body in sources.items() if source_kinds[path] == "fixture"}
    parsed = parse_projection_tree(
        {
            path: body
            for path, body in sources.items()
            if path not in documents
            and path not in fixtures
            and source_kinds[path] != "presentation"
        },
        registry=projection_registry_for_compiler(accepted.compiler),
        artifact_kinds=kinds,
        artifact_codec=codec,
    )
    envelopes = list(parsed.envelopes)
    pins = {(pin.source_identity, pin.target_identity): pin for pin in parsed.pins}
    for path, content in documents.items():
        document = parse_document(content, path=path, codec=codec)
        envelopes.append(
            ArtifactEnvelopeRow(
                document.identity,
                document.kind,
                document.tag,
                path,
                document_digest(document).tagged,
                document.predecessor_digest,
                document.lifecycle.revision,
            )
        )
        for pin in document.pins:
            key = (document.identity, pin.target_identity)
            previous = pins.get(key)
            if previous is not None and previous.target_digest != pin.target_digest:
                raise ProjectionIntegrityError(
                    "one artifact pins the same dependency identity at conflicting digests"
                )
            pins[key] = PinRow(document.identity, pin.target_identity, pin.target_digest)
    # Extension declarations govern produced facts, not the static fixture owner.
    # Read its accepted source contract without substituting the default registry.
    for path, content in fixtures.items():
        fixture = FixtureArtifact.model_validate_json(content)
        envelopes.append(
            ArtifactEnvelopeRow(
                fixture.artifact_id,
                fixture.kind,
                fixture.tag,
                path,
                file_digest(content).tagged,
                fixture.predecessor_digest,
                fixture.revision,
            )
        )
        for fixture_pin in fixture.pins:
            key = (fixture.artifact_id, fixture_pin.target_identity)
            previous = pins.get(key)
            if previous is not None and previous.target_digest != fixture_pin.target_digest:
                raise ProjectionIntegrityError(
                    "one artifact pins the same dependency identity at conflicting digests"
                )
            pins[key] = PinRow(
                fixture.artifact_id, fixture_pin.target_identity, fixture_pin.target_digest
            )
    return replace(
        parsed,
        envelopes=tuple(sorted(envelopes, key=lambda row: row.identity)),
        pins=tuple(pins[key] for key in sorted(pins)),
    )


def authenticate_source_rows(projection: Any, *, repository: Any) -> None:
    """Cold completeness proof for static owner selection; requires no live CAS.

    Covers all member commitments, every registered typed owner field, principal
    rows and both pin kinds. Citation envelope availability and presentation
    outputs are evaluated under their own live/source rules, outside this proof.
    """
    from cruxible_core.compiler.compiler import (
        artifact_codec_for_compiler,
        artifact_kinds_for_compiler,
    )
    from cruxible_core.compiler.projection_tree import TreeReadLimits, read_registered_tree

    accepted = projection.accepted
    sources = {
        blob.path: blob.content
        for blob in read_registered_tree(
            repository,
            accepted.git_oid,
            limits=TreeReadLimits(),
            artifact_kinds=artifact_kinds_for_compiler(accepted.compiler),
        )
    }
    codec = artifact_codec_for_compiler(accepted.compiler)
    parsed = parse_static_owners(sources, accepted=accepted)
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(schema_sql())
        insert_members(expected, sources, accepted.git_object_format)
        insert_owners(
            expected,
            parsed=parsed,
            blobs=sources,
            codec=codec,
            resolve_digest=cold_claim_digest_resolver(
                sources,
                repository=repository,
                head_oid=accepted.git_oid,
                codec=codec,
                coordinates={},
            ),
        )
        for table in ("members", "principals", "pins", *(owner.table for owner in OWNER_CODECS)):
            info = expected.execute(f"PRAGMA table_info({table})").fetchall()
            keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            sql = f"SELECT * FROM {table} ORDER BY {','.join(keys)}"
            if [
                tuple(row)
                for row in projection._connection.execute(
                    sql.replace(f"FROM {table}", f"FROM main.{table}")
                )
            ] != expected.execute(sql).fetchall():
                raise ProjectionIntegrityError(
                    f"typed projection {table} rows differ from accepted source"
                )
    finally:
        expected.close()


def complete_schema_sql() -> str:
    specs = [spec for spec in _TABLE_SPECS if spec.name in (*_METADATA_TABLES, *_EXTENSION_TABLES)]
    statements = [schema_sql(), SCHEMA_SQL]
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
    bodies: Any = None,
) -> None:
    changed = tuple(sorted(set(changed_paths)))
    for path in changed:
        for identity, kind in connection.execute(
            "SELECT identity,kind FROM artifact_lookup WHERE path=?", (path,)
        ).fetchall():
            if kind == "claim":
                remove_owner_citations(connection, (("Claim", identity),))
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
    if bodies is not None:
        populate_citations(
            connection,
            sources,
            bodies=bodies,
            owner_exists=lambda kind, identity: (
                kind == "Claim"
                and connection.execute(
                    "SELECT 1 FROM claims WHERE identity=?", (identity,)
                ).fetchone()
                is not None
            ),
            artifact_codec=codec,
        )
    for fact in parsed.semantic_facts:
        if fact.schema_id not in ("playbill.procedure.track_record", "playbill.line.track_record"):
            continue
        value = fact.value
        assert isinstance(value, dict)
        digest = value["promotion_digest"]["$digest"]
        owners = connection.execute(
            "SELECT identity FROM exhaust_promotions WHERE artifact_digest=?", (digest,)
        ).fetchall()
        if len(owners) != 1:
            raise ProjectionIntegrityError("promoted result has no exact unique typed owner")
        connection.execute(
            "INSERT INTO promotion_subjects VALUES (?,?,?,?)",
            (owners[0][0], fact.subject_identity, fact.schema_id.split(".")[1], fact.fact_key),
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
    bodies: Any = None,
    registry: ProjectionExtensionRegistry | None = None,
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
            bodies=bodies,
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
        from cruxible_client.contracts.canonical import canonical_bytes

        fixture_ids = {row.identity for row in parsed.envelopes if row.kind == "fixture"}
        for table, facts, declarations in (
            (
                "semantic_facts",
                tuple(f for f in parsed.semantic_facts if f.subject_identity in fixture_ids),
                "projection_fact_schemas",
            ),
            ("presentation_facts", parsed.presentation_facts, "presentation_fact_schemas"),
        ):
            connection.executemany(
                f"INSERT INTO {table} VALUES (?,?,?,?,?)",
                [
                    (
                        fact.schema_id,
                        fact.schema_version,
                        fact.subject_identity,
                        fact.fact_key,
                        canonical_bytes(fact.value).decode(),
                    )
                    for fact in facts
                ],
            )
            if registry is not None:
                keys = {(fact.schema_id, fact.schema_version) for fact in facts}
                selected = registry.declarations(
                    "semantic" if table == "semantic_facts" else "presentation"
                )
                connection.executemany(
                    f"INSERT INTO {declarations} VALUES (?,?,?)",
                    [
                        (
                            declaration.schema_id,
                            declaration.schema_version,
                            canonical_bytes(list(declaration.constraints)).decode(),
                        )
                        for declaration in selected
                        if (declaration.schema_id, declaration.schema_version) in keys
                    ],
                )
        connection.commit()
        verify_schema(connection)
        return row_counts(connection)
    finally:
        connection.close()


def update(
    path: Path,
    *,
    parent: sqlite3.Connection,
    request: AssemblerRequest,
    parsed: ParsedProjectionTree,
    sources: Mapping[str, bytes],
    changed_paths: Iterable[str],
    codec: ArtifactCodec,
    bodies: Any = None,
    resolve_digest: Callable[[str], Iterable[str]] | None = None,
) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        parent.backup(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        replace_rows(
            connection,
            request=request,
            parsed=parsed,
            sources=sources,
            changed_paths=changed_paths,
            codec=codec,
            bodies=bodies,
            resolve_digest=resolve_digest,
        )
        connection.execute(
            "UPDATE generation_metadata SET instance_id=?,git_object_format=?,git_oid=?,semantic_root=?,generation_root=? WHERE singleton=1",
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
