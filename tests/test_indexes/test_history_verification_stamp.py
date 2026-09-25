"""A reopened instance re-derives only history its verified record does not cover."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._knowledge_loop_support import seed_claims


def _reopen_and_read(instance: PlaybillInstance) -> tuple[PlaybillInstance, int]:
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    with reopened.accepted_history_reader():
        pass
    return reopened, reopened._accepted_history_index.generations_checked


def test_a_reopen_trusts_the_stamped_history_and_rederives_a_changed_row(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    with instance.accepted_history_reader():
        pass
    head = instance.accepted_history()[-1].sequence
    assert head > 1

    reopened, checked = _reopen_and_read(instance)
    assert checked == 0  # every generation through the head was already verified

    database = reopened._accepted_history_index.path
    connection = sqlite3.connect(database)
    with connection:
        (before,) = connection.execute(
            "SELECT artifact_revision FROM artifact_versions WHERE occurrence_sequence=? LIMIT 1",
            (head,),
        ).fetchone()
        connection.execute(
            "UPDATE artifact_versions SET artifact_revision=artifact_revision+100 "
            "WHERE occurrence_sequence=?",
            (head,),
        )
    connection.close()

    repaired, checked = _reopen_and_read(instance)
    assert checked == head + 1  # the record no longer matches: full re-derivation
    connection = sqlite3.connect(database)
    (after,) = connection.execute(
        "SELECT artifact_revision FROM artifact_versions WHERE occurrence_sequence=? LIMIT 1",
        (head,),
    ).fetchone()
    connection.close()
    assert after == before
    _again, checked = _reopen_and_read(instance)
    assert checked == 0


def test_rows_and_record_changed_after_startup_are_rederived(tmp_path: Path) -> None:
    """Only the record present when the index opened, or one it wrote, is honored."""

    import json

    from cruxible_core.indexes.history import history_index

    instance, _owner = seed_claims(tmp_path)
    with instance.accepted_history_reader():
        pass
    head = instance.accepted_history()[-1].sequence
    live = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)  # startup
    index = live._accepted_history_index
    record_path = index._verification_stamp_path()
    ready = json.loads(record_path.read_bytes())["ready"]

    # Before the first history read: change a row and re-chain a matching record.
    connection = sqlite3.connect(index.path)
    with connection:
        (before,) = connection.execute(
            "SELECT artifact_revision FROM artifact_versions WHERE occurrence_sequence=? LIMIT 1",
            (head,),
        ).fetchone()
        connection.execute(
            "UPDATE artifact_versions SET artifact_revision=artifact_revision+100 "
            "WHERE occurrence_sequence=?",
            (head,),
        )
        chain = history_index._history_chain(connection, through=head)
    connection.close()
    record_path.write_text(json.dumps({"ready": ready, "chain": chain}, sort_keys=True))

    with live.accepted_history_reader():
        pass
    assert index.generations_checked == head + 1  # full re-derivation
    connection = sqlite3.connect(index.path)
    (after,) = connection.execute(
        "SELECT artifact_revision FROM artifact_versions WHERE occurrence_sequence=? LIMIT 1",
        (head,),
    ).fetchone()
    connection.close()
    assert after == before
