"""The daemon summarizes an off-stride head once acceptances go quiet."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_core.ledger import recovery
from cruxible_core.ledger.checkpoints import (
    CHECKPOINT_DIRECTORY,
    QuietCheckpointWriter,
    checkpoint_path,
    discard_checkpoint,
    load_checkpoint_file,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.playbill_manager import PlaybillInstanceManager
from tests.test_ledger.test_activation import _instance
from tests.test_ledger.test_activation_handoff_guards import _input


def _directory(instance: PlaybillInstance) -> Path:
    return instance.root / CHECKPOINT_DIRECTORY


def _stamp(directory: Path) -> str:
    return (directory / (checkpoint_path(directory).name + ".verified")).read_text().strip()


def test_a_flushed_summary_at_the_head_reopens_without_replaying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner, reviewer = _instance(tmp_path)
    instance.defer_replay_checkpoints(quiet_seconds=3600)
    for index in (1, 2):
        instance.settle_and_activate(**_input(instance, reviewer, index=index))
    head = instance.accepted_coordinate().git_oid
    directory = _directory(instance)
    before = load_checkpoint_file(directory)
    assert before is None or before.body.git_oid != head  # off the stride of 50

    instance.flush_replay_checkpoint()
    record = load_checkpoint_file(directory)
    assert record is not None
    assert (record.body.sequence, record.body.git_oid) == (2, head)
    assert _stamp(directory) == record.checkpoint_digest

    replayed: list[int] = []
    original = recovery._verify_successor

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        replayed.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery, "_verify_successor", counted)
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_history() == instance.accepted_history()
    assert replayed == []

    # Without the summary the same reopen replays both generations.
    discard_checkpoint(directory)
    PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert len(replayed) == 2


def test_a_summary_is_written_only_while_its_generation_is_main(tmp_path: Path) -> None:
    instance, _owner, reviewer = _instance(tmp_path)
    deferred: list = []
    instance._quiet_checkpoints = SimpleNamespace(defer=deferred.append)  # type: ignore[assignment]
    for index in (1, 2):
        instance.settle_and_activate(**_input(instance, reviewer, index=index))
    assert len(deferred) == 2
    directory = _directory(instance)
    discard_checkpoint(directory)

    deferred[0]()  # generation 1 is no longer main
    assert load_checkpoint_file(directory) is None

    deferred[1]()
    deferred[0]()  # a late older summary never replaces the head's
    record = load_checkpoint_file(directory)
    assert record is not None
    assert record.body.git_oid == instance.accepted_coordinate().git_oid


def test_only_the_newest_write_runs_after_the_quiet_period() -> None:
    writer = QuietCheckpointWriter(quiet_seconds=0.2, name="quiet-test")
    ran: list[str] = []
    done = threading.Event()
    writer.defer(lambda: ran.append("older"))
    writer.defer(lambda: (ran.append("newer"), done.set()))
    assert ran == []
    assert done.wait(5)
    deadline = time.monotonic() + 5
    while writer._thread is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ran == ["newer"]
    assert writer._thread is None


def test_flush_writes_now_and_a_failed_write_is_dropped() -> None:
    writer = QuietCheckpointWriter(quiet_seconds=3600, name="quiet-test")
    ran: list[str] = []
    writer.defer(lambda: ran.append("pending"))
    writer.flush()
    assert ran == ["pending"]
    deadline = time.monotonic() + 5
    while writer._thread is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert writer._thread is None  # a flush releases the waiting thread
    writer.flush()  # nothing pending
    assert ran == ["pending"]

    def fail() -> None:
        raise OSError("disk full")

    writer.defer(fail)
    writer.flush()


def test_only_a_daemon_manager_defers_checkpoints() -> None:
    enabled: list[float] = []
    instance = SimpleNamespace(
        defer_replay_checkpoints=lambda *, quiet_seconds: enabled.append(quiet_seconds)
    )
    manager = PlaybillInstanceManager()
    manager._keep("inst_embedded", instance)  # type: ignore[arg-type]
    assert enabled == []
    manager.quiet_checkpoint_seconds = 5.0
    manager._keep("inst_daemon", instance)  # type: ignore[arg-type]
    assert enabled == [5.0]
