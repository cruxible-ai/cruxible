"""Provider builds: several live per name, and which one a reference names.

A provider name holds any number of builds, each its own artifact
(``Provider:<name>/<build key>``); a single-path Provider (the built-in, or one
registered before builds) is one more build of its name. Pins name exact builds.
Only new authoring and unpinned references -- a Blueprint slot bound by name, a
source selection, ``provider move --to newest`` -- resolve a bare name, to the
most recently registered live build: the one first accepted last. Later
signing-key or capture-declaration successors do not re-register a build.
Interface registrations resolve the same way, per interface id.

Inventory and authoring resolutions read maintained rows and registration order.
Installation and move use the maintained name head instead, without enumerating
retained builds; move selects only live reverse pinners from its name index.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_installation import (
    ProviderBuildResolutionV1,
    ProviderBuildSummaryV1,
)
from cruxible_client.contracts.provider_interfaces import (
    provider_interface_id_of,
)
from cruxible_client.contracts.providers import (
    ProviderFormatError,
    parse_provider,
    parse_provider_build_name,
    provider_builds_prefix,
    provider_digest,
    provider_path,
)
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.derived.derived_state import paths_with_prefix
from cruxible_core.errors import RequestRefusedError


@dataclass(frozen=True)
class BuildRow:
    """One build of a provider name (or one registration of an interface id)."""

    identity: str
    path: str
    artifact_digest: str
    live: bool
    version: str | None = None
    registration_sequence: int = 0


def tree_provider_builds(tree: Mapping[str, bytes], name: str) -> tuple[BuildRow, ...]:
    """Every build of ``name`` in ``tree``: its prefix range plus its single path."""

    rows = []
    for path in (*paths_with_prefix(tree, provider_builds_prefix(name)), provider_path(name)):
        content = tree.get(path)
        if content is None:
            continue
        provider = parse_provider(content, path=path)
        runtime = getattr(provider, "runtime_artifact", None)
        rows.append(
            BuildRow(
                identity=provider.identity.qualified,
                path=path,
                artifact_digest=provider_digest(provider).tagged,
                live=provider.lifecycle.state == "live",
                version=None if runtime is None else runtime.distribution.version,
            )
        )
    return tuple(rows)


def indexed_provider_builds(connection: sqlite3.Connection, name: str) -> tuple[BuildRow, ...]:
    """Every build of ``name`` from the typed index (``providers_by_name``)."""

    return tuple(
        BuildRow(str(identity), str(path), str(digest), lifecycle == "live", version, sequence)
        for identity, path, digest, lifecycle, version, sequence in connection.execute(
            "SELECT identity,path,artifact_digest,lifecycle,distribution_version,"
            "registration_sequence FROM "
            "main.providers WHERE provider_name=? ORDER BY identity",
            (name,),
        )
    )


def indexed_newest_provider(connection: sqlite3.Connection, name: str) -> BuildRow | None:
    """One maintained name head; no build or accepted-history enumeration."""
    head = connection.execute(
        "SELECT identity,ambiguous FROM main.provider_heads WHERE provider_name=?", (name,)
    ).fetchone()
    if head is None or head[0] is None:
        return None
    if head[1]:
        raise RequestRefusedError(
            "cruxible.provider.build_ambiguous",
            "several live builds were registered together, so none is the most recent; "
            "name the exact build",
            repair=RepairOperation(operation="cruxible.provider.list"),
        )
    return indexed_exact_provider(connection, str(head[0]))


def indexed_exact_provider(connection: sqlite3.Connection, identity: str) -> BuildRow | None:
    row = connection.execute(
        "SELECT identity,path,artifact_digest,lifecycle,distribution_version,"
        "registration_sequence FROM main.providers "
        "WHERE identity=?",
        (identity,),
    ).fetchone()
    return (
        None if row is None else BuildRow(row[0], row[1], row[2], row[3] == "live", row[4], row[5])
    )


def indexed_move_pinners(
    connection: sqlite3.Connection,
    name: str,
    target: str,
    only: tuple[str, ...],
    *,
    procedures_only: bool = False,
) -> tuple[tuple[str, str, str], ...]:
    """Live Procedure/Blueprint pins of this name, sized by the affected pinners.

    The derived relation excludes retired sources and has a name prefix index;
    builds without pinners never enter this read.
    """
    select = (
        "SELECT p.source_identity,coalesce(proc.path,bp.path),p.target_identity "
        "FROM main.provider_pinners p "
        "LEFT JOIN main.procedures proc ON proc.identity=p.source_identity "
        "LEFT JOIN main.blueprints bp ON bp.identity=p.source_identity "
        "JOIN main.providers b ON b.identity=p.target_identity "
        "AND b.artifact_digest=p.target_digest "
        "WHERE p.provider_name=? AND p.target_identity!=? AND b.lifecycle='live'"
    )
    if procedures_only:
        select += " AND proc.identity IS NOT NULL"
    parameters: list[str] = [name, target]
    if only:
        select += " AND p.source_identity IN (" + ",".join("?" for _ in only) + ")"
        parameters.extend(only)
    return tuple(
        connection.execute(select + " ORDER BY p.source_identity,p.target_identity", parameters)
    )


def indexed_interface_registrations(
    connection: sqlite3.Connection, interface_id: str
) -> tuple[BuildRow, ...]:
    """Every registration of ``interface_id`` from the typed index."""

    return tuple(
        BuildRow(
            str(identity),
            str(path),
            str(digest),
            lifecycle == "live",
            registration_sequence=sequence,
        )
        for identity, path, digest, lifecycle, sequence in connection.execute(
            "SELECT identity,path,artifact_digest,lifecycle,registration_sequence "
            "FROM main.provider_interfaces "
            "WHERE interface_id=? ORDER BY identity",
            (interface_id,),
        )
    )


def live_pinners(
    connection: sqlite3.Connection, identity: str, artifact_digest: str
) -> tuple[str, ...]:
    """The live definitions pinning this exact version (``pins_by_target``)."""

    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT p.source_identity FROM main.pins p "
            "JOIN main.artifact_lookup a ON a.identity=p.source_identity "
            "WHERE p.target_identity=? AND p.target_digest=? AND p.edge_kind='required_pin' "
            "AND a.lifecycle='live' ORDER BY p.source_identity",
            (identity, artifact_digest),
        )
    )


def _summaries(
    rows: Sequence[BuildRow],
    sequences: Mapping[str, int],
    newest: str | None,
    pinned_by: Callable[[BuildRow], int] | None,
) -> tuple[ProviderBuildSummaryV1, ...]:
    return tuple(
        ProviderBuildSummaryV1(
            build=row.identity.partition(":")[2],
            version=row.version,
            state="live" if row.live else "retired",
            newest=row.identity == newest,
            pinned_by=0 if pinned_by is None else pinned_by(row),
        )
        for row in sorted(rows, key=lambda item: (-sequences.get(item.identity, 0), item.identity))
    )


def newest_live(rows: Sequence[BuildRow], sequences: Mapping[str, int]) -> BuildRow | None:
    """The live row registered last, or None; a tie refuses (``build_ambiguous``)."""

    live = [row for row in rows if row.live]
    if not live:
        return None
    last = max(sequences.get(row.identity, 0) for row in live)
    newest = [row for row in live if sequences.get(row.identity, 0) == last]
    if len(newest) > 1:
        raise RequestRefusedError(
            "cruxible.provider.build_ambiguous",
            "several live builds were registered together ("
            + ", ".join(sorted(row.identity for row in newest))
            + "), so none is the most recent; name the exact build",
            repair=RepairOperation(operation="cruxible.provider.list"),
        )
    return newest[0]


def resolve_provider_build(
    requested: str,
    *,
    rows: Sequence[BuildRow],
    sequences: Mapping[str, int],
    coordinate: AcceptedCoordinate,
    pinned_by: Callable[[BuildRow], int] | None = None,
) -> ProviderBuildResolutionV1:
    """Which build ``requested`` names: itself when it names one exactly, otherwise
    the most recently registered live build of the name.

    ``requested`` is a provider identity name: ``name`` or ``name/<build key>``.
    """

    try:
        parsed = parse_provider_build_name(requested.removeprefix("Provider:"))
    except ProviderFormatError as exc:
        raise RequestRefusedError(
            "cruxible.provider.unknown",
            f"{requested!r} is not a provider name or build",
            repair=RepairOperation(operation="cruxible.provider.list"),
        ) from exc
    exact = None
    newest = None
    if parsed.build is not None:
        exact = next(
            (row for row in rows if row.identity == f"Provider:{parsed.identity_name}"), None
        )
        if exact is None:
            raise RequestRefusedError(
                "cruxible.provider.unknown",
                f"no build {parsed.identity_name} is installed here",
                repair=RepairOperation(operation="cruxible.provider.list"),
            )
        if not exact.live:
            raise RequestRefusedError(
                "cruxible.provider.build_retired",
                f"build {parsed.identity_name} is retired; new pins name a live build",
                repair=RepairOperation(operation="cruxible.provider.list"),
            )
        chosen = exact
    else:
        newest = newest_live(rows, sequences)
        if newest is None:
            raise RequestRefusedError(
                "cruxible.provider.no_live_build",
                f"no live build of {parsed.name} is installed here",
                repair=RepairOperation(operation="cruxible.provider.list"),
            )
        chosen = newest
    return ProviderBuildResolutionV1(
        requested=requested,
        rule="exact" if exact is not None else "most_recently_registered",
        resolved=chosen.identity.partition(":")[2],
        artifact_digest=chosen.artifact_digest,
        version=chosen.version,
        considered=_summaries(
            rows if exact is None else (exact,),
            sequences,
            None if newest is None else newest.identity,
            pinned_by,
        ),
        coordinate=coordinate,
    )


class BuildResolver:
    """Resolve bare provider names and interface ids for one authoring base.

    ``provider_rows`` and ``interface_rows`` read one name's (or interface id's)
    rows; registration sequences are maintained by accepted deltas. Exact references
    seek one row without enumerating sibling builds. Every provider resolution made is
    kept in ``resolutions``, in request order.
    """

    def __init__(
        self,
        *,
        provider_rows: Callable[[str], Sequence[BuildRow]],
        interface_rows: Callable[[str], Sequence[BuildRow]],
        exact_provider: Callable[[str], BuildRow | None],
        coordinate: AcceptedCoordinate,
    ) -> None:
        self._provider_rows = provider_rows
        self._interface_rows = interface_rows
        self._exact_provider = exact_provider
        self._coordinate = coordinate
        self.resolutions: list[ProviderBuildResolutionV1] = []

    def _sequences(self, rows: Sequence[BuildRow]) -> dict[str, int]:
        return {row.identity: row.registration_sequence for row in rows}

    def provider(self, requested: str) -> ProviderBuildResolutionV1:
        """The build a provider reference names (exact, or most recently registered)."""

        name = requested.removeprefix("Provider:")
        rows: Sequence[BuildRow]
        if "/" in name:
            exact = self._exact_provider("Provider:" + name)
            rows = () if exact is None else (exact,)
        else:
            rows = self._provider_rows(name)
        resolution = resolve_provider_build(
            name,
            rows=rows,
            sequences={row.identity: 0 for row in rows} if "/" in name else self._sequences(rows),
            coordinate=self._coordinate,
        )
        if resolution not in self.resolutions:
            self.resolutions.append(resolution)
        return resolution

    def interface(self, requested: str) -> str:
        """The registration identity a bare interface id names: its most recently
        registered live registration. An exact name (``id/<key>``) is itself."""

        name = requested.removeprefix("ProviderInterface:")
        if "/" in name:
            return f"ProviderInterface:{name}"
        rows = self._interface_rows(provider_interface_id_of(name))
        newest = newest_live(rows, self._sequences(rows))
        return f"ProviderInterface:{name}" if newest is None else newest.identity

    def resolve(self, kind: str, name: str) -> str:
        """The exact identity a ``kind:name`` reference names (``resolve_source``)."""

        if kind == "Provider":
            return "Provider:" + self.provider(name).resolved
        return self.interface(name)


def pinned_interface(provider: Any, interface_id: str) -> str | None:
    """The registration identity ``provider`` binds for ``interface_id``, if any."""

    from cruxible_client.contracts.providers import provider_interface_pin

    pin = provider_interface_pin(provider, provider_interface_id_of(interface_id))
    return None if pin is None else pin.target.qualified


def names_of(rows: Iterable[BuildRow]) -> tuple[str, ...]:
    return tuple(row.identity.partition(":")[2] for row in rows)


def resolve_in_tree(
    instance: Any,
    coordinate: Any,
    tree: Mapping[str, bytes],
    requested: str,
) -> ProviderBuildResolutionV1:
    """Resolve a provider reference against one authoring tree at ``coordinate``.

    The coordinate-bound projection supplies maintained registration order.
    """

    name = requested.removeprefix("Provider:")
    try:
        base = parse_provider_build_name(name).name
    except ProviderFormatError as exc:
        raise RequestRefusedError(
            "cruxible.provider.unknown",
            f"{requested!r} is not a provider name or build",
            repair=RepairOperation(operation="cruxible.provider.list"),
        ) from exc
    served = AcceptedCoordinate.from_internal(coordinate)
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        rows: Sequence[BuildRow]
        if "/" in name:
            exact = indexed_exact_provider(connection, "Provider:" + name)
            rows = () if exact is None else (exact,)
        else:
            rows = indexed_provider_builds(connection, base)
        return resolve_provider_build(
            name,
            rows=rows,
            sequences={row.identity: row.registration_sequence for row in rows},
            coordinate=served,
        )
