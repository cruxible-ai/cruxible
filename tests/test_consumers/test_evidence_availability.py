"""The evidence worker flags cited Captures the store can no longer produce, and only those."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.captures import parse_capture_envelope
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_core.consumers import evidence
from cruxible_core.consumers.evidence import EVIDENCE_AVAILABILITY as WORKER
from cruxible_core.consumers.evidence import SWEEP_INTERVAL, evidence_findings
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world

READ = BodyAccessContext(principal_id="test", can_read_body=True)
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, *_rest = shared_capture_world(tmp_path)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        (capture,) = {
            row[0]
            for row in projection.typed.connection.execute(
                "SELECT capture_digest FROM citation_uses WHERE owner_kind='Claim'"
            )
        }
    return instance, capture


def _drain(instance, *, now: datetime) -> None:  # type: ignore[no-untyped-def]
    WORKER.match(instance, now=now, daemon_id="daemon")
    manager = SimpleNamespace(get=lambda _id: instance)
    for work in WORKER.due(instance, now=now):
        WORKER.run(manager, "instance", work, now=now)


def _findings(instance):  # type: ignore[no-untyped-def]
    return {(item.capture_digest, item.part, item.state) for item in evidence_findings(instance)}


def test_a_healthy_world_sweeps_clean_and_reports_running(tmp_path: Path) -> None:
    instance, _capture = _world(tmp_path)
    _drain(instance, now=NOW)  # starts the cursor at head; history is left to the sweep
    _drain(instance, now=NOW)

    assert _findings(instance) == set()
    (health,) = WORKER.health(instance, now=NOW)
    assert health.state == "running" and health.detail["sweep_completed_at"] is not None


def test_a_missing_or_rotted_envelope_is_found_by_the_sweep_and_cleared_once_restored(
    tmp_path: Path,
) -> None:
    instance, capture = _world(tmp_path)
    store = instance.body_store()
    path = store._path(capture)
    original = path.read_bytes()
    _drain(instance, now=NOW)
    _drain(instance, now=NOW)

    path.chmod(0o600)
    path.write_bytes(b"rot" + original[3:])
    # Nothing re-checks before the sweep comes due.
    _drain(instance, now=NOW + timedelta(hours=1))
    assert _findings(instance) == set()
    _drain(instance, now=NOW + SWEEP_INTERVAL)
    assert _findings(instance) == {(capture, "envelope", "corrupt")}

    path.unlink()
    _drain(instance, now=NOW + 2 * SWEEP_INTERVAL)
    assert _findings(instance) == {(capture, "envelope", "missing")}

    path.write_bytes(original)
    _drain(instance, now=NOW + 3 * SWEEP_INTERVAL)
    assert _findings(instance) == set()


def test_a_newly_cited_capture_is_checked_without_waiting_for_the_sweep(tmp_path: Path) -> None:
    instance, capture = _world(tmp_path)
    _drain(instance, now=NOW)
    _drain(instance, now=NOW)
    with instance.accepted_history_reader() as history:
        head = history.sequence
    # Rewind the worker so the generation that cited the capture is new to it.
    with sqlite3.connect(evidence._root(instance) / "state.sqlite3") as connection:
        connection.execute("UPDATE progress SET generation=?", (head - 1,))
    instance.body_store()._path(capture).unlink()

    _drain(instance, now=NOW + timedelta(minutes=1))

    assert _findings(instance) == {(capture, "envelope", "missing")}


def test_a_body_its_policy_lets_go_is_not_a_finding_but_a_rotted_body_is(
    tmp_path: Path,
) -> None:
    instance, capture = _world(tmp_path)
    store = instance.body_store()
    envelope = parse_capture_envelope(store.read(capture, access=READ))
    if envelope.commitment.materialization != "cas":
        pytest.skip("this world's Capture keeps no body in the store")
    body = store._path(envelope.commitment.digest)
    original = body.read_bytes()
    _drain(instance, now=NOW)

    body.unlink()  # optional retention: the envelope still commits to the bytes
    _drain(instance, now=NOW + SWEEP_INTERVAL)
    assert _findings(instance) == set()

    body.write_bytes(b"x" + original[1:])
    _drain(instance, now=NOW + 2 * SWEEP_INTERVAL)
    assert _findings(instance) == {(capture, "body", "corrupt")}


def test_the_operator_can_turn_the_worker_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "evidence")
    assert not WORKER.active(SimpleNamespace())
    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "")
    assert WORKER.active(SimpleNamespace())


def test_a_failing_worker_is_stalled_with_a_repair_and_recovers_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.consumers.runner import consumer_health

    instance, _capture = _world(tmp_path)
    _drain(instance, now=NOW)
    original = WORKER._sweep

    def broken(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("store unreadable")

    monkeypatch.setattr(WORKER, "_sweep", broken)
    due = NOW + SWEEP_INTERVAL
    with pytest.raises(OSError):
        _drain(instance, now=due)
    (health,) = [item for item in consumer_health(instance, now=due) if item.kind == "evidence"]
    assert health.state == "stalled" and "store unreadable" in health.detail["last_error"]
    assert health.repair is not None and health.repair.operation == "hand_edit"

    monkeypatch.setattr(WORKER, "_sweep", original)
    _drain(instance, now=due)
    (health,) = WORKER.health(instance, now=due)
    assert health.state == "running"


def test_server_status_lists_open_instances_consumers_including_disabled_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.consumers.runner import consumer_statuses

    instance, _capture = _world(tmp_path)
    _drain(instance, now=NOW)
    manager = SimpleNamespace(open_instances=lambda: (("inst", instance),))

    (running,) = consumer_statuses(manager)
    assert (running.instance_id, running.kind, running.state) == ("inst", "evidence", "running")

    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "evidence")
    (disabled,) = consumer_statuses(manager)
    assert (disabled.consumer_id, disabled.state) == ("consumer:evidence", "disabled")
