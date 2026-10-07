"""The typed SDK traverses HTTP into the same retained evaluate/dispatch service."""

import json
from datetime import timedelta

from click.testing import CliRunner

from cruxible_client import AccessProfile, Cruxible, CruxibleClient
from cruxible_client.contracts.operational_reads import GetLineCard
from cruxible_client.contracts.triggers import CaptureLandingSchedule
from cruxible_core.cli.commands import playbill as commands
from cruxible_core.exhaust.line_dispatch import dispatch_root
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.credentials import get_runtime_credential_store
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME


def _served_line_world(playbill_http, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """A Line world served as the host's instance, at a pinned daemon instant."""

    http, instance_id, _ = playbill_http
    (tmp_path / "line-world").mkdir()
    instance, line, procedure = line_world(
        tmp_path / "line-world", CaptureLandingSchedule(event=SELECTOR)
    )
    manager = get_playbill_manager()
    manager.consumer_runner.close()  # deterministic matching is exercised in its own thread test
    original_get = manager.get
    monkeypatch.setattr(
        manager, "get", lambda key: instance if key == instance_id else original_get(key)
    )
    now = READ_TIME + timedelta(seconds=2)
    monkeypatch.setattr("cruxible_core.runtime.playbill_api._evaluation_time", lambda _: now)
    transport = CruxibleClient(base_url="http://testserver")
    transport._client.close()
    transport._client = http
    return http, instance_id, instance, line, procedure, now, transport


def test_typed_sdk_http_evaluate_enable_and_dispatch(playbill_http, tmp_path, monkeypatch):
    _http, instance_id, instance, line, procedure, now, transport = _served_line_world(
        playbill_http, tmp_path, monkeypatch
    )
    capture(instance, procedure)
    sdk = Cruxible(
        client=transport,
        instance_id=instance_id,
        workspace=tmp_path,
        access_profile=AccessProfile("test", (), False),
        clock=lambda: now,
    )
    handle = sdk.line(line.identity.name)
    assert handle.ref == f"Line:{line.identity.name}"
    checked = handle.evaluate(dry_run=True)
    assert checked.status == "met" and not checked.occurrences[0].pending
    assert not dispatch_root(instance).exists()
    enabled = handle.enable()
    assert enabled.state == "enabled" and enabled.enabled_by.kind == "local_operator"
    assert enabled.outcome == "enabled"
    card = sdk.get(handle.ref).value
    assert isinstance(card, GetLineCard)
    row = card.enablements[0]
    assert (row.enablement, row.state, row.enabled_by) == (
        enabled.enablement_id,
        "running",
        enabled.enabled_by.label,
    )
    assert row.line_artifact_digest == enabled.line_artifact_digest
    assert row.triggers == enabled.triggers and row.evaluated_until == enabled.evaluated_until
    again = handle.enable()
    assert (again.enablement_id, again.outcome) == (enabled.enablement_id, "already_enabled")
    disabled = handle.disable()
    assert (disabled.enablement_id, disabled.state, disabled.stop_reason, disabled.outcome) == (
        enabled.enablement_id,
        "stopped",
        "disabled",
        "disabled",
    )
    assert disabled.stopped_at == enabled.evaluated_until
    repeat = handle.disable()
    assert (repeat.enablement_id, repeat.outcome) == (enabled.enablement_id, "already_disabled")
    evaluated = handle.evaluate(since=READ_TIME, until=now)
    assert evaluated.occurrences[0].pending
    result = handle.dispatch()
    assert result.items[0].status == "admitted", result
    assert handle.dispatch().items == ()
    retried = handle.dispatch(occurrence_id=evaluated.occurrences[0].occurrence_id, retry=True)
    assert retried.items[0].run_id == result.items[0].run_id

    monkeypatch.setattr(commands, "_server_call", lambda call, **_: call(transport, instance_id))
    cli_retry = CliRunner().invoke(
        commands.line_group,
        [
            "dispatch",
            line.identity.name,
            "--occurrence-id",
            evaluated.occurrences[0].occurrence_id,
            "--retry",
            "--json",
        ],
    )
    assert cli_retry.exit_code == 0, cli_retry.output
    assert result.items[0].run_id in cli_retry.output
    cli_disable = CliRunner().invoke(commands.line_group, ["disable", line.identity.name])
    assert cli_disable.exit_code == 0, cli_disable.output
    assert "already disabled; nothing changed" in cli_disable.output
    final = handle.evaluate(dry_run=True)
    assert not final.occurrences[0].pending
    assert final.occurrences[0].admitted_run_id == result.items[0].run_id


def test_cli_line_dispatch_drains_every_pending_occurrence_by_default(
    playbill_http, tmp_path, monkeypatch
):
    """One `line dispatch` admits all pending work; `--limit 1` admits exactly one."""

    _http, instance_id, instance, line, procedure, now, transport = _served_line_world(
        playbill_http, tmp_path, monkeypatch
    )
    for index in range(3):
        capture(instance, procedure, partition=f"run:anchor-{index}")
    monkeypatch.setattr(commands, "_server_call", lambda call, **_: call(transport, instance_id))
    runner = CliRunner()
    evaluated = runner.invoke(
        commands.line_group,
        [
            "evaluate",
            line.identity.name,
            "--since",
            READ_TIME.isoformat(),
            "--until",
            now.isoformat(),
            "--json",
        ],
    )
    assert evaluated.exit_code == 0, evaluated.output
    pending = [
        item["occurrence_id"]
        for item in json.loads(evaluated.output)["occurrences"]
        if item["pending"]
    ]
    assert len(pending) == 3, evaluated.output

    one = runner.invoke(
        commands.line_group, ["dispatch", line.identity.name, "--limit", "1", "--json"]
    )
    assert one.exit_code == 0, one.output
    first = json.loads(one.output)["items"]
    assert [item["status"] for item in first] == ["admitted"], one.output

    rest = runner.invoke(commands.line_group, ["dispatch", line.identity.name, "--json"])
    assert rest.exit_code == 0, rest.output
    drained = json.loads(rest.output)["items"]
    assert [item["status"] for item in drained] == ["admitted", "admitted"], rest.output
    assert {item["occurrence_id"] for item in first + drained} == set(pending)

    empty = runner.invoke(commands.line_group, ["dispatch", line.identity.name])
    assert empty.exit_code == 0, empty.output
    assert "No pending occurrences" in empty.output


def test_a_read_only_caller_may_dry_run_evaluate_but_not_enqueue(
    playbill_http, tmp_path, monkeypatch
):
    """`POST /lines/{line}/evaluate`: a dry run is a read; enqueueing needs governed write."""

    http, instance_id, instance, line, procedure, now, _transport = _served_line_world(
        playbill_http, tmp_path, monkeypatch
    )
    capture(instance, procedure)
    reader = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label="reader-token",
        permission_mode=PermissionMode.READ_ONLY,
        principal_id=None,
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    headers = {"Authorization": f"Bearer {reader.token}"}
    path = f"/api/v1/{instance_id}/lines/{line.identity.name}/evaluate"

    dry = http.post(path, json={"dry_run": True}, headers=headers)
    assert dry.status_code == 200, dry.text
    assert dry.json()["status"] == "met"
    assert not dispatch_root(instance).exists()

    refused = http.post(
        path,
        json={"since": READ_TIME.isoformat(), "until": now.isoformat()},
        headers=headers,
    )
    assert refused.status_code == 403, refused.text
    answer = refused.json()
    assert answer["error_type"] == "PermissionDeniedError", refused.text
    assert answer["context"]["required_mode"] == "GOVERNED_WRITE", refused.text
    assert not dispatch_root(instance).exists()


def test_cli_line_dispatch_drains_past_a_full_page_that_stays_blocked(
    playbill_http, tmp_path, monkeypatch
):
    """S3 review b F-002: a blocked first page does not hide the occurrences after it."""

    from cruxible_client.contracts.errors import ExecutionError
    from cruxible_core.service.procedures import line_dispatch

    _http, instance_id, instance, line, procedure, now, transport = _served_line_world(
        playbill_http, tmp_path, monkeypatch
    )
    for index in range(5):
        capture(instance, procedure, partition=f"run:anchor-{index}")
    monkeypatch.setattr(commands, "_server_call", lambda call, **_: call(transport, instance_id))
    monkeypatch.setattr(commands, "_LINE_DISPATCH_PAGE", 2)
    runner = CliRunner()
    evaluated = runner.invoke(
        commands.line_group,
        [
            "evaluate",
            line.identity.name,
            "--since",
            READ_TIME.isoformat(),
            "--until",
            now.isoformat(),
            "--json",
        ],
    )
    assert evaluated.exit_code == 0, evaluated.output
    ordered = sorted(
        (item for item in json.loads(evaluated.output)["occurrences"] if item["pending"]),
        key=lambda item: (item["eligible_at"], item["occurrence_id"]),
    )
    assert len(ordered) == 5
    blocked = {item["occurrence_id"] for item in ordered[:2]}
    run = line_dispatch.service_run_playbill_line

    def blocking(*args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["request"].occurrence_id in blocked:
            raise ExecutionError("provider lane busy")
        return run(*args, **kwargs)

    monkeypatch.setattr(line_dispatch, "service_run_playbill_line", blocking)

    drained = runner.invoke(commands.line_group, ["dispatch", line.identity.name, "--json"])

    assert drained.exit_code == 0, drained.output
    items = json.loads(drained.output)["items"]
    # Every occurrence is visited exactly once: the two blocked ones are
    # reported once and stay pending, the three after them are admitted.
    assert [item["occurrence_id"] for item in items] == [item["occurrence_id"] for item in ordered]
    assert [item["status"] for item in items] == ["blocked"] * 2 + ["admitted"] * 3
