"""Typed accepted citation relationships; exact envelopes remain authoritative.

No conflict results or CAS availability are stored here. Group reads feed the
frozen relation computation, and callers retain only their request's results.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Literal

from cruxible_client.contracts.canonical import CURRENT_ARTIFACT_CODEC, ArtifactCodec
from cruxible_client.contracts.captures import (
    CaptureEnvelopeAny,
    capture_digest,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext, BodyProjectionProtocol
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationV2,
    claim_attestation_v2_envelope_digest,
)
from cruxible_client.contracts.claims import claim_citation_references, parse_claim
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
)
from cruxible_core.evidence.citation_relations import (
    _conflict_facts,
    _digest_identity,
    _same_version_span_key,
    external_source_relation_subject,
)

OwnerKind = Literal["Claim", "attestation"]
OwnerExists = Callable[[str, str], bool]
_ACCESS = BodyAccessContext(principal_id="playbill-citation-relation", can_read_body=True)
_MAX_SQL_INTEGER = (1 << 63) - 1

SCHEMA_SQL = """
CREATE TABLE source_references (
 source_ref_key TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('external','ledger','cas')),
 source_identity TEXT, producer_binding_digest TEXT, coordinate_type TEXT,
 source_version_key TEXT, selector_type TEXT, start_byte INTEGER, end_byte INTEGER,
 ledger_git_oid TEXT, ledger_semantic_root TEXT, ledger_generation_root TEXT,
 ledger_compiler_digest TEXT, ledger_path TEXT, cas_content_digest TEXT, replayability TEXT,
 CHECK ((start_byte IS NULL AND end_byte IS NULL) OR
        (start_byte IS NOT NULL AND end_byte IS NOT NULL
         AND 0 <= start_byte AND start_byte < end_byte))
) STRICT;
CREATE INDEX source_spans_by_version ON source_references
 (source_version_key,start_byte,end_byte,source_ref_key) WHERE source_version_key IS NOT NULL;
CREATE TABLE captures (
 capture_digest TEXT PRIMARY KEY, contract_digest TEXT NOT NULL,
 evidence_commitment_digest TEXT NOT NULL, logical_source_id TEXT,
 source_ref_key TEXT NOT NULL REFERENCES source_references(source_ref_key),
 producer_identity TEXT NOT NULL, observed_at_us INTEGER NOT NULL,
 access_class TEXT NOT NULL CHECK(access_class IN ('public','instance','restricted'))
) STRICT;
CREATE INDEX captures_by_commitment ON captures(evidence_commitment_digest,capture_digest);
CREATE INDEX captures_by_logical_source ON captures(logical_source_id,capture_digest);
CREATE INDEX captures_by_source_ref ON captures(source_ref_key,capture_digest);
CREATE TABLE citation_uses (
 owner_kind TEXT NOT NULL CHECK(owner_kind IN ('Claim','attestation')),
 owner_key TEXT NOT NULL, use_key TEXT NOT NULL,
 capture_digest TEXT NOT NULL REFERENCES captures(capture_digest), origin TEXT, role TEXT,
 PRIMARY KEY(owner_kind,owner_key,use_key),
 CHECK ((owner_kind='Claim' AND origin IS NOT NULL AND role IS NOT NULL) OR
        (owner_kind='attestation' AND origin IS NULL AND role IS NULL AND use_key=capture_digest))
) STRICT;
CREATE INDEX citations_by_capture ON citation_uses(capture_digest,owner_kind,owner_key,use_key);
CREATE TABLE citation_group_members (
 group_kind TEXT NOT NULL CHECK(group_kind IN ('capture','exact_external','same_version_span')),
 group_key TEXT NOT NULL, claim_identity TEXT NOT NULL, citation_id TEXT NOT NULL,
 PRIMARY KEY(group_kind,group_key,claim_identity,citation_id)
) STRICT;
CREATE INDEX citation_groups_by_use ON citation_group_members
 (claim_identity,citation_id,group_kind,group_key);
"""


def _insert_capture(
    connection: sqlite3.Connection, digest: str, bodies: BodyProjectionProtocol
) -> CaptureEnvelopeAny:
    envelope = parse_capture_envelope(bodies.read(digest, access=_ACCESS))
    if capture_digest(envelope).tagged != digest:
        raise ProjectionFormatError("citation Capture digest differs from its exact envelope")
    source = envelope.source
    # Source locators commit the full source contract. Conflict grouping keeps
    # its original narrower derivation, including its producer-binding exclusion.
    key = _digest_identity("source-reference", source.model_dump(mode="json"))
    span = _same_version_span_key({"source": source.model_dump(mode="json")})
    version, start, end = span if span is not None else (None, None, None)
    if end is not None and end > _MAX_SQL_INTEGER:
        # Canonical source extensions allow arbitrary exact integers. Retain the
        # version and selector, include this row in candidate queries, and compare
        # the original envelope's integers. Never cast or saturate signed bytes.
        start = end = None
    external = source if isinstance(source, ExternalSourceReferenceV1) else None
    ledger = source if isinstance(source, LedgerSourceReferenceV1) else None
    cas = source if isinstance(source, CasSourceReferenceV1) else None
    connection.execute(
        "INSERT OR IGNORE INTO source_references VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            key,
            source.kind,
            external.source_identity if external else None,
            external.producer_binding_digest if external else None,
            external.coordinate_type if external else None,
            version,
            external.selector_type if external else None,
            start,
            end,
            ledger.coordinate.git_oid if ledger else None,
            ledger.coordinate.semantic_root if ledger else None,
            ledger.coordinate.generation_root if ledger else None,
            ledger.coordinate.compiler_digest if ledger else None,
            ledger.address.artifact_path if ledger else None,
            cas.content_digest if cas else None,
            external.replayability if external else None,
        ),
    )
    elapsed = envelope.observed_at.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    observed_us = (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds
    connection.execute(
        "INSERT OR IGNORE INTO captures VALUES (?,?,?,?,?,?,?,?)",
        (
            digest,
            envelope.capture_contract_digest,
            envelope.commitment.digest,
            external.source_identity
            if external
            else (ledger.address.artifact_path if ledger else None),
            key,
            envelope.producer.qualified,
            observed_us,
            "instance",
        ),
    )
    return envelope


def _cleanup_captures(connection: sqlite3.Connection, captures: Iterable[str]) -> None:
    for digest in set(captures):
        source = connection.execute(
            "SELECT source_ref_key FROM captures WHERE capture_digest=?", (digest,)
        ).fetchone()
        connection.execute(
            "DELETE FROM captures WHERE capture_digest=? AND NOT EXISTS "
            "(SELECT 1 FROM citation_uses WHERE capture_digest=?)",
            (digest, digest),
        )
        if source is not None:
            connection.execute(
                "DELETE FROM source_references WHERE source_ref_key=? AND NOT EXISTS "
                "(SELECT 1 FROM captures WHERE source_ref_key=?)",
                (source[0], source[0]),
            )


def remove_owner_citations(
    connection: sqlite3.Connection, owners: Iterable[tuple[str, str]]
) -> None:
    """Remove selected owner relationships inside the caller's publication transaction."""
    connection.execute("SAVEPOINT remove_citations")
    try:
        for kind, key in owners:
            if kind not in ("Claim", "attestation"):
                raise ProjectionFormatError("unknown citation owner kind")
            captures = [
                r[0]
                for r in connection.execute(
                    "SELECT capture_digest FROM citation_uses WHERE owner_kind=? AND owner_key=?",
                    (kind, key),
                )
            ]
            if kind == "Claim":
                connection.execute(
                    "DELETE FROM citation_group_members WHERE claim_identity=?", (key,)
                )
            connection.execute(
                "DELETE FROM citation_uses WHERE owner_kind=? AND owner_key=?", (kind, key)
            )
            _cleanup_captures(connection, captures)
        connection.execute("RELEASE remove_citations")
    except BaseException:
        connection.execute("ROLLBACK TO remove_citations")
        connection.execute("RELEASE remove_citations")
        raise


def populate_citations(
    connection: sqlite3.Connection,
    sources: Mapping[str, bytes],
    *,
    bodies: BodyProjectionProtocol,
    owner_exists: OwnerExists,
    artifact_codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> None:
    """Replace changed Claim owners after typed owner publication, with exact source IDs."""
    # SAVEPOINT composes with a larger transaction and rolls back a partial owner
    # replacement on failed/missing CAS reads without committing caller writes.
    connection.execute("SAVEPOINT populate_citations")
    try:
        for path, content in sorted(sources.items()):
            if not path.startswith("claims/"):
                continue
            claim = parse_claim(content, path=path, codec=artifact_codec)
            identity = claim.identity.qualified
            if not owner_exists("Claim", identity):
                raise ProjectionFormatError("citation Claim owner is not published")
            old_captures = [
                r[0]
                for r in connection.execute(
                    "SELECT capture_digest FROM citation_uses "
                    "WHERE owner_kind='Claim' AND owner_key=?",
                    (identity,),
                )
            ]
            connection.execute(
                "DELETE FROM citation_group_members WHERE claim_identity=?", (identity,)
            )
            connection.execute(
                "DELETE FROM citation_uses WHERE owner_kind='Claim' AND owner_key=?", (identity,)
            )
            envelopes: dict[str, CaptureEnvelopeAny] = {}
            for reference in claim_citation_references(claim):
                digest = reference.capture_digest
                envelope = envelopes.get(digest)
                if envelope is None:
                    envelope = _insert_capture(connection, digest, bodies)
                    envelopes[digest] = envelope
                connection.execute(
                    "INSERT INTO citation_uses VALUES ('Claim',?,?,?,?,?)",
                    (
                        identity,
                        reference.citation_id,
                        digest,
                        getattr(reference, "origin", "legacy"),
                        getattr(reference, "role", "legacy"),
                    ),
                )
                groups = [("capture", digest)]
                if isinstance(envelope.source, ExternalSourceReferenceV1):
                    groups.append(
                        ("exact_external", external_source_relation_subject(envelope.source))
                    )
                    span = _same_version_span_key(
                        {"source": envelope.source.model_dump(mode="json")}
                    )
                    if span is not None:
                        groups.append(("same_version_span", span[0]))
                connection.executemany(
                    "INSERT INTO citation_group_members VALUES (?,?,?,?)",
                    [(kind, key, identity, reference.citation_id) for kind, key in groups],
                )
            _cleanup_captures(connection, old_captures)
        connection.execute("RELEASE populate_citations")
    except BaseException:
        connection.execute("ROLLBACK TO populate_citations")
        connection.execute("RELEASE populate_citations")
        raise


def populate_attestation_citations(
    connection: sqlite3.Connection,
    attestation: ClaimAttestationV2,
    *,
    bodies: BodyProjectionProtocol,
    owner_exists: OwnerExists,
) -> None:
    """Shared owner writer; acceptance/new-kind wiring is deliberately a separate step."""
    key = claim_attestation_v2_envelope_digest(attestation)
    if not owner_exists("attestation", key):
        raise ProjectionFormatError("citation attestation owner is not published")
    connection.execute("SAVEPOINT attestation_citations")
    try:
        for digest in attestation.statement.cited_capture_digests:
            _insert_capture(connection, digest, bodies)
            connection.execute(
                "INSERT OR IGNORE INTO citation_uses VALUES ('attestation',?,?,?,NULL,NULL)",
                (key, digest, digest),
            )
        connection.execute("RELEASE attestation_citations")
    except BaseException:
        connection.execute("ROLLBACK TO attestation_citations")
        connection.execute("RELEASE attestation_citations")
        raise


class CitationReader:
    """Read one bound SQLite publication; never establish acceptance or cache bodies."""

    def __init__(
        self, connection: sqlite3.Connection, exact_source: Callable[[str], bytes]
    ) -> None:
        self.connection = connection
        self.exact_source = exact_source

    def capture_contract_path(self, digest: str) -> str | None:
        row = self.connection.execute(
            "SELECT path FROM capture_contracts WHERE artifact_digest=?", (digest,)
        ).fetchone()
        return None if row is None else str(row[0])

    def owner_uses(self, kind: OwnerKind, key: str) -> tuple[dict[str, object], ...]:
        """SQL locator order; signed order belongs to the exact typed owner contract."""
        return self._rows(
            "SELECT owner_kind,owner_key,use_key,capture_digest,origin,role FROM citation_uses "
            "WHERE owner_kind=? AND owner_key=? ORDER BY use_key",
            (kind, key),
        )

    def owners_for_capture(self, digest: str) -> tuple[dict[str, object], ...]:
        return self._rows(
            "SELECT owner_kind,owner_key,use_key,capture_digest,origin,role FROM citation_uses "
            "WHERE capture_digest=? ORDER BY owner_kind,owner_key,use_key",
            (digest,),
        )

    def _rows(self, sql: str, values: tuple[object, ...] = ()) -> tuple[dict[str, object], ...]:
        cursor = self.connection.execute(sql, values)
        names = tuple(column[0] for column in cursor.description)
        return tuple(dict(zip(names, row, strict=True)) for row in cursor)

    @staticmethod
    def _envelope(
        digest: str, bodies: BodyProjectionProtocol, envelopes: dict[str, CaptureEnvelopeAny]
    ) -> CaptureEnvelopeAny:
        envelope = envelopes.get(digest)
        if envelope is None:
            envelope = parse_capture_envelope(bodies.read(digest, access=_ACCESS))
            if capture_digest(envelope).tagged != digest:
                raise ProjectionFormatError("citation Capture differs from its exact envelope")
            envelopes[digest] = envelope
        return envelope

    def _relation_uses(
        self,
        rows: Iterable[dict[str, object]],
        *,
        bodies: BodyProjectionProtocol,
        envelopes: dict[str, CaptureEnvelopeAny] | None = None,
    ) -> tuple[dict[str, object], ...]:
        resolved = {} if envelopes is None else envelopes
        uses = []
        for row in rows:
            digest = str(row["capture_digest"])
            envelope = self._envelope(digest, bodies, resolved)
            uses.append(
                {
                    "capture_contract_digest": {"$digest": envelope.capture_contract_digest},
                    "capture_digest": {"$digest": digest},
                    "citation_id": row["use_key"],
                    "claim_artifact_digest": {"$digest": row["artifact_digest"]},
                    "claim_identity": row["owner_key"],
                    "claim_lifecycle": row["lifecycle"],
                    "claim_path": row["path"],
                    "commitment": envelope.commitment.model_dump(mode="json"),
                    "origin": row["origin"],
                    "role": row["role"],
                    "source": envelope.source.model_dump(mode="json"),
                }
            )
        return tuple(
            sorted(uses, key=lambda use: (str(use["claim_path"]), str(use["citation_id"])))
        )

    def uses_for_source(
        self,
        source_id: str,
        *,
        bodies: BodyProjectionProtocol,
    ) -> tuple[dict[str, object], ...]:
        rows = self._rows(
            "SELECT u.*, c.path,c.artifact_digest,c.lifecycle FROM captures p "
            "JOIN source_references s USING(source_ref_key) "
            "JOIN citation_uses u USING(capture_digest) JOIN claims c ON c.identity=u.owner_key "
            "WHERE p.logical_source_id=? AND s.kind='external' AND u.owner_kind='Claim' "
            "ORDER BY c.path,u.use_key",
            (source_id,),
        )
        return self._relation_uses(rows, bodies=bodies)

    def overlapping_uses(
        self,
        source_version_key: str,
        start_byte: int,
        end_byte: int,
        *,
        bodies: BodyProjectionProtocol,
    ) -> tuple[dict[str, object], ...]:
        """Narrow by version and interval start, then compare original exact offsets."""
        if (
            type(start_byte) is not int
            or type(end_byte) is not int
            or not 0 <= start_byte < end_byte
        ):
            raise ValueError("citation interval requires increasing nonnegative exact integers")
        # Null bounds with a version key denote a recognized but unindexable
        # arbitrary-precision span, not a missing span. They are always candidates.
        rows = self._rows(
            "SELECT u.*,c.path,c.artifact_digest,c.lifecycle FROM source_references s "
            "JOIN captures p USING(source_ref_key) JOIN citation_uses u USING(capture_digest) "
            "JOIN claims c ON c.identity=u.owner_key WHERE s.source_version_key=? "
            "AND (s.start_byte IS NULL OR s.start_byte < ?) AND u.owner_kind='Claim' "
            "ORDER BY c.path,u.use_key",
            (source_version_key, min(end_byte, _MAX_SQL_INTEGER)),
        )
        uses = self._relation_uses(rows, bodies=bodies)
        return tuple(
            use
            for use in uses
            if (
                (span := _same_version_span_key(use)) is not None
                and span[1] < end_byte
                and start_byte < span[2]
            )
        )

    def conflicts(
        self,
        *,
        bodies: BodyProjectionProtocol,
        claim_identities: Iterable[str] | None = None,
    ) -> tuple[ProjectionFact, ...]:
        """Compute conflicts and witnesses together over complete relevant groups.

        Selected Claim queries read every group of each target, so capture
        precedence cannot hide an unvisited stronger conflict. A global worklist
        selects groups shared by live and retired Claims, not every Claim blob.
        """
        targets = None if claim_identities is None else set(claim_identities)
        groups: set[tuple[str, str]] = set()
        if targets is None:
            group_rows = self.connection.execute(
                "SELECT DISTINCT r.group_kind,r.group_key FROM claims c "
                "JOIN citation_group_members r ON r.claim_identity=c.identity "
                "WHERE c.lifecycle='retired' AND EXISTS (SELECT 1 FROM citation_group_members l "
                "JOIN claims live ON live.identity=l.claim_identity "
                "WHERE l.group_kind=r.group_kind "
                "AND l.group_key=r.group_key AND live.lifecycle='live')"
            )
            groups.update((row[0], row[1]) for row in group_rows)
        else:
            for identity in sorted(targets):
                groups.update(
                    (row[0], row[1])
                    for row in self.connection.execute(
                        "SELECT group_kind,group_key FROM citation_group_members "
                        "WHERE claim_identity=?",
                        (identity,),
                    )
                )
        envelopes: dict[str, CaptureEnvelopeAny] = {}
        facts: list[ProjectionFact] = []
        for kind, key in sorted(groups):
            rows = self._rows(
                "SELECT u.*,c.path,c.artifact_digest,c.lifecycle FROM citation_group_members g "
                "JOIN citation_uses u ON u.owner_kind='Claim' AND u.owner_key=g.claim_identity "
                "AND u.use_key=g.citation_id JOIN claims c ON c.identity=g.claim_identity "
                "WHERE g.group_kind=? AND g.group_key=? ORDER BY c.path,u.use_key",
                (kind, key),
            )
            uses = list(self._relation_uses(rows, bodies=bodies, envelopes=envelopes))
            facts.extend(_conflict_facts(uses, {f"{kind}:{key}"}))
        valued = [(fact, fact.value) for fact in facts if isinstance(fact.value, dict)]
        if targets is not None:
            valued = [
                (fact, value) for fact, value in valued if value["live_claim_identity"] in targets
            ]
        capture_precedence = {
            value["live_claim_identity"]
            for _, value in valued
            if value["relation_kind"] == "capture"
        }
        return tuple(
            sorted(
                (
                    fact
                    for fact, value in valued
                    if (
                        value["relation_kind"] == "capture"
                        or value["live_claim_identity"] not in capture_precedence
                    )
                ),
                key=lambda fact: fact.fact_key,
            )
        )
