"""The typed SDK traverses HTTP into the same retained check/evaluate/dispatch service."""

from datetime import timedelta

from cruxible_client import AccessProfile, Cruxible, CruxibleClient
from cruxible_client.contracts.triggers import CaptureLandingSchedule
from cruxible_core.exhaust.line_dispatch import dispatch_root
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME


def test_typed_sdk_http_check_listen_evaluate_and_dispatch(playbill_http, tmp_path, monkeypatch):
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
    capture(instance, procedure)
    transport = CruxibleClient(base_url="http://testserver")
    transport._client.close()
    transport._client = http
    sdk = Cruxible(
        client=transport,
        instance_id=instance_id,
        workspace=tmp_path,
        access_profile=AccessProfile("test", (), False),
        clock=None,
    )
    checked = sdk.check_line(line.identity.name)
    assert checked.status == "met" and not checked.occurrences[0].pending
    assert not dispatch_root(instance).exists()
    armed = sdk.arm_line(line.identity.name)
    assert armed.state == "armed" and armed.armed_by.kind == "local_operator"
    assert armed.outcome == "armed"
    assert sdk.line_status(line.identity.name) == armed.model_copy(
        update={"outcome": None, "coordinate": None}
    )
    again = sdk.arm_line(line.identity.name)
    assert (again.arm_id, again.outcome) == (armed.arm_id, "already_armed")
    disarmed = sdk.disarm_line(line.identity.name)
    assert (disarmed.arm_id, disarmed.state, disarmed.stop_reason, disarmed.outcome) == (
        armed.arm_id,
        "stopped",
        "disarmed",
        "disarmed",
    )
    assert disarmed.stopped_at == armed.evaluated_until
    repeat = sdk.disarm_line(line.identity.name)
    assert (repeat.arm_id, repeat.outcome) == (armed.arm_id, "already_disarmed")
    evaluated = sdk.evaluate_line(line.identity.name, since=READ_TIME, until=now)
    assert evaluated.occurrences[0].pending
    result = sdk.dispatch_line(line.identity.name)
    assert result.items[0].status == "admitted", result
    assert sdk.dispatch_line(line.identity.name).items == ()
    retried = sdk.dispatch_line(
        line.identity.name, occurrence_id=evaluated.occurrences[0].occurrence_id, retry=True
    )
    assert retried.items[0].run_id == result.items[0].run_id
    from click.testing import CliRunner

    from cruxible_core.cli.commands import playbill as commands

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
    cli_disarm = CliRunner().invoke(commands.line_group, ["disarm", line.identity.name])
    assert cli_disarm.exit_code == 0, cli_disarm.output
    assert "already disarmed; nothing changed" in cli_disarm.output
    final = sdk.check_line(line.identity.name)
    assert not final.occurrences[0].pending
    assert final.occurrences[0].admitted_run_id == result.items[0].run_id
