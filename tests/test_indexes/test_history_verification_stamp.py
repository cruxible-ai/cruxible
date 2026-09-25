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
