"""Ledger compaction: packing keeps every ref, note, replay, old read and new write."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import WriteRequest
from cruxible_core.runtime import instance as instance_module
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.retention import RetentionSettings
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._write_support import KIND, caller, seed_write_surface
from tests.test_floor.test_floor_index import _set, _write

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"


def _git(instance: PlaybillInstance, *arguments: str) -> str:
    return subprocess.run(
        ["git", f"--git-dir={instance._ledger.path}", *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"},
    ).stdout


def _refs(instance: PlaybillInstance) -> str:
    return _git(instance, "for-each-ref", "--format=%(objectname) %(refname)")


def _notes(instance: PlaybillInstance) -> dict[str, str]:
    refs = [line.split()[1] for line in _refs(instance).splitlines() if " refs/notes/" in line]
    return {ref: _git(instance, "notes", f"--ref={ref}", "list") for ref in refs}


def _loose(instance: PlaybillInstance) -> int:
    return instance._ledger.loose_object_count()


def _leave_open(instance: PlaybillInstance) -> Any:
    outcome = service_playbill_write(
        instance,
        request=WriteRequest.model_validate(
            {"because": "Left open.", "accept": "never", "changes": [_set(WI2, "title", "Two")]}
        ),
        caller=caller(),
    )
    assert outcome.status != "accepted"
    return outcome


def test_compaction_and_prune_keep_refs_notes_replay_old_reads_and_new_writes(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked", "done"):
        _write(instance, _set(WI1, "status", value))
    _leave_open(instance)
    instance.settled_workspace_advertisement()
    refs, notes = _refs(instance), _notes(instance)
    behind = instance.accepted_coordinate()
    assert _loose(instance) > 0
    assert instance.compact_ledger(prune=True) is True
    assert _loose(instance) == 0
    assert list((instance._ledger.path / "objects/pack").glob("*.pack"))
    assert (instance._ledger.path / "objects/info/commit-graph").is_file()
    # Every ref and note is where it was.
    assert _refs(instance) == refs
    assert _notes(instance) == notes
    # Replay from the packed ledger reaches the same head.
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    # A new write lands, and an evicted old coordinate rebuilds from packed objects.
    _write(reopened, _set(WI1, "status", "ready"))
    _write(reopened, _set(WI1, "status", "blocked"))
    reopened.retention = RetentionSettings(generations=1)
    reopened.evict_projection_pieces()
    with reopened.bind_accepted_projection(behind) as projection:
        assert projection.accepted.git_oid == behind.git_oid
    reopened.settled_workspace_advertisement()
    assert reopened.compact_ledger() is False


def test_prune_waits_for_no_generation_in_flight(tmp_path: Path) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"))
    instance.settled_workspace_advertisement()
    with instance._ledger.generation_attempt():
        assert instance.compact_ledger(prune=True) is False
    assert instance.compact_ledger(prune=True) is True


def test_live_compaction_runs_once_per_stride_off_the_acceptance_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    monkeypatch.setattr(instance_module, "LEDGER_COMPACTION_STRIDE", 2)
    compacted: list[int] = []
    compact = instance.compact_ledger

    def counted(**kwargs: Any) -> bool:
        with instance.accepted_history_reader() as history:
            compacted.append(history.sequence)
        assert kwargs.get("prune", False) is False
        return compact(**kwargs)

    monkeypatch.setattr(instance, "compact_ledger", counted)
    instance.settled_workspace_advertisement()
    for value in ("ready", "blocked", "done", "ready", "blocked"):
        _write(instance, _set(WI1, "status", value))
        instance.settled_workspace_advertisement()
    assert compacted, "no stride was crossed"
    assert len(compacted) == len({sequence // 2 for sequence in compacted})
    assert _loose(instance) < 64


def test_the_daemon_start_pass_skips_a_ledger_with_few_loose_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    instance.settled_workspace_advertisement()
    loose = _loose(instance)
    monkeypatch.setattr(instance_module, "LEDGER_START_COMPACTION_LOOSE_OBJECTS", loose + 1)
    assert instance.compact_ledger_at_start() is False
    assert _loose(instance) == loose
    monkeypatch.setattr(instance_module, "LEDGER_START_COMPACTION_LOOSE_OBJECTS", loose)
    assert instance.compact_ledger_at_start() is True
    assert _loose(instance) == 0
