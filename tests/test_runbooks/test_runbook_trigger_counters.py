"""D-R48 review fix round 2, R1: exact Runbook/Trigger counters under every row
change, not only inserts.

``replace_rows`` (``indexes/typed_sqlite.py``) deletes a changed owner's old
row by path before its successor is inserted: a Runbook or Trigger revision,
retirement or (for a Trigger) retargeting all go through that same DELETE,
then INSERT -- never an UPDATE in place (only retained Claims get that
treatment). ``runbook_live_count`` and ``trigger_aim_counts`` (D-R48 fix round
1, F1/F2) must therefore uncount the replaced row's own OLD bucket on delete,
mirroring the insert trigger, or a revision double-counts, a retirement never
decrements, and a retarget leaves a stale bucket at the old target.

Two layers:
- direct schema-level tests reproduce the exact DELETE-then-INSERT sequence
  ``replace_rows`` performs (including inside a rolled-back transaction);
- one production-pipeline test runs a mixed real history (revision, retarget,
  retirement) through the authoring/accept pipeline and compares the
  maintained counters against a direct ``count(*)``-equivalent reference at
  every step, then against a cold rebuild from authority at the end.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from cruxible_core.indexes.typed_state import schema_sql

_RUNBOOK_COLUMNS = (
    "identity,path,format_tag,artifact_digest,predecessor_digest,revision,lifecycle,step_count"
)
_TRIGGER_COLUMNS = (
    "identity,path,format_tag,artifact_digest,predecessor_digest,revision,lifecycle,"
    "target_kind,target,schedule_kind,interval_seconds"
)


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.executescript(schema_sql())
    return connection


def _insert_runbook(
    connection: sqlite3.Connection,
    *,
    path: str,
    digest: str,
    revision: int,
    lifecycle: str,
    predecessor: str | None,
    identity: str = "Runbook:rb",
) -> None:
    connection.execute(
        f"INSERT INTO runbooks ({_RUNBOOK_COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
        (identity, path, "runbook-v1", digest, predecessor, revision, lifecycle, 1),
    )


def _delete_runbook(connection: sqlite3.Connection, path: str) -> None:
    connection.execute("DELETE FROM runbooks WHERE path=?", (path,))


def _insert_trigger(
    connection: sqlite3.Connection,
    *,
    path: str,
    digest: str,
    revision: int,
    lifecycle: str,
    predecessor: str | None,
    target: str,
    schedule_kind: str,
    identity: str = "Trigger:t",
) -> None:
    connection.execute(
        f"INSERT INTO triggers ({_TRIGGER_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            path,
            "trigger-v1",
            digest,
            predecessor,
            revision,
            lifecycle,
            "runbook",
            target,
            schedule_kind,
            None,
        ),
    )


def _delete_trigger(connection: sqlite3.Connection, path: str) -> None:
    connection.execute("DELETE FROM triggers WHERE path=?", (path,))


def _runbook_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT amount FROM runbook_live_count WHERE singleton=1").fetchone()
    return 0 if row is None else int(row[0])


def _trigger_bucket(connection: sqlite3.Connection, target: str, schedule_kind: str) -> int:
    row = connection.execute(
        "SELECT amount FROM trigger_aim_counts WHERE target_kind='runbook' AND target=? "
        "AND schedule_kind=?",
        (target, schedule_kind),
    ).fetchone()
    return 0 if row is None else int(row[0])


def _digest(tag: int) -> str:
    return f"sha256:{tag:064x}"


# -- direct schema-level mechanics: revision, retirement, retargeting, rollback -----


def test_a_live_runbooks_revision_does_not_double_count(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "revise.sqlite3")
    path = "runbooks/rb.json"
    _insert_runbook(
        connection, path=path, digest=_digest(0), revision=1, lifecycle="live", predecessor=None
    )
    assert _runbook_count(connection) == 1
    # replace_rows: DELETE the old row at the same path, then INSERT the successor.
    _delete_runbook(connection, path)
    _insert_runbook(
        connection,
        path=path,
        digest=_digest(1),
        revision=2,
        lifecycle="live",
        predecessor=_digest(0),
    )
    assert _runbook_count(connection) == 1


def test_a_runbooks_retirement_decrements_and_the_zero_bucket_is_removed(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "retire.sqlite3")
    path = "runbooks/rb.json"
    _insert_runbook(
        connection, path=path, digest=_digest(0), revision=1, lifecycle="live", predecessor=None
    )
    assert _runbook_count(connection) == 1
    _delete_runbook(connection, path)
    _insert_runbook(
        connection,
        path=path,
        digest=_digest(1),
        revision=2,
        lifecycle="retired",
        predecessor=_digest(0),
    )
    assert _runbook_count(connection) == 0
    # CHECK(amount>0) forbids a zero row: it must be removed, not merely zeroed.
    assert connection.execute("SELECT COUNT(*) FROM runbook_live_count").fetchone()[0] == 0


def test_a_second_runbooks_retirement_leaves_the_first_counted(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "two_retire.sqlite3")
    _insert_runbook(
        connection,
        path="runbooks/one.json",
        digest=_digest(0),
        revision=1,
        lifecycle="live",
        predecessor=None,
        identity="Runbook:one",
    )
    _insert_runbook(
        connection,
        path="runbooks/two.json",
        digest=_digest(1),
        revision=1,
        lifecycle="live",
        predecessor=None,
        identity="Runbook:two",
    )
    assert _runbook_count(connection) == 2
    _delete_runbook(connection, "runbooks/one.json")
    _insert_runbook(
        connection,
        path="runbooks/one.json",
        digest=_digest(2),
        revision=2,
        lifecycle="retired",
        predecessor=_digest(0),
        identity="Runbook:one",
    )
    assert _runbook_count(connection) == 1


def test_a_triggers_retargeting_moves_the_bucket_old_and_new(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "retarget.sqlite3")
    path = "triggers/t.json"
    _insert_trigger(
        connection,
        path=path,
        digest=_digest(0),
        revision=1,
        lifecycle="live",
        predecessor=None,
        target="Runbook:one",
        schedule_kind="cron",
    )
    assert _trigger_bucket(connection, "Runbook:one", "cron") == 1
    _delete_trigger(connection, path)
    _insert_trigger(
        connection,
        path=path,
        digest=_digest(1),
        revision=2,
        lifecycle="live",
        predecessor=_digest(0),
        target="Runbook:two",
        schedule_kind="cadence",
    )
    # The old bucket uncounts to zero and is removed, never left stale.
    assert _trigger_bucket(connection, "Runbook:one", "cron") == 0
    assert (
        connection.execute(
            "SELECT 1 FROM trigger_aim_counts WHERE target_kind='runbook' AND target='Runbook:one' "
            "AND schedule_kind='cron'"
        ).fetchone()
        is None
    )
    # The new bucket (new target, new schedule_kind) counts the retargeted Trigger.
    assert _trigger_bucket(connection, "Runbook:two", "cadence") == 1


def test_a_triggers_retirement_leaves_no_stale_bucket(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "trigger_retire.sqlite3")
    path = "triggers/t.json"
    _insert_trigger(
        connection,
        path=path,
        digest=_digest(0),
        revision=1,
        lifecycle="live",
        predecessor=None,
        target="Runbook:one",
        schedule_kind="cron",
    )
    _delete_trigger(connection, path)
    _insert_trigger(
        connection,
        path=path,
        digest=_digest(1),
        revision=2,
        lifecycle="retired",
        predecessor=_digest(0),
        target="Runbook:one",
        schedule_kind="cron",
    )
    assert connection.execute("SELECT COUNT(*) FROM trigger_aim_counts").fetchone()[0] == 0


def test_a_rolled_back_replacement_leaves_counters_at_their_pre_transaction_values(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path / "rollback.sqlite3")
    runbook_path = "runbooks/rb.json"
    trigger_path = "triggers/t.json"
    _insert_runbook(
        connection,
        path=runbook_path,
        digest=_digest(0),
        revision=1,
        lifecycle="live",
        predecessor=None,
    )
    _insert_trigger(
        connection,
        path=trigger_path,
        digest=_digest(0),
        revision=1,
        lifecycle="live",
        predecessor=None,
        target="Runbook:one",
        schedule_kind="cron",
    )
    assert _runbook_count(connection) == 1
    assert _trigger_bucket(connection, "Runbook:one", "cron") == 1
    connection.execute("BEGIN")
    _delete_runbook(connection, runbook_path)
    _insert_runbook(
        connection,
        path=runbook_path,
        digest=_digest(1),
        revision=2,
        lifecycle="retired",
        predecessor=_digest(0),
    )
    _delete_trigger(connection, trigger_path)
    _insert_trigger(
        connection,
        path=trigger_path,
        digest=_digest(1),
        revision=2,
        lifecycle="live",
        predecessor=_digest(0),
        target="Runbook:two",
        schedule_kind="cadence",
    )
    connection.execute("ROLLBACK")
    assert _runbook_count(connection) == 1
    assert _trigger_bucket(connection, "Runbook:one", "cron") == 1
    assert _trigger_bucket(connection, "Runbook:two", "cadence") == 0


# -- production pipeline: a mixed real history, warm reference and cold rebuild -----


def _counter_state(connection: Any) -> tuple[int, tuple[tuple[str, str, int], ...]]:
    row = connection.execute("SELECT amount FROM runbook_live_count WHERE singleton=1").fetchone()
    amount = 0 if row is None else int(row[0])
    triggers = tuple(
        sorted(
            (str(target), str(schedule_kind), int(bucket_amount))
            for target, schedule_kind, bucket_amount in connection.execute(
                "SELECT target,schedule_kind,amount FROM trigger_aim_counts "
                "WHERE target_kind='runbook'"
            )
        )
    )
    return amount, triggers


def _reference_state(connection: Any) -> tuple[int, tuple[tuple[str, str, int], ...]]:
    """The exact current live population, computed fresh -- the old (unoptimized) semantics."""

    amount = connection.execute("SELECT COUNT(*) FROM runbooks WHERE lifecycle='live'").fetchone()[
        0
    ]
    triggers = tuple(
        sorted(
            (str(target), str(schedule_kind), int(bucket_amount))
            for target, schedule_kind, bucket_amount in connection.execute(
                "SELECT target,schedule_kind,COUNT(*) FROM triggers WHERE lifecycle='live' "
                "AND target_kind='runbook' GROUP BY target,schedule_kind"
            )
        )
    )
    return int(amount), triggers


def _assert_matches_reference(instance: Any) -> None:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        connection = projection.typed.connection
        assert _counter_state(connection) == _reference_state(connection)


def _retire_runbook(instance: Any, name: str) -> None:
    from cruxible_client.contracts.authoring.inputs import RunbookInput
    from tests.support.runbooks import author

    author(instance, RunbookInput(kind="runbook", name=name, retire=True), name="retire-" + name)


def _retire_trigger(instance: Any, name: str, *, runbook: str, schedule: dict[str, Any]) -> None:
    from cruxible_client.contracts.authoring.inputs import TriggerInput
    from tests.support.runbooks import author

    author(
        instance,
        TriggerInput.model_validate(
            {
                "kind": "trigger",
                "name": name,
                "schedule": schedule,
                "runbook_name": runbook,
                "retire": True,
            }
        ),
        name="retire-trigger-" + name,
    )


def test_warm_counters_match_a_full_reference_and_a_cold_rebuild_over_a_mixed_history(
    tmp_path: Path,
) -> None:
    """Revision, retarget and retirement, through the real authoring/accept pipeline.

    At every step the maintained counters must equal the direct reference
    query; at the end, a cold rebuild from authority (no cached projection
    file for the head) must see the same counters a warm incremental accept
    already holds.
    """

    from cruxible_core.runtime.instance import PlaybillInstance
    from tests.support.runbooks import SETTLE, author_runbook, author_runbook_trigger, runbook_world

    instance, _root = runbook_world(tmp_path)

    author_runbook(instance, "rb.one", [{"line": SETTLE}])
    _assert_matches_reference(instance)
    author_runbook(instance, "rb.two", [{"line": SETTLE}])
    _assert_matches_reference(instance)
    author_runbook_trigger(
        instance, "trig.a", runbook="rb.one", schedule={"kind": "cron", "expression": "0 6 * * *"}
    )
    _assert_matches_reference(instance)
    # Revision: rb.one's steps change under the same name (same path, new digest).
    author_runbook(instance, "rb.one", [{"line": SETTLE}, {"action": "evidence.sweep"}])
    _assert_matches_reference(instance)
    # Retargeting: trig.a moves from rb.one/cron to rb.two/cadence.
    author_runbook_trigger(
        instance, "trig.a", runbook="rb.two", schedule={"kind": "cadence", "interval_seconds": 3600}
    )
    _assert_matches_reference(instance)
    # Retirement: rb.one is no longer aimed at by anything live.
    _retire_runbook(instance, "rb.one")
    _assert_matches_reference(instance)
    _retire_trigger(
        instance, "trig.a", runbook="rb.two", schedule={"kind": "cadence", "interval_seconds": 3600}
    )
    _assert_matches_reference(instance)

    head = instance.accepted_coordinate()
    with instance.bind_accepted_projection(head) as warm:
        warm_counters = _counter_state(warm.typed.connection)
    directory = instance.root / "projections"
    manifest = next(
        path
        for path in directory.glob("projection-*.json")
        if json.loads(path.read_bytes())["git_oid"] == head.git_oid
    )
    data = json.loads(manifest.read_bytes())
    piece = directory / data["pieces"][0]["name"]
    manifest.unlink()
    piece.unlink()
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == head
    with reopened.bind_accepted_projection(head) as rebuilt:
        cold_counters = _counter_state(rebuilt.typed.connection)
    assert cold_counters == warm_counters
    # A non-trivial history: one live Runbook left, one live retargeted Trigger retired too.
    assert warm_counters == (1, ())
