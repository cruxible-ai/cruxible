"""Provider name summaries maintained from accepted changed owners and records.

Only cold construction reads all retained records. A successor consumes its new
records and changed providers; indexed top-two seeks maintain newest selection
and ambiguity without enumerating a name's retained builds.
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from cruxible_client.contracts.canonical import file_digest
from cruxible_client.contracts.provider_interfaces import (
    parse_provider_interface,
    provider_interface_contract,
    provider_interface_digest,
)
from cruxible_client.contracts.providers import parse_provider, provider_build_key, provider_digest
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


def registration_key(content: bytes, path: str) -> str:
    """Stable runtime identity, unaffected by signing/capture metadata successors."""
    if path.startswith("provider-interfaces/"):
        registration = parse_provider_interface(content, path=path)
        return provider_interface_contract(
            interface_id=registration.interface_id,
            interface_bytes_hex=registration.interface_bytes_hex,
            interface_digest_domain=registration.interface_digest_domain,
            interface_digest=registration.interface_digest,
            effect_class=registration.effect_class,
        ).identity.name.partition("/")[2]
    provider = parse_provider(content, path=path)
    runtime = getattr(provider, "runtime_artifact", None)
    return "" if runtime is None else provider_build_key(runtime).tagged


def cold_provider_source(
    repository: Any, head_oid: str, sources: Mapping[str, bytes]
) -> Callable[[str, int], bytes]:
    """Cold reconstruction only: locate exact historical legacy runtime bytes.

    This one walk is O(retained generations); ordinary deltas supply their own bytes.
    """
    records = [
        parse_change_set_record(raw, path=path)
        for path, raw in sources.items()
        if path.startswith("changesets/")
    ]
    latest = max((record.sequence for record in records), default=0)
    coordinates = {latest: head_oid}
    oldest = latest

    def read(path: str, sequence: int) -> bytes:
        nonlocal oldest
        while sequence not in coordinates:
            coordinates[oldest - 1] = repository.parent_of(coordinates[oldest])
            oldest -= 1
        raw = repository.blob_at(coordinates[sequence], path)
        if raw is None:
            raise ValueError(f"historical Provider {path} at {sequence} is absent")
        return raw

    return read


def refresh_provider_heads(
    connection: sqlite3.Connection,
    sources: Mapping[str, bytes],
    *,
    historical_source: Callable[[str, int], bytes] | None = None,
) -> None:
    # sources is the accepted delta (the entire source only at cold construction).
    for path, content in sources.items():
        if not path.startswith("changesets/"):
            continue
        record = parse_change_set_record(content, path=path)
        for member in record.members:
            if member.artifact_kind not in ("provider", "provider-interface"):
                continue
            name = member.path.partition("/")[2].removesuffix(".json")
            digest = ""
            if "/" not in name:
                raw = sources.get(member.path)
                commitment = getattr(member, "candidate_artifact_digest", None)
                legacy = getattr(member, "artifact_digest", None)

                def artifact_digest(raw: bytes) -> str:
                    if member.artifact_kind == "provider":
                        return provider_digest(parse_provider(raw, path=member.path)).tagged
                    return provider_interface_digest(
                        parse_provider_interface(raw, path=member.path)
                    ).tagged

                matches = raw is not None and (
                    artifact_digest(raw) == commitment
                    if commitment is not None
                    else file_digest(raw).tagged == legacy
                )
                if not matches:
                    if historical_source is None:
                        raise ValueError(
                            "cold legacy registration reconstruction needs historical bytes"
                        )
                    raw = historical_source(member.path, record.sequence)
                assert raw is not None
                digest = registration_key(raw, member.path)
            connection.execute(
                "INSERT INTO provider_registration_order VALUES (?,?,?) ON CONFLICT(path,digest) "
                "DO UPDATE SET sequence=min(sequence,excluded.sequence)",
                (member.path, digest, record.sequence),
            )
    for path, content in sources.items():
        if not path.startswith("provider-interfaces/"):
            continue
        name = path.partition("/")[2].removesuffix(".json")
        key = "" if "/" in name else registration_key(content, path)
        sequence = connection.execute(
            "SELECT sequence FROM provider_registration_order WHERE path=? AND digest=?",
            (path, key),
        ).fetchone()
        connection.execute(
            "UPDATE provider_interfaces SET registration_sequence=? WHERE path=?",
            (0 if sequence is None else sequence[0], path),
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
        # Metadata successors keep the first acceptance of this stable runtime.
        key = "" if "/" in identity else registration_key(content, path)
        sequence = connection.execute(
            "SELECT min(sequence) FROM provider_registration_order WHERE path=? AND digest=?",
            (path, key),
        ).fetchone()[0]
        if sequence is None:
            connection.execute(
                "INSERT INTO provider_registration_order VALUES (?,?,0)", (path, key)
            )
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
