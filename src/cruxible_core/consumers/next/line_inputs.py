"""The workspace files live Lines read, indexed as their definitions are accepted.

A Line whose Procedure reads a workspace file (the built-in ``workspace.file``
read of one path) needs that file there before it can run. This fold keeps,
per live Line, each such input: the path resolved from the Line's parameters,
the parameter it comes from, the author's description of it, and the byte
budget its CaptureContract declares. It also keeps which Lines each live
Runbook names as steps, since a Runbook step runs a Line that need not be
enabled.

The fold reads only accepted version deltas after its cursor: each changed
Line costs its own, its Procedure's, its params Contract's and its
CaptureContract's bytes at that generation; each changed Runbook its own. It
never reads all Lines or any history. ``next`` then stats each indexed input
of an enabled or Runbook-named Line once, with the daemon's own workspace
reader: no workspace scan, no run history.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cruxible_client.contracts.captures import capture_contract_path, parse_capture_contract
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_client.contracts.runbooks import parse_runbook
from cruxible_client.contracts.shapes import Contract, contract_path_for_identity, parse_contract
from cruxible_core.consumers.protocol import ConsumerHealth, ConsumerWork
from cruxible_core.consumers.state import DisposableState
from cruxible_core.exhaust.line_dispatch import LineDispatchStore
from cruxible_core.procedures.declared_inputs import declared_inputs, parameter_description
from cruxible_core.providers.automation_carry import parse_carry_line

_STATE = DisposableState(
    "next/line-inputs",
    """
CREATE TABLE progress (singleton INTEGER PRIMARY KEY, generation INTEGER NOT NULL) STRICT;
INSERT INTO progress VALUES(1,0);
CREATE TABLE inputs (line TEXT NOT NULL, node_id TEXT NOT NULL, line_id TEXT NOT NULL,
 relative_path TEXT NOT NULL, workspace TEXT, parameter TEXT, description TEXT,
 max_bytes INTEGER, PRIMARY KEY(line,node_id)) STRICT;
CREATE TABLE runbook_lines (runbook TEXT NOT NULL, line TEXT NOT NULL,
 PRIMARY KEY(runbook,line)) STRICT;
CREATE INDEX runbook_lines_by_line ON runbook_lines(line);
""",
)


@dataclass(frozen=True)
class LineWorkspaceInput:
    """One workspace file a live Line reads, as its accepted definitions declare it."""

    line: str
    node_id: str
    relative_path: str
    workspace: str | None
    parameter: str | None
    description: str | None
    max_bytes: int | None


def _blob(instance: Any, git_oid: str, path: str) -> bytes | None:
    try:
        return instance.blob_at(git_oid, path)  # type: ignore[no-any-return]
    except (KeyError, OSError, ValueError):
        return None


def _line_inputs(instance: Any, git_oid: str, raw: bytes, path: str) -> list[tuple[Any, ...]]:
    """The rows one accepted Line version contributes: its workspace-file inputs."""

    line = parse_carry_line(raw, path=path)
    if line.lifecycle.state != "live":
        return []
    procedure_bytes = _blob(instance, git_oid, procedure_path(line.procedure.target.name))
    if procedure_bytes is None:
        return []
    procedure = parse_procedure(procedure_bytes, path=procedure_path(line.procedure.target.name))
    if procedure_artifact_digest(procedure).tagged != line.procedure.artifact_digest:
        # Not the version the Line pins: the Line cannot run as accepted here.
        return []
    definition = procedure.definition
    wanted = [
        item
        for item in declared_inputs(definition, line)
        if item.kind == "workspace_file" and item.location is not None
    ]
    if not wanted:
        return []
    contract: Contract | None = None
    if definition.parameter_contract is not None:
        contract_path = contract_path_for_identity(definition.parameter_contract.target.name)
        contract_bytes = _blob(instance, git_oid, contract_path)
        if contract_bytes is not None:
            contract = parse_contract(contract_bytes, path=contract_path)
    line_id = line_identity_digest(line.identity)
    rows = []
    for item in wanted:
        max_bytes: int | None = None
        if item.capture_contract is not None:
            capture_path = capture_contract_path(item.capture_contract)
            capture_bytes = _blob(instance, git_oid, capture_path)
            if capture_bytes is not None:
                capture = parse_capture_contract(capture_bytes, path=capture_path)
                budget = getattr(capture, "selection_budget", None)
                max_bytes = None if budget is None else budget.max_bytes
        rows.append(
            (
                line.identity.qualified,
                item.node_id,
                line_id,
                item.location,
                item.workspace,
                item.parameter,
                parameter_description(contract, item.parameter),
                max_bytes,
            )
        )
    return rows


def catch_up(instance: Any, *, through: int | None = None) -> None:
    """Fold only the Line and Runbook versions accepted after the persisted cursor."""

    with instance.accepted_history_reader() as history, _STATE.open(instance) as connection:
        assert connection is not None
        (cursor,) = connection.execute("SELECT generation FROM progress").fetchone()
        end = history.sequence if through is None else min(through, history.sequence)
        for sequence in range(cursor + 1, end + 1):
            versions = tuple(
                v
                for v in history.versions_at(sequence)
                if v.path.startswith(("lines/", "runbooks/"))
            )
            if not versions:
                continue
            git_oid = history.generation(sequence).git_oid
            for version in versions:
                raw = instance.blob_at(git_oid, version.path)
                if raw is None:
                    raise ValueError(f"accepted bytes unavailable: {version.identity}")
                if version.path.startswith("runbooks/"):
                    runbook = parse_runbook(raw, path=version.path)
                    connection.execute(
                        "DELETE FROM runbook_lines WHERE runbook=?", (version.identity,)
                    )
                    if runbook.lifecycle.state == "live":
                        connection.executemany(
                            "INSERT OR IGNORE INTO runbook_lines VALUES(?,?)",
                            [(version.identity, line.qualified) for line in runbook.lines],
                        )
                    continue
                connection.execute("DELETE FROM inputs WHERE line=?", (version.identity,))
                connection.executemany(
                    "INSERT OR REPLACE INTO inputs VALUES(?,?,?,?,?,?,?,?)",
                    _line_inputs(instance, git_oid, raw, version.path),
                )
        connection.execute("UPDATE progress SET generation=?", (end,))


def _enabled(dispatch: Any, line_id: str) -> bool:
    return (
        dispatch.execute(
            "SELECT 1 FROM sessions WHERE line_id=? AND active=1 LIMIT 1", (line_id,)
        ).fetchone()
        is not None
    )


def needed_workspace_inputs(instance: Any) -> tuple[LineWorkspaceInput, ...]:
    """Each indexed input of a Line that is enabled or a step of a live Runbook.

    After a catch-up over new generations: one keyed Runbook lookup and at
    most one enablement probe per indexed Line.
    """

    catch_up(instance)
    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return ()
        rows = connection.execute(
            "SELECT line,node_id,line_id,relative_path,workspace,parameter,description,max_bytes "
            "FROM inputs ORDER BY line,node_id"
        ).fetchall()
        if not rows:
            return ()
        in_runbook = {
            str(line)
            for line in {row[0] for row in rows}
            if connection.execute(
                "SELECT 1 FROM runbook_lines INDEXED BY runbook_lines_by_line WHERE line=? LIMIT 1",
                (line,),
            ).fetchone()
            is not None
        }
    found = []
    with LineDispatchStore(instance).locked() as dispatch:
        for line, node_id, line_id, path, workspace, parameter, description, max_bytes in rows:
            if line not in in_runbook and not _enabled(dispatch, str(line_id)):
                continue
            found.append(
                LineWorkspaceInput(
                    line=str(line),
                    node_id=str(node_id),
                    relative_path=str(path),
                    workspace=None if workspace is None else str(workspace),
                    parameter=None if parameter is None else str(parameter),
                    description=None if description is None else str(description),
                    max_bytes=None if max_bytes is None else int(max_bytes),
                )
            )
    return tuple(found)


class LineInputsPart:
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


_PART = LineInputsPart()
