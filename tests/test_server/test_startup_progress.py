"""A daemon start that migrates says so: start and done lines, and a CLI that reads them."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from structlog.testing import capture_logs

from cruxible_core import startup_progress
from cruxible_core.runtime import instance as instance_module
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.retention import RetentionSettings
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_floor.test_floor_index import _set, _write


def _steps(logs: list[dict[str, object]], step: str) -> list[dict[str, object]]:
    return [line for line in logs if line.get("step") == step]


def test_a_slow_step_logs_its_start_its_counts_and_its_elapsed_time() -> None:
    with capture_logs() as logs:
        with startup_progress.slow_step("ledger_compaction", instance_id="inst_x", loose_objects=9):
            pass
        with pytest.raises(RuntimeError):
            with startup_progress.slow_step("projection_rebuild", instance_id="inst_x"):
                raise RuntimeError("boom")

    started, finished, _, failed = logs
    assert started["event"] == startup_progress.STARTED and started["loose_objects"] == 9
    assert finished["event"] == startup_progress.FINISHED
    assert isinstance(finished["elapsed_s"], float)
    assert failed["event"] == startup_progress.FAILED and failed["reason"] == "RuntimeError"


def test_start_compaction_logs_its_loose_objects_before_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    instance.settled_workspace_advertisement()
    monkeypatch.setattr(instance_module, "LEDGER_START_COMPACTION_LOOSE_OBJECTS", 1)

    with capture_logs() as logs:
        assert instance.compact_ledger_at_start() is True

    started, finished = _steps(logs, "ledger_compaction")
    assert started["instance_id"] == instance.descriptor.instance_id
    assert isinstance(started["loose_objects"], int) and started["loose_objects"] >= 1
    assert finished["loose_objects_after"] == 0


def test_the_open_that_evicts_a_backlog_logs_how_many_pieces_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.workspace_advertisement import NOT_ATTACHED_ADVERTISEMENT
    from cruxible_core.runtime import retention

    monkeypatch.setattr(
        PlaybillInstance, "advertise_workspace", lambda self: NOT_ATTACHED_ADVERTISEMENT
    )
    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked", "done"):
        _write(instance, _set(f"{KIND}/wi-1", "status", value))
    monkeypatch.setattr(retention, "DEFAULT_RETENTION", RetentionSettings(generations=1))
    monkeypatch.setattr(instance_module, "DEFAULT_RETENTION", RetentionSettings(generations=1))

    with capture_logs() as logs:
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    started, finished = _steps(logs, "projection_eviction")
    assert started["candidates"] == finished["evicted"]
    assert isinstance(finished["evicted"], int) and finished["evicted"] >= 1
    assert isinstance(finished["freed_bytes"], int) and finished["freed_bytes"] > 0


def _line(event: str, step: str, *, boot_id: str, **fields: object) -> str:
    # The whole start is no one instance's step.
    instance_id = None if step == "daemon_start" else "inst_x"
    return json.dumps(
        {"event": event, "step": step, "instance_id": instance_id, "boot_id": boot_id, **fields}
    )


class _Starting:
    """A lifecycle client whose new image never answers: it is still migrating."""

    def daemon_identity(self) -> tuple[str, str | None]:
        raise ConnectionRefusedError("socket missing")


def test_restart_prints_the_new_images_steps_and_says_a_long_one_is_not_a_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cruxible_core.cli.commands import server

    monkeypatch.setattr(server, "_RESTART_POLL_INTERVAL_SECONDS", 0.01)
    log = tmp_path / "server.log"
    log.write_text(
        "\n".join(
            (
                _line(startup_progress.STARTED, "ledger_compaction", boot_id="boot_old"),
                _line(startup_progress.STARTED, "daemon_start", boot_id="boot_new"),
                _line(
                    startup_progress.STARTED,
                    "ledger_compaction",
                    boot_id="boot_new",
                    loose_objects=51953,
                ),
            )
        )
        + "\n"
    )

    with pytest.raises(click.ClickException, match="still starting, not hung") as raised:
        server._wait_for_daemon(
            _Starting(),  # type: ignore[arg-type]
            0.1,
            old_boot_id="boot_old",
            startup_log=server._StartupLog(path=log, offset=0, skip_boot_id="boot_old"),
        )

    assert "compacting the ledger for inst_x" in raised.value.message
    printed = capsys.readouterr().err.splitlines()
    assert printed == [
        "Starting: opening instances and running start recovery...",
        "Migrating: compacting the ledger for inst_x (loose objects 51953)...",
    ]


def test_status_names_the_start_step_a_daemon_that_cannot_answer_yet_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.cli.commands import server
    from cruxible_core.server.state_lock import StateRootLock

    monkeypatch.delenv("CRUXIBLE_SERVER_LOG_PATH", raising=False)
    state_root = tmp_path / "state"
    log = state_root / "daemon" / "logs" / "server.log"
    log.parent.mkdir(parents=True)
    assert server._starting_daemon_note(state_root) is None  # no daemon holds it

    with StateRootLock(state_root, transport="unix:test", boot_id="boot_new"):
        log.write_text(
            "\n".join(
                (
                    _line(startup_progress.STARTED, "daemon_start", boot_id="boot_new"),
                    _line(
                        startup_progress.STARTED,
                        "projection_eviction",
                        boot_id="boot_new",
                        candidates=40,
                    ),
                )
            )
            + "\n"
        )
        note = server._starting_daemon_note(state_root)
        assert note is not None and "not hung" in note
        assert "evicting projection pieces past retention for inst_x (candidates 40)" in note

        with log.open("a") as handle:
            handle.write(
                _line(startup_progress.FINISHED, "daemon_start", boot_id="boot_new") + "\n"
            )
        # Its start is done: a failure to answer is something else.
        assert server._starting_daemon_note(state_root) is None
