"""A/C readers bind the namespace actually used to acquire their WAL snapshot."""

import os
import shutil
import sqlite3

import pytest

from cruxible_core.indexes.acquisition import DatabasePathChangedError
from tests.core_support._knowledge_loop_support import seed_claims


@pytest.mark.parametrize("component", ["history", "proposals"])
@pytest.mark.parametrize("ancestor_depth", [0, 1])
@pytest.mark.parametrize("repeat_attack", [False, True])
def test_restored_directory_swap_never_serves_forged_snapshot(
    tmp_path, monkeypatch, component, ancestor_depth, repeat_attack
):
    (tmp_path / "instance").mkdir()
    instance, _ = seed_claims(tmp_path / "instance")
    history_index = instance._accepted_history_index
    evidence = instance.proposal_evidence()
    proposal_index = evidence.index
    assert proposal_index is not None
    table, column = (
        ("artifact_versions", "artifact_digest")
        if component == "history"
        else ("proposals", "proposal_id")
    )

    def read_identity():
        if component == "history":
            with instance.accepted_history_reader() as reader:
                return reader._connection.execute(
                    f"SELECT {column} FROM {table} ORDER BY {column} LIMIT 1"
                ).fetchone()
        with proposal_index.read(evidence) as reader:
            return tuple(
                reader.execute(f"SELECT {column} FROM {table} ORDER BY {column} LIMIT 1").fetchone()
                or ()
            )

    expected = read_identity()
    assert expected
    checked = history_index.generations_checked
    reconstructed = proposal_index.reconstructions
    checkpoint = (evidence.root / ".proposal-source.json").read_bytes()
    database = history_index.path
    original_directory = database.parents[ancestor_depth]
    replacement = tmp_path / "forged"
    saved = tmp_path / "saved"
    shutil.copytree(original_directory, replacement)
    with sqlite3.connect(replacement / database.relative_to(original_directory)) as connection:
        connection.execute(f"DELETE FROM {table}")
    connect = sqlite3.connect
    attempts = 0

    def intercepted(database_arg, *args, **kwargs):
        nonlocal attempts
        if not str(database_arg).startswith(database.as_uri() + "?mode=ro"):
            return connect(database_arg, *args, **kwargs)
        attempts += 1
        if attempts > 1 and not repeat_attack:
            return connect(database_arg, *args, **kwargs)
        os.rename(original_directory, saved)
        os.rename(replacement, original_directory)
        try:
            connection = connect(database_arg, *args, **kwargs)
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        finally:
            os.rename(original_directory, replacement)
            os.rename(saved, original_directory)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", intercepted)
        if repeat_attack:
            with pytest.raises(DatabasePathChangedError, match="namespace changed"):
                read_identity()
            assert attempts == 3
        else:
            assert read_identity() == expected
            assert attempts >= 2
    assert read_identity() == expected
    assert history_index.generations_checked == checked
    assert proposal_index.reconstructions == reconstructed
    assert (evidence.root / ".proposal-source.json").read_bytes() == checkpoint
