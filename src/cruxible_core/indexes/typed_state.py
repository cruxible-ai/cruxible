"""Typed accepted-state directory; exact source bodies remain in governed Git.

The registry maps registered kinds to their typed owners, including Document
identities and the two singleton exceptions. Source bytes and their original
artifact digest rules remain authoritative.
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from cruxible_client.contracts.accepted_attestations import parse_accepted_attestation
from cruxible_client.contracts.acquisition_policies import parse_acquisition_policy
from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_IDENTITY,
    APPROVAL_POLICY_PATH,
    approval_policy_digest,
    parse_approval_policy,
)
from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.canonical import (
    ArtifactCodec,
    artifact_path_for_codec,
)
from cruxible_client.contracts.captures import parse_capture_contract
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationV2,
    claim_attestation_v2_envelope_digest,
    claim_attestation_v2_statement_digest,
)
from cruxible_client.contracts.claim_types import ClaimType, parse_claim_type
from cruxible_client.contracts.claims import claim_statement_digest, parse_claim
from cruxible_client.contracts.documents import parse_document
from cruxible_client.contracts.errors import PrincipalIntegrityError, ProjectionIntegrityError
from cruxible_client.contracts.principals import PrincipalRegistrySnapshot
from cruxible_client.contracts.procedure_mandates import parse_procedure_mandate
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_IDENTITY,
    PROCEDURE_RUNTIME_POLICY_PATH,
    parse_procedure_runtime_policy,
    procedure_runtime_policy_digest,
)
from cruxible_client.contracts.procedures.artifacts import parse_procedure
from cruxible_client.contracts.procedures.line_specs import line_identity_digest, parse_line_spec
from cruxible_client.contracts.provider_interfaces import parse_provider_interface
from cruxible_client.contracts.providers import parse_provider
from cruxible_client.contracts.query.definitions import parse_query_definition
from cruxible_client.contracts.resolution_contracts import parse_resolution_contract
from cruxible_client.contracts.standing_mandates import parse_standing_mandate
from cruxible_client.contracts.subjects import parse_subject
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.compiler.projection_artifacts import (
    ArtifactEnvelopeRow,
    ParsedProjectionTree,
)
from cruxible_core.exhaust.promotions import parse_exhaust_promotion

SQLValue = str | int | None

if TYPE_CHECKING:
    from cruxible_core.claims.closure import ArtifactDependencyStateV1


def utc_microseconds(value: datetime | None) -> int | None:
    if value is None:
        return None
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


@dataclass(frozen=True)
class OwnerCodec:
    kind: str
    identity_kind: str | None
    table: str
    parse: Callable[..., Any]
    fields: tuple[tuple[str, str], ...] = ()
    lifecycle: bool = True
    versioned: bool = True


# Field names intentionally follow the source contracts; nested executable and
# policy content is not duplicated as SQL JSON or speculative child relations.
OWNER_CODECS = (
    OwnerCodec(
        "resolution-contract",
        "ResolutionContract",
        "resolution_contracts",
        parse_resolution_contract,
        tuple(
            (name, "TEXT NOT NULL")
            for name in (
                "hypothesis_identity",
                "hypothesis_artifact_digest",
                "hypothesis_statement_digest",
                "hypothesis_git_oid",
                "hypothesis_semantic_root",
                "hypothesis_generation_root",
                "hypothesis_compiler_digest",
                "rule_kind",
            )
        ),
    ),
    OwnerCodec(
        "attestation",
        "ClaimAttestation",
        "attestations",
        parse_accepted_attestation,
        (
            ("envelope_digest", "TEXT PRIMARY KEY NOT NULL"),
            ("statement_digest", "TEXT NOT NULL"),
            ("claim_identity", "TEXT NOT NULL"),
            ("claim_artifact_digest", "TEXT NOT NULL"),
            ("claim_statement_digest", "TEXT NOT NULL"),
            ("principal_id", "TEXT NOT NULL"),
            ("signing_key_digest", "TEXT NOT NULL"),
            ("basis", "TEXT NOT NULL CHECK(basis IN ('examined_existing','new_capture'))"),
            ("stance", "TEXT NOT NULL CHECK(stance IN ('support','contradict','unsure'))"),
            ("attested_at_us", "INTEGER NOT NULL"),
            ("valid_until_us", "INTEGER"),
            ("subject_shell_digest", "TEXT NOT NULL"),
            ("object_shell_digest", "TEXT"),
            ("referent_git_oid", "TEXT NOT NULL"),
            ("referent_semantic_root", "TEXT NOT NULL"),
            ("referent_generation_root", "TEXT NOT NULL"),
            ("referent_compiler_digest", "TEXT NOT NULL"),
        ),
        lifecycle=False,
        versioned=False,
    ),
    OwnerCodec(
        "claim",
        "Claim",
        "claims",
        parse_claim,
        (
            ("statement_digest", "TEXT NOT NULL"),
            ("claim_type_identity", "TEXT NOT NULL"),
            ("claim_type_digest", "TEXT NOT NULL"),
            ("subject_path", "TEXT NOT NULL"),
            ("subject_selector_scheme", "TEXT NOT NULL"),
            ("subject_selector_value", "TEXT NOT NULL"),
            ("predicate", "TEXT NOT NULL"),
            ("qualifier", "TEXT"),
            ("role", "TEXT NOT NULL"),
            ("object_kind", "TEXT NOT NULL"),
            ("object_path", "TEXT"),
            ("object_selector_scheme", "TEXT"),
            ("object_selector_value", "TEXT"),
            ("object_content_digest", "TEXT"),
            ("object_span_start_text", "TEXT"),
            ("object_span_end_text", "TEXT"),
            ("literal_type", "TEXT"),
            ("literal_text", "TEXT"),
            ("literal_boolean", "INTEGER CHECK(literal_boolean IN (0,1))"),
            ("literal_integer_text", "TEXT"),
            ("effective_from_us", "INTEGER"),
            ("effective_until_us", "INTEGER"),
            ("shell_context_digest", "TEXT"),
        ),
    ),
    OwnerCodec(
        "procedure",
        "Procedure",
        "procedures",
        parse_procedure,
        (
            ("definition_digest", "TEXT NOT NULL"),
            ("definition_format", "TEXT NOT NULL"),
            (
                "activation_policy",
                "TEXT NOT NULL CHECK(activation_policy IN ('drain','abort','snapshot','epoch-check'))",
            ),
            ("directly_runnable", "INTEGER NOT NULL CHECK(directly_runnable IN (0,1))"),
        ),
    ),
    OwnerCodec(
        "line",
        "Line",
        "lines",
        parse_line_spec,
        (
            ("identity_digest", "TEXT NOT NULL"),
            ("occurrence_epoch", "INTEGER NOT NULL CHECK(occurrence_epoch>=1)"),
            ("procedure_identity", "TEXT NOT NULL"),
            ("procedure_digest", "TEXT NOT NULL"),
            (
                "requested_terminal_rung",
                "INTEGER NOT NULL CHECK(requested_terminal_rung IN (1,2,3))",
            ),
        ),
    ),
    OwnerCodec(
        "subject",
        "Subject",
        "subjects",
        parse_subject,
        (
            ("subject_kind", "TEXT NOT NULL"),
            ("subject_id", "TEXT NOT NULL"),
        ),
    ),
    OwnerCodec(
        "document",
        "Document",
        "documents",
        parse_document,
        (
            ("document_kind", "TEXT NOT NULL"),
            ("media_type", "TEXT NOT NULL"),
            ("title", "TEXT NOT NULL"),
            ("body_digest", "TEXT NOT NULL"),
        ),
    ),
    OwnerCodec(
        "claim-type",
        "ClaimType",
        "claim_types",
        parse_claim_type,
        (
            ("predicate", "TEXT NOT NULL"),
            ("object_kind", "TEXT NOT NULL"),
            ("cardinality", "TEXT NOT NULL"),
            ("referent_sensitivity", "TEXT NOT NULL"),
        ),
    ),
    OwnerCodec(
        "provider", "Provider", "providers", parse_provider, (("control_domain", "TEXT NOT NULL"),)
    ),
    OwnerCodec(
        "provider-interface",
        "ProviderInterface",
        "provider_interfaces",
        parse_provider_interface,
        (
            ("interface_id", "TEXT NOT NULL"),
            ("interface_digest", "TEXT NOT NULL"),
            ("effect_class", "TEXT NOT NULL"),
        ),
    ),
    OwnerCodec(
        "source-acquisition-policy",
        "SourceAcquisitionPolicy",
        "source_acquisition_policies",
        parse_acquisition_policy,
    ),
    OwnerCodec(
        "standing-mandate",
        "StandingMandate",
        "standing_mandates",
        parse_standing_mandate,
        (
            ("provider_identity", "TEXT NOT NULL"),
            ("capture_contract_digest", "TEXT NOT NULL"),
            ("valid_from_us", "INTEGER NOT NULL"),
            ("valid_until_us", "INTEGER NOT NULL"),
        ),
    ),
    OwnerCodec(
        "procedure-mandate",
        "ProcedureMandate",
        "procedure_mandates",
        parse_procedure_mandate,
        (
            ("procedure_identity", "TEXT NOT NULL"),
            ("procedure_digest", "TEXT NOT NULL"),
            ("rung", "INTEGER NOT NULL CHECK(rung IN (2,3))"),
            ("valid_from_us", "INTEGER NOT NULL"),
            ("expires_at_us", "INTEGER NOT NULL"),
        ),
    ),
    OwnerCodec("query-definition", "QueryDefinition", "query_definitions", parse_query_definition),
    OwnerCodec(
        "capture-contract",
        "CaptureContract",
        "capture_contracts",
        parse_capture_contract,
        (
            ("epistemic_grade", "TEXT NOT NULL"),
            ("replay_policy_digest", "TEXT NOT NULL"),
            ("provenance_rule_digest", "TEXT NOT NULL"),
        ),
    ),
    OwnerCodec(
        "exhaust-promotion",
        "ExhaustPromotion",
        "exhaust_promotions",
        parse_exhaust_promotion,
        (
            ("output_digest", "TEXT NOT NULL"),
            ("reducer_digest", "TEXT NOT NULL"),
            ("receipt_set_manifest_digest", "TEXT NOT NULL"),
        ),
    ),
)
OWNER_BY_KIND = {owner.kind: owner for owner in OWNER_CODECS}
OWNER_BY_IDENTITY_KIND = {
    owner.identity_kind: owner for owner in OWNER_CODECS if owner.identity_kind
}
SINGLETONS = {
    APPROVAL_POLICY_IDENTITY: ("approval-policy", APPROVAL_POLICY_PATH, parse_approval_policy),
    PROCEDURE_RUNTIME_POLICY_IDENTITY: (
        "procedure-runtime-policy",
        PROCEDURE_RUNTIME_POLICY_PATH,
        parse_procedure_runtime_policy,
    ),
}
COMMON_COLUMNS = "identity,kind,format_tag,path,artifact_digest,predecessor_digest,revision"


def owner_for_identity(identity: str) -> OwnerCodec | None:
    """Use the canonical identity codec, never a file or string-prefix heuristic."""
    try:
        return OWNER_BY_IDENTITY_KIND.get(parse_artifact_identity(identity).kind)
    except ValueError:
        # Document identities are resolved against indexed
        # UNION branches. Their strings are not assigned a fabricated modern kind.
        return None


_CLAIM_CHECK = """
CHECK(object_kind IN ('literal','subject','exact_content')),
CHECK((object_kind='subject' AND object_path IS NOT NULL AND object_selector_scheme IS NOT NULL
       AND object_selector_value IS NOT NULL AND object_content_digest IS NULL
       AND object_span_start_text IS NULL AND object_span_end_text IS NULL AND literal_type IS NULL)
   OR (object_kind='exact_content' AND object_path IS NULL AND object_selector_scheme IS NULL
       AND object_selector_value IS NULL AND object_content_digest IS NOT NULL
       AND ((object_span_start_text IS NULL AND object_span_end_text IS NULL)
         OR (object_span_start_text IS NOT NULL AND object_span_end_text IS NOT NULL))
       AND literal_type IS NULL)
   OR (object_kind='literal' AND object_path IS NULL AND object_selector_scheme IS NULL
       AND object_selector_value IS NULL AND object_content_digest IS NULL
       AND object_span_start_text IS NULL AND object_span_end_text IS NULL
       AND literal_type IS NOT NULL
       AND literal_type IN ('null','boolean','integer','string','array','object'))),
CHECK((literal_type='string' AND literal_text IS NOT NULL AND literal_boolean IS NULL AND literal_integer_text IS NULL)
   OR (literal_type='boolean' AND literal_text IS NULL AND literal_boolean IS NOT NULL AND literal_integer_text IS NULL)
   OR (literal_type='integer' AND literal_text IS NULL AND literal_boolean IS NULL AND literal_integer_text IS NOT NULL)
   OR ((literal_type IS NULL OR literal_type IN ('null','array','object'))
       AND literal_text IS NULL AND literal_boolean IS NULL AND literal_integer_text IS NULL))
"""


def schema_sql() -> str:
    statements = [
        """CREATE TABLE members (
        path TEXT PRIMARY KEY NOT NULL, git_blob_oid TEXT NOT NULL,
        file_digest TEXT NOT NULL, byte_length INTEGER NOT NULL CHECK(byte_length>=0)
    ) STRICT"""
    ]
    branches = []
    for owner in OWNER_CODECS:
        columns = [
            "identity TEXT PRIMARY KEY NOT NULL",
            "path TEXT NOT NULL UNIQUE REFERENCES members(path)",
            "format_tag TEXT NOT NULL",
            "artifact_digest TEXT NOT NULL",
        ]
        if owner.versioned:
            columns.extend(
                ("predecessor_digest TEXT", "revision INTEGER NOT NULL CHECK(revision>=1)")
            )
        if owner.lifecycle:
            values = "'active'" if owner.kind == "document" else "'live','retired'"
            columns.append(f"lifecycle TEXT NOT NULL CHECK(lifecycle IN ({values}))")
        if owner.kind == "attestation":
            columns[0] = "identity TEXT NOT NULL UNIQUE"
        columns.extend(f"{name} {definition}" for name, definition in owner.fields)
        if owner.kind == "claim":
            columns.append(_CLAIM_CHECK)
        statements.append(f"CREATE TABLE {owner.table} ({','.join(columns)}) STRICT")
        statements.append(
            f"CREATE INDEX {owner.table}_by_digest ON {owner.table}(artifact_digest,identity)"
        )
        lifecycle = "lifecycle" if owner.lifecycle else "NULL AS lifecycle"
        version = (
            "predecessor_digest,revision"
            if owner.versioned
            else "NULL AS predecessor_digest,1 AS revision"
        )
        branches.append(
            f"SELECT identity,'{owner.kind}' AS kind,format_tag,path,artifact_digest,{version},{lifecycle} FROM {owner.table}"
        )
    statements.extend(
        [
            "CREATE VIEW artifact_lookup AS " + " UNION ALL ".join(branches),
            """CREATE TABLE principals (
            principal_id TEXT PRIMARY KEY NOT NULL, path TEXT NOT NULL UNIQUE REFERENCES members(path),
            format_tag TEXT NOT NULL, algorithm TEXT NOT NULL CHECK(algorithm='ed25519-v1'),
            public_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK(kind IN ('ordinary','recovery','daemon')),
            status TEXT NOT NULL CHECK(status IN ('active','revoked'))
        ) STRICT""",
            """CREATE TABLE pins (
            source_identity TEXT NOT NULL, edge_kind TEXT NOT NULL CHECK(edge_kind IN ('required_pin','consumed_claim_input')),
            ordinal INTEGER NOT NULL CHECK(ordinal>=0), target_identity TEXT, target_digest TEXT NOT NULL,
            resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','unresolved','ambiguous')),
            PRIMARY KEY(source_identity,edge_kind,ordinal),
            CHECK((resolution_status='resolved' AND target_identity IS NOT NULL)
               OR (resolution_status IN ('unresolved','ambiguous') AND target_identity IS NULL)),
            CHECK(edge_kind!='required_pin' OR resolution_status='resolved')
        ) STRICT""",
            "CREATE UNIQUE INDEX pins_required_target_unique ON pins(source_identity,target_identity) WHERE edge_kind='required_pin'",
            "CREATE INDEX pins_by_target ON pins(target_identity,edge_kind,source_identity,ordinal) WHERE target_identity IS NOT NULL",
            "CREATE INDEX pins_by_target_digest ON pins(target_digest,edge_kind,source_identity,ordinal)",
            """CREATE TABLE claim_type_names (
            source_identity TEXT NOT NULL REFERENCES claim_types(identity),
            kind TEXT NOT NULL CHECK(kind IN ('predicate','subject_kind')),
            name TEXT NOT NULL, leaf TEXT NOT NULL,
            PRIMARY KEY(source_identity,kind,name)
        ) STRICT""",
            "CREATE INDEX claim_type_names_by_name ON claim_type_names(name,kind,source_identity)",
            "CREATE INDEX claim_type_names_by_leaf ON claim_type_names(leaf,kind,name,source_identity)",
            "CREATE INDEX claims_by_subject_predicate ON claims(subject_path,predicate,subject_selector_scheme,subject_selector_value,identity)",
            "CREATE INDEX resolution_contracts_by_hypothesis_version ON resolution_contracts(hypothesis_identity,hypothesis_artifact_digest,identity)",
            "CREATE INDEX attestations_by_claim_version ON attestations(claim_identity,claim_artifact_digest,attested_at_us,envelope_digest)",
            "CREATE INDEX claims_by_lifecycle ON claims(lifecycle,identity)",
            "CREATE INDEX claims_by_object_subject ON claims(object_path,identity) WHERE object_kind='subject'",
            "CREATE INDEX provider_interfaces_by_interface ON provider_interfaces(interface_digest,identity)",
            "CREATE INDEX lines_by_identity_digest ON lines(identity_digest,identity)",
            "CREATE INDEX procedure_mandates_by_procedure ON procedure_mandates(procedure_identity,procedure_digest,valid_from_us,identity) WHERE lifecycle='live'",
            """CREATE TABLE promotion_subjects (
            promotion_identity TEXT NOT NULL REFERENCES exhaust_promotions(identity), subject_identity TEXT NOT NULL,
            record_kind TEXT NOT NULL CHECK(record_kind IN ('procedure','line')), result_key TEXT NOT NULL,
            PRIMARY KEY(promotion_identity,subject_identity,record_kind,result_key)
        ) STRICT""",
            "CREATE INDEX promotion_subjects_by_subject ON promotion_subjects(subject_identity,record_kind,promotion_identity,result_key)",
        ]
    )
    return ";\n".join(statements) + ";\n"


def _claim_fields(claim: Any) -> dict[str, SQLValue]:
    statement = claim.statement
    subject = statement.subject
    result: dict[str, SQLValue] = {
        "statement_digest": claim_statement_digest(statement).tagged,
        "claim_type_identity": statement.claim_type.qualified,
        "claim_type_digest": statement.claim_type_digest,
        "subject_path": subject.artifact_path,
        "subject_selector_scheme": subject.selector.scheme,
        "subject_selector_value": subject.selector.value,
        "predicate": statement.predicate,
        "qualifier": statement.qualifier,
        "role": statement.role,
        "object_kind": statement.object.kind,
        "effective_from_us": utc_microseconds(statement.effective_from),
        "effective_until_us": utc_microseconds(statement.effective_until),
        "shell_context_digest": statement.shell_context_digest,
    }
    obj = statement.object
    if obj.kind == "subject":
        result.update(
            object_path=obj.address.artifact_path,
            object_selector_scheme=obj.address.selector.scheme,
            object_selector_value=obj.address.selector.value,
        )
    elif obj.kind == "exact_content":
        result["object_content_digest"] = obj.content_digest
        if obj.span is not None:
            result.update(
                object_span_start_text=str(obj.span.start_byte),
                object_span_end_text=str(obj.span.end_byte),
            )
    else:
        value = obj.value
        if value is None:
            result["literal_type"] = "null"
        elif isinstance(value, bool):
            result.update(literal_type="boolean", literal_boolean=int(value))
        elif isinstance(value, int):
            result.update(literal_type="integer", literal_integer_text=str(value))
        elif isinstance(value, str):
            result.update(literal_type="string", literal_text=value)
        elif isinstance(value, list):
            result["literal_type"] = "array"
        elif isinstance(value, dict):
            result["literal_type"] = "object"
        else:
            raise ProjectionIntegrityError("Claim literal escaped its canonical typed contract")
    return result


def owner_values(owner: OwnerCodec, source: Any) -> dict[str, SQLValue]:
    if owner.kind == "resolution-contract":
        h = source.hypothesis
        return {
            "hypothesis_identity": h.identity.qualified,
            "hypothesis_artifact_digest": h.artifact_digest,
            "hypothesis_statement_digest": h.statement_digest,
            "rule_kind": source.rule.operator,
            **{
                "hypothesis_" + k: getattr(h.coordinate, k)
                for k in ("git_oid", "semantic_root", "generation_root", "compiler_digest")
            },
        }

    if owner.kind == "attestation":
        s = source.statement
        return {
            "envelope_digest": claim_attestation_v2_envelope_digest(source),
            "statement_digest": claim_attestation_v2_statement_digest(s),
            "claim_identity": s.claim_identity.qualified,
            "claim_artifact_digest": s.claim_artifact_digest,
            "claim_statement_digest": s.claim_statement_digest,
            "principal_id": s.attesting_principal_id,
            "signing_key_digest": s.signing_key_digest,
            "basis": s.attestation_basis,
            "stance": s.stance,
            "attested_at_us": utc_microseconds(s.attested_at),
            "valid_until_us": utc_microseconds(s.valid_until),
            "subject_shell_digest": s.subject_shell_digest,
            "object_shell_digest": s.object_shell_digest,
            **{
                "referent_" + k: getattr(s.referent_coordinate, k)
                for k in ("git_oid", "semantic_root", "generation_root", "compiler_digest")
            },
        }
    if owner.kind == "claim":
        return _claim_fields(source)
    result = {name: getattr(source, name) for name, _ in owner.fields if hasattr(source, name)}
    if owner.kind == "procedure":
        result.update(
            definition_format=str(source.definition.graph_format),
            directly_runnable=int(source.directly_runnable),
        )
    if owner.kind in ("line", "procedure-mandate"):
        result.update(
            procedure_identity=source.procedure.target.qualified,
            procedure_digest=source.procedure.artifact_digest,
        )
    if owner.kind == "line":
        result["identity_digest"] = line_identity_digest(source.identity)
    if owner.kind == "standing-mandate":
        result["provider_identity"] = source.provider.qualified
    for name in ("valid_from", "valid_until", "expires_at"):
        if hasattr(source, name):
            result[name + "_us"] = utc_microseconds(getattr(source, name))
    return result


def claim_type_names(source: ClaimType) -> tuple[tuple[str, str, str, str], ...]:
    """Only live ontology names are discoverable; their definitions stay in Git."""
    if source.lifecycle.state != "live":
        return ()
    names = {("predicate", source.predicate)} | {
        ("subject_kind", name)
        for name in (*source.allowed_subject_kinds, *source.allowed_object_subject_kinds)
    }
    return tuple(
        (source.identity.qualified, kind, name, name.rsplit(".", 1)[-1])
        for kind, name in sorted(names)
    )


def select_claim_type_names(
    connection: sqlite3.Connection, names: Iterable[str], *, table: str = "main.claim_type_names"
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Keep every matching owner, including ambiguous suffixes, without body scans."""
    owners: set[str] = set()
    kinds: set[str] = set()
    for name in sorted(set(names)):
        for source, kind, full_name in connection.execute(
            f"SELECT source_identity,kind,name FROM {table} WHERE name=? "
            f"UNION SELECT source_identity,kind,name FROM {table} WHERE leaf=?",
            (name, name),
        ):
            if kind == "predicate":
                owners.add(source)
            else:
                kinds.add(full_name)
    return tuple(sorted(owners)), tuple(sorted(kinds))


def insert_owners(
    connection: sqlite3.Connection,
    *,
    parsed: ParsedProjectionTree,
    blobs: Mapping[str, bytes],
    codec: ArtifactCodec,
    resolve_digest: Callable[[str], Iterable[str]] | None = None,
) -> None:
    """Insert validated changed owners after removing both old and new paths."""
    sources: dict[str, Any] = {}
    for row in parsed.envelopes:
        if row.identity in SINGLETONS:
            if row.kind != SINGLETONS[row.identity][0]:
                raise ProjectionIntegrityError("artifact uses a reserved singleton identity")
            continue
        owner = OWNER_BY_KIND[row.kind]
        if connection.execute(
            "SELECT 1 FROM artifact_lookup WHERE identity=? OR path=?", (row.identity, row.path)
        ).fetchone():
            raise ProjectionIntegrityError("cross-kind artifact identity/path collision")
        source = owner.parse(blobs[row.path], path=row.path, codec=codec)
        sources[row.identity] = source
        values: dict[str, SQLValue] = {
            "identity": row.identity,
            "path": row.path,
            "format_tag": row.format_tag,
            "artifact_digest": row.artifact_digest,
        }
        if owner.versioned:
            values.update(predecessor_digest=row.predecessor_digest, revision=row.revision)
        if owner.lifecycle:
            values["lifecycle"] = (
                source.lifecycle.status if row.kind == "document" else source.lifecycle.state
            )
        values.update(owner_values(owner, source))
        columns = tuple(values)
        connection.execute(
            f"INSERT INTO {owner.table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(values.values()),
        )
        if isinstance(source, ClaimType):
            connection.executemany(
                "INSERT INTO claim_type_names VALUES (?,?,?,?)", claim_type_names(source)
            )
    for path, content in blobs.items():
        if path.startswith("principals/"):
            principal = PrincipalRecord.model_validate_json(content)
            connection.execute(
                "INSERT INTO principals VALUES (?,?,?,?,?,?,?)",
                (
                    principal.principal_id,
                    path,
                    principal.tag,
                    principal.algorithm,
                    principal.public_key,
                    principal.kind,
                    principal.status,
                ),
            )
    ordinal: dict[str, int] = {}
    for pin in parsed.pins:
        index = ordinal.get(pin.source_identity, 0)
        connection.execute(
            "INSERT INTO pins VALUES (?,?,?,?,?,?)",
            (
                pin.source_identity,
                "required_pin",
                index,
                pin.target_identity,
                pin.target_digest,
                "resolved",
            ),
        )
        ordinal[pin.source_identity] = index + 1
    for row in parsed.envelopes:
        if row.kind != "claim":
            continue
        for index, digest in enumerate(sources[row.identity].backing.input_claim_digests):
            identities = set(resolve_digest(digest) if resolve_digest else ())
            identities.update(
                item[0]
                for item in connection.execute(
                    "SELECT identity FROM claims WHERE artifact_digest=?", (digest,)
                )
            )
            status = (
                "resolved" if len(identities) == 1 else "ambiguous" if identities else "unresolved"
            )
            identity = next(iter(identities)) if len(identities) == 1 else None
            connection.execute(
                "INSERT INTO pins VALUES (?,?,?,?,?,?)",
                (row.identity, "consumed_claim_input", index, identity, digest, status),
            )


def principal_registry(
    connection: sqlite3.Connection, semantic_root: str
) -> PrincipalRegistrySnapshot:
    principals = tuple(
        PrincipalRecord(
            principal_id=row[0],
            tag=row[1],
            algorithm=row[2],
            public_key=row[3],
            kind=row[4],
            status=row[5],
        )
        for row in connection.execute(
            "SELECT principal_id,format_tag,algorithm,public_key,kind,status FROM principals ORDER BY principal_id"
        )
    )
    try:
        return PrincipalRegistrySnapshot(semantic_root=semantic_root, principals=principals)
    except ValueError as exc:
        raise PrincipalIntegrityError("typed projection principal registry is invalid") from exc


def singleton_path(identity: str, codec: ArtifactCodec) -> str | None:
    singleton = SINGLETONS.get(identity)
    return None if singleton is None else artifact_path_for_codec(singleton[1], codec)


@dataclass(frozen=True)
class ProcedureInventoryRow:
    identity: str
    path: str
    lifecycle: str
    directly_runnable: bool


class TypedStateReader:
    """Indexed metadata and exact selected source reads under one accepted handle."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        accepted: Any,
        repository: Any,
        *,
        bodies: Any = None,
        history: Any = None,
    ) -> None:
        self.connection = connection
        self.accepted = accepted
        self.repository = repository
        self.bodies = bodies
        self.history = history
        from cruxible_core.indexes.history.history_index import RetainedRecordReader

        self.records = RetainedRecordReader(repository.blob_at) if history is not None else None
        self._member_bytes: dict[str, bytes] = {}
        self.work = {"members_read": 0, "source_bytes": 0, "owners_selected": 0}

    @property
    def codec(self) -> ArtifactCodec:
        from cruxible_core.compiler.compiler import artifact_codec_for_compiler

        return artifact_codec_for_compiler(self.accepted.compiler)

    def member_bytes(self, path: str) -> bytes:
        self.prefetch_members((path,))
        return self._member_bytes[path]

    def prefetch_members(self, paths: tuple[str, ...]) -> None:
        """Authenticate selected bytes once per handle, with batched Git object reads."""
        from cruxible_client.contracts.canonical import file_digest

        pending = tuple(dict.fromkeys(path for path in paths if path not in self._member_bytes))
        for start in range(0, len(pending), 500):
            selected = pending[start : start + 500]
            rows = self.connection.execute(
                "SELECT path,git_blob_oid,file_digest,byte_length FROM main.members "
                "WHERE path IN (" + ",".join("?" for _ in selected) + ")",
                selected,
            ).fetchall()
            missing = set(selected) - {row[0] for row in rows}
            if missing:
                raise ProjectionIntegrityError(f"accepted member is absent: {min(missing)}")
            contents = self.repository.read_blobs(tuple(row[1] for row in rows))
            for path, oid, digest, length in rows:
                content = contents[oid]
                self.work["members_read"] += 1
                self.work["source_bytes"] += len(content)
                if len(content) != length or file_digest(content).tagged != digest:
                    raise ProjectionIntegrityError(
                        f"accepted member differs from its projection: {path}"
                    )
                self._member_bytes[path] = content

    def envelope(self, identity: str) -> ArtifactEnvelopeRow | None:
        if identity in SINGLETONS:
            path = singleton_path(identity, self.codec)
            assert path is not None
            if (
                self.connection.execute("SELECT 1 FROM members WHERE path=?", (path,)).fetchone()
                is None
            ):
                return None
            if self.history is None:
                parsed = self._compile_paths((path,))
                return next((row for row in parsed.envelopes if row.identity == identity), None)
            from cruxible_client.contracts.canonical import file_digest

            content = self.member_bytes(path)
            kind, _, parser = SINGLETONS[identity]
            source: Any = parser(content, path=path, codec=self.codec)
            digest = (
                approval_policy_digest(source).tagged
                if identity == APPROVAL_POLICY_IDENTITY
                else procedure_runtime_policy_digest(source).tagged
            )
            with self.history() as history:
                locations = history.member_history(path)
            # Frozen projected_revision counts every relevant member, including
            # unchanged-byte reevaluations. C preserves legacy file digests as
            # file digests, so neither branch reinterprets an old commitment.
            digests = {digest, file_digest(content).tagged}
            revision = len(locations) + int(
                not any(location.artifact_digest in digests for location in locations)
            )
            return ArtifactEnvelopeRow(identity, kind, source.tag, path, digest, None, revision)
        owner = owner_for_identity(identity)
        if owner is None:
            sql = f"SELECT {COMMON_COLUMNS} FROM artifact_lookup WHERE identity=?"
        else:
            version = "predecessor_digest,revision" if owner.versioned else "NULL,1"
            sql = f"SELECT identity,'{owner.kind}',format_tag,path,artifact_digest,{version} FROM {owner.table} WHERE identity=?"
        row = self.connection.execute(sql, (identity,)).fetchone()
        self.work["owners_selected"] += row is not None
        return None if row is None else ArtifactEnvelopeRow(*row)

    def envelopes(
        self, *, paths: tuple[str, ...] | None = None, kind: str | None = None
    ) -> tuple[ArtifactEnvelopeRow, ...]:
        sql = f"SELECT {COMMON_COLUMNS} FROM artifact_lookup"
        params: list[str] = []
        predicates = []
        if paths is not None:
            predicates.append("path IN (" + ",".join("?" for _ in paths) + ")")
            params.extend(paths)
        if kind is not None:
            predicates.append("kind=?")
            params.append(kind)
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        rows = [ArtifactEnvelopeRow(*row) for row in self.connection.execute(sql, params)]
        for identity, singleton in SINGLETONS.items():
            if kind is not None and singleton[0] != kind:
                continue
            path = singleton_path(identity, self.codec)
            assert path is not None
            if paths is not None and path not in paths:
                continue
            row = self.envelope(identity)
            if row is not None:
                rows.append(row)
        self.work["owners_selected"] += len(rows)
        return tuple(sorted(rows, key=lambda row: row.identity.encode("utf-8")))

    def query_artifact_definitions(
        self,
        *,
        kind: str,
        namespaces: tuple[str, ...] = (),
        name_prefixes: tuple[str, ...] = (),
        limit: int,
    ) -> tuple[int, tuple[ArtifactEnvelopeRow, ...]]:
        """Select owners through primary-key ranges, reading only retained result bodies.

        Namespace matches the predicate before its final dot, not descendants.
        Procedure scope is explicitly lexical, never inferred domain membership.
        """
        if kind not in {"ClaimType", "Procedure"}:
            raise ValueError("unsupported artifact query kind")
        table, owner_kind = (
            ("claim_types", "claim-type") if kind == "ClaimType" else ("procedures", "procedure")
        )
        clauses: list[str] = []
        params: list[str | int] = []
        for name in namespaces or name_prefixes:
            prefix = kind + ":" + name + ("." if namespaces else "")
            clause = "(identity >= ? AND identity < ?"
            params.extend((prefix, prefix[:-1] + "/"))
            if namespaces:
                clause += " AND instr(substr(identity, ?), '.') = 0"
                params.append(len(prefix) + 1)
            clauses.append(clause + ")")
        where = "lifecycle='live'" + (" AND (" + " OR ".join(clauses) + ")" if clauses else "")
        count = self.connection.execute(
            f"SELECT count(*) FROM {table} WHERE {where}", params
        ).fetchone()[0]
        rows = self.connection.execute(
            f"SELECT identity,'{owner_kind}',format_tag,path,artifact_digest,predecessor_digest,revision FROM {table} WHERE {where} ORDER BY identity LIMIT ?",
            [*params, limit],
        )
        result = tuple(ArtifactEnvelopeRow(*row) for row in rows)
        self.work["owners_selected"] += len(result)
        return count, result

    def source(self, identity: str) -> Any | None:
        row = self.envelope(identity)
        if row is None:
            return None
        parser = (
            SINGLETONS[identity][2] if identity in SINGLETONS else OWNER_BY_KIND[row.kind].parse
        )
        return parser(self.member_bytes(row.path), path=row.path, codec=self.codec)

    def principal(self, principal_id: str, *, active: bool = False) -> PrincipalRecord | None:
        row = self.connection.execute(
            "SELECT principal_id,format_tag,algorithm,public_key,kind,status FROM principals WHERE principal_id=?",
            (principal_id,),
        ).fetchone()
        if row is None:
            if active:
                raise PrincipalIntegrityError(
                    f"principal is absent at {self.accepted.semantic_root}: {principal_id}"
                )
            return None
        result = PrincipalRecord(
            principal_id=row[0],
            tag=row[1],
            algorithm=row[2],
            public_key=row[3],
            kind=row[4],
            status=row[5],
        )
        if active and result.status != "active":
            raise PrincipalIntegrityError(
                f"principal is not active at {self.accepted.semantic_root}: {principal_id}"
            )
        return result

    def claim_attestations(
        self,
        claim_identity: str | None = None,
        claim_artifact_digest: str | None = None,
        *,
        basis: str | None = None,
        current_claims_only: bool = False,
        claim_predicates: tuple[str, ...] | None = None,
    ) -> tuple[ClaimAttestationV2, ...]:
        from cruxible_client.contracts.accepted_attestations import (
            attestation_artifact_digest,
            parse_accepted_attestation,
        )
        from cruxible_client.contracts.claim_attestations import (
            claim_attestation_v2_envelope_digest,
        )

        predicates: list[str] = []
        parameters: list[str] = []
        if claim_identity is not None:
            if claim_artifact_digest is None:
                raise ValueError("attestation selection requires an exact Claim version")
            predicates.extend(("claim_identity=?", "claim_artifact_digest=?"))
            parameters.extend((claim_identity, claim_artifact_digest))
        if basis is not None:
            predicates.append("basis=?")
            parameters.append(basis)
        if current_claims_only or claim_predicates is not None:
            selected = ""
            if claim_predicates is not None:
                if not claim_predicates:
                    return ()
                selected = " WHERE predicate IN (" + ",".join("?" for _ in claim_predicates) + ")"
                parameters.extend(claim_predicates)
            predicates.append(
                "(claim_identity,claim_artifact_digest) IN "
                "(SELECT identity,artifact_digest FROM claims" + selected + ")"
            )
        rows = self.connection.execute(
            "SELECT path,envelope_digest,artifact_digest FROM attestations "
            + ("WHERE " + " AND ".join(predicates) if predicates else "")
            + " ORDER BY attested_at_us,envelope_digest",
            parameters,
        ).fetchall()
        self.prefetch_members(tuple(row[0] for row in rows))
        values = []
        for path, digest, artifact_digest in rows:
            value = parse_accepted_attestation(self.member_bytes(path), path=path)
            if (
                claim_attestation_v2_envelope_digest(value) != digest
                or attestation_artifact_digest(value).tagged != artifact_digest
                or (basis is not None and value.statement.attestation_basis != basis)
                or (
                    claim_identity is not None
                    and (
                        value.statement.claim_identity.qualified != claim_identity
                        or value.statement.claim_artifact_digest != claim_artifact_digest
                    )
                )
            ):
                raise ProjectionIntegrityError("attestation lookup differs from its signed member")
            values.append(value)
        return tuple(values)

    def procedure_inventory(self) -> tuple[ProcedureInventoryRow, ...]:
        """Return the accepted Procedure catalog without decoding graph bytes."""
        return tuple(
            ProcedureInventoryRow(row[0], row[1], row[2], bool(row[3]))
            for row in self.connection.execute(
                "SELECT identity,path,lifecycle,directly_runnable FROM procedures ORDER BY path"
            )
        )

    def dependency_state(self, identity: str) -> ArtifactDependencyStateV1 | None:
        """Read one exact selected owner contract, preserving role-bearing pins."""
        from cruxible_client.contracts.documents import DocumentArtifactAdapter
        from cruxible_core.claims.closure import ArtifactDependencyStateV1

        row = self.envelope(identity)
        if row is None or row.kind not in OWNER_BY_KIND:
            return None
        if row.kind == "attestation":
            from cruxible_core.claims.closure import parse_dependency_artifact

            return parse_dependency_artifact(row.path, self.member_bytes(row.path))
        source = OWNER_BY_KIND[row.kind].parse(
            self.member_bytes(row.path), path=row.path, codec=self.codec
        )
        adapted = DocumentArtifactAdapter(source) if row.kind == "document" else source
        return ArtifactDependencyStateV1(
            path=row.path,
            artifact_kind=cast(Any, row.kind),
            artifact_tag=row.format_tag,
            identity=adapted.identity,
            artifact_digest=row.artifact_digest,
            pins=adapted.pins,
            lifecycle=adapted.lifecycle,
        )

    def principal_registry(self) -> PrincipalRegistrySnapshot:
        return principal_registry(self.connection, self.accepted.semantic_root)

    def _compile_paths(self, paths: tuple[str, ...]) -> ParsedProjectionTree:
        from cruxible_client.contracts.projection import AcceptedCoordinate
        from cruxible_core.compiler.compiler import (
            artifact_kinds_for_compiler,
            projection_registry_for_compiler,
        )
        from cruxible_core.compiler.projection_artifacts import parse_projection_tree
        from cruxible_core.indexes.projection import AssemblerRequest
        from cruxible_core.proposals.settlement import parse_change_set_record

        records = {}
        coordinates = {}
        self.prefetch_members(paths)
        if self.history is not None:
            assert self.records is not None
            with self.history() as history:
                for path in paths:
                    for location in history.member_history(path):
                        generation = history.generation(location.sequence)
                        record_path = generation.source_record_path
                        record = history.read_member_record(location, load_record=self.records)
                        records[location.sequence] = (record_path, record)
                        coordinates[location.sequence] = AcceptedCoordinate(
                            git_oid=generation.git_oid,
                            semantic_root=generation.semantic_root,
                            generation_root=generation.generation_root,
                            compiler_digest=generation.compiler_digest,
                        )
        else:
            # Explicit standalone publication readers lack an instance's verified
            # history owner. Reconstruction remains a cold fallback; served instance
            # reads inject C's sparse member-history reader below.
            for (path,) in self.connection.execute(
                "SELECT path FROM members WHERE path LIKE 'changesets/%' ORDER BY path"
            ):
                record = parse_change_set_record(self.member_bytes(path), path=path)
                records[record.sequence] = (path, record)
        request = AssemblerRequest(
            instance_id=self.accepted.instance_id,
            repository_path=self.accepted.repository_path,
            git_object_format=self.accepted.git_object_format,
            git_oid=self.accepted.git_oid,
            semantic_root=self.accepted.semantic_root,
            generation_root=self.accepted.generation_root,
            compiler_digest=self.accepted.compiler.rule_digest,
            schema_version=self.accepted.compiler.schema_version,
            output_staging_directory=self.accepted.repository_path,
        )
        return parse_projection_tree(
            {path: self.member_bytes(path) for path in paths},
            registry=projection_registry_for_compiler(self.accepted.compiler),
            artifact_kinds=artifact_kinds_for_compiler(self.accepted.compiler),
            artifact_codec=self.codec,
            bodies=self.bodies,
            coordinate=request,
            accepted_coordinates_by_sequence=coordinates,
            selected_member_history=tuple(records[key] for key in sorted(records)),
        )

    def facts_for(self, rows: tuple[ArtifactEnvelopeRow, ...]) -> dict[str, tuple[Any, ...]]:
        """Compile a selected owner set together, without repeating shared history."""
        grouped: dict[str, list[Any]] = {row.identity: [] for row in rows}
        if rows:
            for fact in self._compile_paths(tuple(row.path for row in rows)).semantic_facts:
                if fact.subject_identity in grouped:
                    grouped[fact.subject_identity].append(fact)
        return {identity: tuple(facts) for identity, facts in grouped.items()}

    def facts(
        self, schema_id: str | None = None, *, identity: str | None = None
    ) -> tuple[Any, ...]:
        if schema_id in ("playbill.procedure.track_record", "playbill.line.track_record"):
            sql = "SELECT DISTINCT p.path FROM promotion_subjects s JOIN exhaust_promotions p ON p.identity=s.promotion_identity WHERE s.record_kind=?"
            values = [schema_id.split(".")[1]]
            if identity is not None:
                sql += " AND s.subject_identity=?"
                values.append(identity)
            paths = tuple(
                row[0] for row in self.connection.execute(sql + " ORDER BY p.path", values)
            )
            parsed = self._compile_paths(paths)
            return tuple(
                fact
                for fact in parsed.semantic_facts
                if fact.schema_id == schema_id
                and (identity is None or fact.subject_identity == identity)
            )
        rows: tuple[ArtifactEnvelopeRow, ...]
        if identity is not None:
            envelope = self.envelope(identity)
            rows = () if envelope is None else (envelope,)
        else:
            kind = None
            if schema_id is not None and schema_id.startswith("playbill."):
                family = schema_id.split(".")[1].replace("_", "-")
                if family in OWNER_BY_KIND:
                    kind = family
            rows = self.envelopes(kind=kind)
        parsed = self._compile_paths(tuple(row.path for row in rows))
        return tuple(
            fact
            for fact in parsed.semantic_facts
            if (schema_id is None or fact.schema_id == schema_id)
            and (identity is None or fact.subject_identity == identity)
        )
