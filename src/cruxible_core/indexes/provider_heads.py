"""Provider name summaries maintained from accepted changed owners and records.

Only cold construction reads all retained records. A successor consumes its new
records and changed providers; indexed top-two seeks maintain newest selection
and ambiguity without enumerating a name's retained builds.
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping

from cruxible_client.contracts.canonical import file_digest
from cruxible_client.contracts.provider_interfaces import (
    ProviderInterfaceContract,
    parse_provider_interface,
    provider_interface_contract_key,
    provider_interface_digest,
)
from cruxible_client.contracts.providers import parse_provider, provider_build_key, provider_digest
from cruxible_core.indexes.sql_batch import execute_values, insert_rows, select_in
from cruxible_core.ledger.git import _proven_blob_entries
from cruxible_core.ledger.protocols import LedgerRepositoryProtocol
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


def remove_provider_domains(connection: sqlite3.Connection, identities: list[str]) -> None:
    """Subtract only replaced builds, aggregating domains before chunking SQL."""
    counts = Counter(
        (name, domain)
        for name, domain in select_in(
            connection,
            "SELECT provider_name,control_domain FROM providers WHERE identity IN ({})",
            identities,
        )
    )
    removed = [(name, domain, count) for (name, domain), count in counts.items()]
    relation = "WITH removed(provider_name,control_domain,n) AS (VALUES {}) "
    match = (
        "SELECT n FROM removed WHERE removed.provider_name=provider_control_domains.provider_name "
        "AND removed.control_domain=provider_control_domains.control_domain"
    )
    execute_values(
        connection,
        relation + "DELETE FROM provider_control_domains WHERE (provider_name,control_domain) "
        f"IN (SELECT provider_name,control_domain FROM removed) AND build_count=({match})",
        removed,
    )
    execute_values(
        connection,
        relation + f"UPDATE provider_control_domains SET build_count=build_count-({match}) "
        "WHERE (provider_name,control_domain) IN (SELECT provider_name,control_domain FROM removed)",
        removed,
    )


def registration_key(content: bytes, path: str) -> str:
    """Stable runtime identity, unaffected by signing/capture metadata successors."""
    if path.startswith("provider-interfaces/"):
        registration = parse_provider_interface(content, path=path)
        if isinstance(registration, ProviderInterfaceContract):
            return f"v{registration.interface.version}"
        return provider_interface_contract_key(
            interface_id=registration.interface_id,
            interface_digest_domain=registration.interface_digest_domain,
            interface_digest=registration.interface_digest,
            effect_class=registration.effect_class,
        ).value

    provider = parse_provider(content, path=path)
    runtime = getattr(provider, "runtime_artifact", None)
    return "" if runtime is None else provider_build_key(runtime).tagged


def cold_provider_source(
    repository: LedgerRepositoryProtocol, head_oid: str, sources: Mapping[str, bytes]
) -> Callable[[str, int], bytes | None]:
    """Cold reconstruction only: locate exact historical legacy runtime bytes.

    Walk ancestry and list each needed historical tree once, only on cold
    reconstruction; ordinary deltas supply their own bytes.
    """
    records = [
        parse_change_set_record(raw, path=path)
        for path, raw in sources.items()
        if path.startswith("changesets/")
    ]
    latest = max((record.sequence for record in records), default=0)
    coordinates: dict[int, str | None] = {latest: head_oid}
    oldest = latest
    historical_entries: dict[str, dict[str, str]] = {}

    def read(path: str, sequence: int) -> bytes | None:
        nonlocal oldest
        if sequence == latest:
            return sources.get(path)
        while sequence not in coordinates:
            oid = coordinates[oldest]
            if oid is None:
                return None
            coordinates[oldest - 1] = repository.parent_of(oid)
            oldest -= 1
        oid = coordinates[sequence]
        if oid is None:
            return None
        # Only cold reconstruction needs historical listings. Cache each once;
        # use the repository protocol rather than assuming an on-disk Git ledger.
        if oid not in historical_entries:
            historical_entries[oid] = {
                entry.path: entry.oid for entry in _proven_blob_entries(repository.list_tree(oid))
            }
        blob_oid = historical_entries[oid].get(path)
        return None if blob_oid is None else repository.read_blob(blob_oid)

    return read


def refresh_provider_heads(
    connection: sqlite3.Connection,
    sources: Mapping[str, bytes],
    *,
    historical_source: Callable[[str, int], bytes | None] | None = None,
) -> None:
    # sources is the accepted delta (the entire source only at cold construction).
    registrations: dict[tuple[str, str], int] = {}

    def register(path: str, digest: str, sequence: int) -> None:
        key = (path, digest)
        registrations[key] = min(registrations.get(key, sequence), sequence)

    if historical_source is not None:
        # Genesis has no change record. Seed its legacy runtime/contract keys
        # before folding recorded occurrences, so metadata cannot re-register it.
        for path in sources:
            if path.count("/") != 1 or not path.startswith(("providers/", "provider-interfaces/")):
                continue
            original = historical_source(path, 0)
            if original is not None:
                register(path, registration_key(original, path), 0)
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
                if raw is None:
                    raise ValueError(
                        f"historical registration {member.path} at {record.sequence} is absent"
                    )
                digest = registration_key(raw, member.path)
            register(member.path, digest, record.sequence)
    interfaces = []
    for path, content in sources.items():
        if not path.startswith("provider-interfaces/"):
            continue
        name = path.partition("/")[2].removesuffix(".json")
        key = "" if "/" in name else registration_key(content, path)
        interfaces.append((path, key))
    provider_paths = [path for path in sources if path.startswith("providers/")]
    by_path = {
        row[0]: row
        for row in select_in(
            connection,
            "SELECT path,provider_name,identity,control_domain FROM providers WHERE path IN ({})",
            provider_paths,
        )
    }
    providers = [by_path[path] for path in provider_paths if path in by_path]
    keys = list(
        dict.fromkeys(
            interfaces
            + [
                (path, "" if "/" in identity else registration_key(sources[path], path))
                for path, _name, identity, _domain in providers
            ]
        )
    )
    sequences = {
        (path, digest): sequence
        for path, digest, sequence in execute_values(
            connection,
            "WITH wanted(path,digest) AS (VALUES {}) "
            "SELECT r.path,r.digest,r.sequence FROM wanted w "
            "JOIN provider_registration_order r ON r.path=w.path AND r.digest=w.digest",
            keys,
        )
    }
    for key in keys:
        if key in registrations:
            sequences[key] = min(sequences.get(key, registrations[key]), registrations[key])
        elif key not in sequences:
            registrations[key] = sequences[key] = 0
    insert_rows(
        connection,
        "provider_registration_order",
        [(path, digest, sequence) for (path, digest), sequence in registrations.items()],
        suffix=" ON CONFLICT(path,digest) DO UPDATE SET sequence=min(sequence,excluded.sequence)",
    )
    # Only changed owners are updated. Exact (path, runtime-key) seeks above
    # never enumerate a legacy path's retained registrations.
    by_key = {path: key for path, key in keys}
    for table, paths in (
        ("provider_interfaces", [path for path, _key in interfaces]),
        ("providers", [path for path, _name, _identity, _domain in providers]),
    ):
        execute_values(
            connection,
            "WITH changed(path,sequence) AS (VALUES {}) "
            f"UPDATE {table} SET registration_sequence=(SELECT sequence FROM changed "
            f"WHERE changed.path={table}.path) WHERE path IN (SELECT path FROM changed)",
            [(path, sequences.get((path, by_key[path]), 0)) for path in paths],
        )
    counts = Counter((name, domain) for _path, name, _identity, domain in providers)
    insert_rows(
        connection,
        "provider_control_domains",
        [(name, domain, count) for (name, domain), count in counts.items()],
        suffix=" ON CONFLICT(provider_name,control_domain) DO UPDATE SET "
        "build_count=build_count+excluded.build_count",
    )
    names = list(dict.fromkeys(name for _path, name, _identity, _domain in providers))
    # Indexed top-two lookups per changed name, regardless of retained build count.
    heads = execute_values(
        connection,
        "WITH changed(provider_name) AS (VALUES {}), selected AS ("
        "SELECT provider_name,"
        "(SELECT identity FROM providers p WHERE p.provider_name=changed.provider_name "
        "AND lifecycle='live' ORDER BY registration_sequence DESC,identity LIMIT 1) AS identity,"
        "(SELECT registration_sequence FROM providers p WHERE p.provider_name=changed.provider_name "
        "AND lifecycle='live' ORDER BY registration_sequence DESC,identity LIMIT 1) AS first,"
        "(SELECT registration_sequence FROM providers p WHERE p.provider_name=changed.provider_name "
        "AND lifecycle='live' ORDER BY registration_sequence DESC,identity LIMIT 1 OFFSET 1) AS second "
        "FROM changed) SELECT provider_name,identity,coalesce(first=second,0) FROM selected",
        [(name,) for name in names],
    )
    insert_rows(
        connection,
        "provider_heads",
        heads,
        suffix=" ON CONFLICT(provider_name) DO UPDATE SET "
        "identity=excluded.identity,ambiguous=excluded.ambiguous",
    )
