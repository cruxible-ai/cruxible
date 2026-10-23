"""Disabled-by-upgrade and never-enabled findings derived from accepted Line version changes.

A Line whose identity is new and was never enabled owes ``line enable``: a
warning row until its first enablement or its retirement.

The durable cursor and rows commit together. A crash before commit replays only
those generations; deleting this disposable projection rebuilds from retained
versions. Warm ticks pay for new generations and changed Lines, never all Lines
or their histories. Reads query pending rows and keyed enablement lookups only.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_core.consumers.protocol import ConsumerHealth, ConsumerWork
from cruxible_core.consumers.state import DisposableState
from cruxible_core.exhaust.line_dispatch import LineDispatchStore
from cruxible_core.providers.automation_carry import parse_carry_line

_STATE = DisposableState(
    "next/line-upgrades",
    """
CREATE TABLE progress (singleton INTEGER PRIMARY KEY, generation INTEGER NOT NULL) STRICT;
INSERT INTO progress VALUES(1,0);
CREATE TABLE lines (identity TEXT PRIMARY KEY, digest TEXT NOT NULL,
 procedure TEXT NOT NULL) STRICT;
CREATE TABLE disabled (identity TEXT PRIMARY KEY, line_id TEXT NOT NULL,
 digest TEXT NOT NULL, procedure TEXT NOT NULL) STRICT;
CREATE TABLE unenabled (identity TEXT PRIMARY KEY, line_id TEXT NOT NULL) STRICT;
""",
)


def catch_up(instance: Any, *, through: int | None = None) -> None:
    """Fold only accepted version deltas after the persisted cursor.

    A cold/repair pass replays retained versions once. Blob parsing uses the
    immutable contract memo. No operational append accompanies activation.
    """

    with instance.accepted_history_reader() as history, _STATE.open(instance) as connection:
        assert connection is not None
        (cursor,) = connection.execute("SELECT generation FROM progress").fetchone()
        end = history.sequence if through is None else min(through, history.sequence)
        for sequence in range(cursor + 1, end + 1):
            versions = tuple(
                v for v in history.versions_at(sequence) if v.path.startswith("lines/")
            )
            if not versions:
                continue
            generation = history.generation(sequence)
            for version in versions:
                raw = instance.blob_at(generation.git_oid, version.path)
                if raw is None:
                    raise ValueError(f"accepted Line bytes unavailable: {version.identity}")
                line = parse_carry_line(raw, path=version.path)
                old = connection.execute(
                    "SELECT digest,procedure FROM lines WHERE identity=?", (version.identity,)
                ).fetchone()
                pending = connection.execute(
                    "SELECT line_id,digest FROM disabled WHERE identity=?", (version.identity,)
                ).fetchone()
                if pending is not None:
                    if _was_enabled(instance, *pending):
                        connection.execute(
                            "DELETE FROM disabled WHERE identity=?", (version.identity,)
                        )
                    elif old is not None and old[0] == line.lifecycle.predecessor_digest:
                        # An ordinary edit before enablement still owes the same
                        # repair, now over the current version. A previously enabled
                        # ancestor closes the row even when reads missed that arm.
                        connection.execute(
                            "UPDATE disabled SET digest=? WHERE identity=?",
                            (version.artifact_digest, version.identity),
                        )
                line_id = line_identity_digest(line.identity)
                if line.lifecycle.state == "retired" or _ever_enabled(instance, line_id):
                    connection.execute(
                        "DELETE FROM unenabled WHERE identity=?", (version.identity,)
                    )
                elif old is None:
                    # A new Line identity starts disabled: name `line enable` until
                    # its first enablement (or retirement) closes the row.
                    connection.execute(
                        "INSERT OR REPLACE INTO unenabled VALUES(?,?)", (version.identity, line_id)
                    )
                if line.lifecycle.state == "retired":
                    connection.execute("DELETE FROM disabled WHERE identity=?", (version.identity,))
                elif (
                    old is not None
                    and old[0] == line.lifecycle.predecessor_digest
                    and old[1] != line.procedure.artifact_digest
                ):
                    connection.execute(
                        "INSERT OR REPLACE INTO disabled VALUES(?,?,?,?)",
                        (
                            version.identity,
                            line_id,
                            version.artifact_digest,
                            line.procedure.artifact_digest,
                        ),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO lines VALUES(?,?,?)",
                    (version.identity, version.artifact_digest, line.procedure.artifact_digest),
                )
        connection.execute("UPDATE progress SET generation=?", (end,))


def _ever_enabled(instance: Any, line_id: str) -> bool:
    """Whether any version of the Line was ever enabled: a keyed probe of its sessions."""

    with LineDispatchStore(instance).locked() as dispatch:
        return (
            dispatch.execute(
                "SELECT 1 FROM sessions WHERE line_id=? LIMIT 1", (line_id,)
            ).fetchone()
            is not None
        )


def unenabled_lines(instance: Any) -> tuple[str, ...]:
    """Accepted Lines never enabled since they were added; a first enablement closes each."""

    catch_up(instance)
    with _STATE.open(instance) as connection:
        assert connection is not None
        rows = connection.execute(
            "SELECT identity,line_id FROM unenabled ORDER BY identity"
        ).fetchall()
        result = []
        for identity, line_id in rows:
            if _ever_enabled(instance, line_id):
                connection.execute("DELETE FROM unenabled WHERE identity=?", (identity,))
            else:
                result.append(str(identity))
        return tuple(result)


def _was_enabled(instance: Any, line_id: str, digest: str) -> bool:
    with LineDispatchStore(instance).locked() as dispatch:
        return (
            dispatch.execute(
                "SELECT 1 FROM sessions WHERE line_id=? "
                "AND json_extract(payload,'$.line_artifact_digest')=? LIMIT 1",
                (line_id, digest),
            ).fetchone()
            is not None
        )


def disabled_upgrades(instance: Any) -> tuple[tuple[str, str], ...]:
    """One pending disabled upgrade per Line; enabling its version closes it forever."""

    catch_up(instance)
    with _STATE.open(instance) as connection:
        assert connection is not None
        rows = connection.execute(
            "SELECT identity,line_id,digest,procedure FROM disabled ORDER BY identity"
        ).fetchall()
        if not rows:
            return ()
        result = []
        with LineDispatchStore(instance).locked() as dispatch:
            for identity, line_id, digest, procedure in rows:
                enabled = dispatch.execute(
                    "SELECT 1 FROM sessions WHERE line_id=? "
                    "AND json_extract(payload,'$.line_artifact_digest')=? LIMIT 1",
                    (line_id, digest),
                ).fetchone()
                if enabled is not None:
                    connection.execute("DELETE FROM disabled WHERE identity=?", (identity,))
                else:
                    result.append((identity, procedure))
        return tuple(result)


class LineUpgradesPart:
    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        catch_up(instance)

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        return ()

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        return None

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            (cursor,) = connection.execute("SELECT generation FROM progress").fetchone()
        with instance.accepted_history_reader() as history:
            head = history.sequence
        return (
            ConsumerHealth(
                kind="next",
                consumer_id="consumer:next",
                state="running" if cursor == head else "lagging",
                detail={"generation": cursor},
            ),
        )


_PART = LineUpgradesPart()
