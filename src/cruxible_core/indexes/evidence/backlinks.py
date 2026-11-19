"""The backlink index: every pointer into a target, keyed by the target (D-R12, D-R17).

Three pointer kinds, one table:

- ``cites``: a live Claim cites a workspace section (``write --section``). The
  target is the section (its catalogued source and heading path); the row also
  carries the citing Claim's Subject, so "sections citing X's Claims" is one
  indexed range.
- ``about``: a live note links a workspace section about a Subject, or a
  governed Document links ``about`` a Subject.
- ``guides``: a governed Document links ``guides`` a ClaimType or Procedure
  (review guidance).

Core relation vocabulary is closed (``about``, ``guides``); any other Document
link relation is a label and is not indexed. Rows are derived from accepted
artifacts alone and maintained inside the typed projection's publication
transaction, O(the owners a delta changes): a Claim's section citations are
found by one batched join over the citation index's own rows (only workspace
section Captures are opened, to read their heading-path label), a note's from
its own artifact, a Document's from its links.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import FOREIGN_SOURCE_SELECTOR_TYPE, parse_capture_envelope
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.documents import parse_document
from cruxible_client.contracts.notes import capture_section, parse_note
from cruxible_core.compiler.projection_artifacts import ParsedProjectionTree
from cruxible_core.indexes.sql_batch import execute_in, insert_rows, select_in

_ACCESS = BodyAccessContext(principal_id="playbill-backlink-index", can_read_body=True)
_OWNER_KINDS = {"claim": "Claim", "note": "Note", "document": "Document"}

SCHEMA_SQL = """
CREATE TABLE backlinks (
 relation TEXT NOT NULL CHECK(relation IN ('cites','about','guides')),
 target TEXT NOT NULL,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('Claim','Note','Document')),
 source_identity TEXT NOT NULL,
 link_key TEXT NOT NULL,
 source_id TEXT, section TEXT, cited_digest TEXT, capture_digest TEXT, role TEXT,
 claim_subject TEXT, observed_at_us INTEGER,
 PRIMARY KEY(target,relation,source_identity,link_key),
 CHECK ((relation='cites') = (source_kind='Claim')),
 CHECK ((source_kind='Document') = (source_id IS NULL)),
 CHECK ((relation='cites') = (claim_subject IS NOT NULL))
) STRICT;
CREATE INDEX backlinks_by_source ON backlinks(source_identity);
CREATE INDEX backlinks_by_claim_subject ON backlinks
 (claim_subject,source_identity,link_key) WHERE claim_subject IS NOT NULL;
CREATE INDEX backlinks_by_section ON backlinks
 (source_id,section,observed_at_us) WHERE source_id IS NOT NULL;
"""


def section_target(source_id: str, section: Iterable[str]) -> str:
    """The canonical key of one workspace section: its source and heading path."""

    return canonical_bytes([source_id, list(section)]).decode("utf-8")


def _section_text(section: Iterable[str]) -> str:
    return canonical_bytes(list(section)).decode("utf-8")


def _microseconds(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def _subject_identity(subject_path: str) -> str:
    return "Subject:" + subject_path.removeprefix("subjects/").removesuffix(".json")


def refresh_backlinks(
    connection: sqlite3.Connection,
    *,
    parsed: ParsedProjectionTree,
    sources: Mapping[str, bytes],
    replaced: Iterable[tuple[str, str, str]],
    bodies: Any,
) -> None:
    """Re-derive the rows of the owners this publication replaces or writes.

    ``replaced`` is (path, identity, kind) of every artifact the delta replaces;
    ``parsed`` carries the artifacts it writes. A retired owner leaves no rows.
    ``bodies`` is the publication's CAS reader; without one (no citation index
    either) only Document links are indexed.
    """

    incoming = [row for row in parsed.envelopes if row.kind in _OWNER_KINDS]
    owners = sorted(
        {identity for _path, identity, kind in replaced if kind in _OWNER_KINDS}
        | {row.identity for row in incoming}
    )
    execute_in(connection, "DELETE FROM backlinks WHERE source_identity IN ({})", owners)
    rows: list[tuple[object, ...]] = []
    for row in incoming:
        if row.kind == "document":
            document = parse_document(sources[row.path], path=row.path)
            rows.extend(
                (link.relation, link.target_identity, "Document", row.identity, "") + (None,) * 7
                for link in document.links
                if link.indexed
            )
        elif row.kind == "note":
            note = parse_note(sources[row.path], path=row.path)
            if note.lifecycle.state != "live":
                continue
            observed = None
            if bodies is not None:
                envelope = parse_capture_envelope(bodies.read(note.capture_digest, access=_ACCESS))
                observed = _microseconds(envelope.observed_at)
            rows.extend(
                (
                    "about",
                    target.qualified,
                    "Note",
                    row.identity,
                    "",
                    note.source_id,
                    _section_text(note.section),
                    note.section_digest,
                    note.capture_digest,
                    None,
                    None,
                    observed,
                )
                for target in note.about
            )
    claims = [row.identity for row in incoming if row.kind == "claim"]
    if bodies is not None and claims:
        rows.extend(_claim_section_rows(connection, claims, bodies))
    insert_rows(connection, "backlinks", rows)


def _claim_section_rows(
    connection: sqlite3.Connection, claims: list[str], bodies: Any
) -> list[tuple[object, ...]]:
    """Each live Claim's citations of a workspace section, from the citation index.

    One batched join selects the citations of workspace Captures (the
    foreign-source selector); only those envelopes are opened, for their
    heading-path label. A Procedure's Claims cite provider Captures, so their
    settle path opens none.
    """

    cited = select_in(
        connection,
        "SELECT u.owner_key,u.capture_digest,u.role,k.subject_path,c.observed_at_us "
        "FROM citation_uses u JOIN claims k ON k.identity=u.owner_key "
        "JOIN captures c ON c.capture_digest=u.capture_digest "
        "JOIN source_references s ON s.source_ref_key=c.source_ref_key "
        "WHERE u.owner_kind='Claim' AND k.lifecycle='live' AND s.selector_type=? "
        "AND u.owner_key IN ({})",
        claims,
        prefix=(FOREIGN_SOURCE_SELECTOR_TYPE,),
    )
    sections: dict[str, tuple[str, tuple[str, ...], str] | None] = {}
    rows: list[tuple[object, ...]] = []
    for owner, capture, role, subject_path, observed in cited:
        if capture not in sections:
            envelope = parse_capture_envelope(bodies.read(capture, access=_ACCESS))
            sections[capture] = capture_section(envelope)
        found = sections[capture]
        if found is None:
            continue
        source_id, section, digest = found
        rows.append(
            (
                "cites",
                section_target(source_id, section),
                "Claim",
                owner,
                capture,
                source_id,
                _section_text(section),
                digest,
                capture,
                role,
                _subject_identity(str(subject_path)),
                observed,
            )
        )
    # One Claim may cite one Capture in two roles (evidence and copy): one row.
    return list({(row[1], row[3], row[4]): row for row in rows}.values())


__all__ = ["SCHEMA_SQL", "refresh_backlinks", "section_target"]
