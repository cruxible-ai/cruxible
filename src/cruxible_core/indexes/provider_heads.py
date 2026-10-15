"""Provider name summaries maintained from accepted changed owners and records.

Only cold construction reads all retained records. A successor consumes its new
records and changed providers; indexed top-two seeks maintain newest selection
and ambiguity without enumerating a name's retained builds.
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from cruxible_client.contracts.canonical import file_digest
from cruxible_core.proposals.settlement import parse_change_set_record

SCHEMA_SQL = """
CREATE TABLE provider_registration_order (
 path TEXT NOT NULL, digest TEXT NOT NULL, sequence INTEGER NOT NULL,
 PRIMARY KEY(path,digest)
) STRICT;
CREATE TABLE provider_heads (
 provider_name TEXT PRIMARY KEY, identity TEXT, ambiguous INTEGER NOT NULL CHECK(ambiguous IN (0,1))
) STRICT;
CREATE TABLE provider_control_domains (
 provider_name TEXT NOT NULL, control_domain TEXT NOT NULL, build_count INTEGER NOT NULL CHECK(build_count>0),
 PRIMARY KEY(provider_name,control_domain)
) STRICT;
CREATE INDEX providers_by_registration ON providers(provider_name,registration_sequence DESC,identity) WHERE lifecycle='live';
CREATE TABLE provider_pinners (
 source_identity TEXT NOT NULL, target_identity TEXT NOT NULL, target_digest TEXT NOT NULL, provider_name TEXT NOT NULL,
 PRIMARY KEY(source_identity,target_identity,target_digest)
) STRICT;
CREATE INDEX provider_pinners_by_name ON provider_pinners(provider_name,source_identity,target_identity);
"""


def remove_provider_domain(connection: sqlite3.Connection, name: str, domain: str) -> None:
    connection.execute(
        "DELETE FROM provider_control_domains WHERE provider_name=? AND control_domain=? AND build_count=1",
        (name, domain),
    )
    connection.execute(
        "UPDATE provider_control_domains SET build_count=build_count-1 WHERE provider_name=? AND control_domain=?",
        (name, domain),
    )


def refresh_provider_heads(connection: sqlite3.Connection, sources: Mapping[str, bytes]) -> None:
    # sources is the accepted delta (the entire source only at cold construction).
    for path, content in sources.items():
        if not path.startswith("changesets/"):
            continue
        record = parse_change_set_record(content, path=path)
        for member in record.members:
            if member.artifact_kind != "provider":
                continue
            name = member.path.removeprefix("providers/").removesuffix(".json")
            digest = (
                ""
                if "/" in name
                else getattr(
                    member, "candidate_artifact_digest", getattr(member, "artifact_digest", None)
                )
            )
            if digest is None:
                continue
            connection.execute(
                "INSERT INTO provider_registration_order VALUES (?,?,?) ON CONFLICT(path,digest) "
                "DO UPDATE SET sequence=min(sequence,excluded.sequence)",
                (member.path, digest, record.sequence),
            )
    names = set()
    for path, content in sources.items():
        if not path.startswith("providers/"):
            continue
        row = connection.execute(
            "SELECT provider_name,identity,artifact_digest,control_domain FROM providers WHERE path=?",
            (path,),
        ).fetchone()
        if row is None:
            continue
        name, identity, digest, domain = row
        names.add(name)
        # A build keeps its first registration through metadata successors. A
        # historical single-path Provider orders the current version instead.
        keys = ("", "") if "/" in identity else (digest, file_digest(content).tagged)
        sequence = connection.execute(
            "SELECT min(sequence) FROM provider_registration_order WHERE path=? AND digest IN (?,?)",
            (path, *keys),
        ).fetchone()[0]
        connection.execute(
            "UPDATE providers SET registration_sequence=? WHERE identity=?",
            (0 if sequence is None else sequence, identity),
        )
        connection.execute(
            "INSERT INTO provider_control_domains VALUES (?,?,1) ON CONFLICT(provider_name,control_domain) "
            "DO UPDATE SET build_count=build_count+1",
            (name, domain),
        )
    for name in names:
        rows = connection.execute(
            "SELECT identity,registration_sequence FROM providers WHERE provider_name=? AND lifecycle='live' "
            "ORDER BY registration_sequence DESC,identity LIMIT 2",
            (name,),
        ).fetchall()
        connection.execute(
            "INSERT INTO provider_heads VALUES (?,?,?) ON CONFLICT(provider_name) DO UPDATE SET "
            "identity=excluded.identity,ambiguous=excluded.ambiguous",
            (name, rows[0][0] if rows else None, int(len(rows) == 2 and rows[0][1] == rows[1][1])),
        )
