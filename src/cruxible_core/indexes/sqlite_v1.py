"""Frozen storage-v1 schema; retained logical exports must use these exact specs."""

from dataclasses import dataclass


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
            "schema_version INTEGER NOT NULL, compiler_digest TEXT NOT NULL) STRICT"
        ),
        columns=(
            ("singleton", "INTEGER", False),
            ("schema_version", "INTEGER", False),
            ("compiler_digest", "TEXT", False),
        ),
        primary_key=("singleton",),
        constraints=("check(singleton=1)",),
        indexes=(),
        logical=True,
    ),
    _TableSpec(
        name="assembler_metadata",
        create_sql=(
            "CREATE TABLE assembler_metadata (singleton INTEGER PRIMARY KEY "
            "CHECK(singleton = 1), implementation TEXT NOT NULL, "
            "contract_version INTEGER NOT NULL) STRICT"
        ),
        columns=(
            ("singleton", "INTEGER", False),
            ("implementation", "TEXT", False),
            ("contract_version", "INTEGER", False),
        ),
        primary_key=("singleton",),
        constraints=("check(singleton=1)",),
        indexes=(),
        logical=False,
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
