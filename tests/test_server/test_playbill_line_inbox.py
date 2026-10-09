"""The Line inbox over HTTP: the typed SDK and the CLI read and judge what a Line posted."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from click.testing import CliRunner

from cruxible_client import AccessProfile, Cruxible, CruxibleClient
from cruxible_client.contracts.read_values import ShownObject, TruncatedText
from cruxible_client.errors import CoreError as CruxibleError
from cruxible_core.cli.commands import playbill as commands
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.service.procedures.line_inbox import payload_preview
from tests.test_procedures.test_line_inbox_delivery import ROWS, inbox_world
from tests.test_procedures.test_manual_line_runs import manual_run
from tests.test_procedures.test_procedure_source_runs import NOW


def _served_inbox(playbill_http, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    http, instance_id, _ = playbill_http
    (tmp_path / "inbox-world").mkdir()
    instance, root, line = inbox_world(tmp_path / "inbox-world")
    state = manual_run(instance, line, at=NOW, root=root)
    assert state.status == "succeeded", state.terminal
    manager = get_playbill_manager()
    manager.consumer_runner.close()
    original_get = manager.get
    monkeypatch.setattr(
        manager, "get", lambda key: instance if key == instance_id else original_get(key)
    )
    transport = CruxibleClient(base_url="http://testserver")
    transport._client.close()
    transport._client = http
    sdk = Cruxible(
        client=transport,
        instance_id=instance_id,
        workspace=tmp_path,
        access_profile=AccessProfile("test", (), False),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    return instance, line, transport, instance_id, sdk


def test_the_sdk_lists_reads_and_judges_inbox_items(playbill_http, tmp_path, monkeypatch):
    instance, line, _transport, _instance_id, sdk = _served_inbox(
        playbill_http, tmp_path, monkeypatch
    )
    inbox = sdk.inbox()

    page = inbox.list(line=line.identity.name, status="unread", limit=2)
    assert page.truncated and len(page.entries) == 2
    rest = inbox.list(line=f"Line:{line.identity.name}", status="unread", cursor=page.next_cursor)
    entries = (*page.entries, *rest.entries)
    assert not rest.truncated
    assert sorted(entry.item.item_id for entry in entries) == sorted(
        {entry.item.item_id for entry in entries}
    )
    assert all(isinstance(entry.payload_preview, ShownObject) for entry in entries)
    first, second, third = (entry.item.item_id for entry in entries)

    whole = inbox.get(first[:16])
    assert whole.item.item_id == first and whole.payload in ROWS
    assert whole.dispositions == ()

    acked = inbox.ack(first)
    assert acked.recorded and acked.item.status == "acknowledged"
    assert not inbox.ack(first).recorded
    dismissed = inbox.dismiss(second, reason="not ours")
    assert dismissed.item.status == "dismissed"
    assert dismissed.item.disposition is not None
    assert dismissed.item.disposition.disposition == "dismissed"

    proposals = sdk.proposals(limit=1).entries
    linked = inbox.link(third, proposal=proposals[0].proposal_id[:20])
    assert linked.item.status == "linked"
    assert linked.item.disposition is not None
    assert linked.item.disposition.target.proposal_id == proposals[0].proposal_id  # type: ignore[union-attr]

    assert inbox.list(status="unread").entries == ()
    assert [item.disposition for item in inbox.get(first).dispositions] == ["acknowledged"]


def test_refusals_name_the_list_repair(playbill_http, tmp_path, monkeypatch):
    _instance, _line, _transport, _instance_id, sdk = _served_inbox(
        playbill_http, tmp_path, monkeypatch
    )
    inbox = sdk.inbox()
    item = inbox.list(limit=1).entries[0].item.item_id

    with pytest.raises(CruxibleError) as missing:
        inbox.get("INB-" + "0" * 12)
    assert getattr(missing.value, "error_code", None) == "cruxible.inbox.item_not_found"
    assert getattr(missing.value, "repair").operation == "cruxible.inbox.list"
    with pytest.raises(CruxibleError) as subject:
        inbox.link(item, subject="dev.note/absent")
    assert "dev.note/absent" in str(subject.value)
    with pytest.raises(CruxibleError):
        inbox.link(item, proposal="sha256:" + "0" * 12)
    with pytest.raises(CruxibleError) as cursor:
        inbox.list(cursor="not-a-cursor")
    assert getattr(cursor.value, "error_code", None) == "cruxible.inbox.request_invalid"
    assert getattr(cursor.value, "repair").operation == "cruxible.inbox.list"
    assert inbox.get(item).item.status == "unread"


def test_the_cli_lists_and_judges_inbox_items(playbill_http, tmp_path, monkeypatch):
    _instance, line, transport, instance_id, _sdk = _served_inbox(
        playbill_http, tmp_path, monkeypatch
    )
    monkeypatch.setattr(commands, "_server_call", lambda call, **_: call(transport, instance_id))
    runner = CliRunner()

    listed = runner.invoke(commands.inbox_group, ["list", "--line", line.identity.name, "--json"])
    assert listed.exit_code == 0, listed.output
    items = [entry["item"]["item_id"] for entry in json.loads(listed.output)["entries"]]
    assert len(items) == 3
    human = runner.invoke(commands.inbox_group, ["list", "--status", "unread"])
    assert human.exit_code == 0, human.output
    assert "CVE-2026-0001" in human.output and items[0][:16] in human.output

    got = runner.invoke(commands.inbox_group, ["get", items[0][:16]])
    assert got.exit_code == 0, got.output
    assert items[0] in got.output and '"cve"' in got.output

    assert runner.invoke(commands.inbox_group, ["ack", items[0]]).exit_code == 0
    dismissed = runner.invoke(commands.inbox_group, ["dismiss", items[1], "--reason", "noise"])
    assert dismissed.exit_code == 0 and "dismissed" in dismissed.output
    both = runner.invoke(
        commands.inbox_group, ["link", items[2], "--proposal", "p", "--subject", "a/b"]
    )
    assert both.exit_code != 0 and "exactly one" in both.output
    shown = runner.invoke(commands.inbox_group, ["get", items[1], "--json"])
    detail = json.loads(shown.output)
    assert detail["dispositions"][0]["reason"] == "noise"


def test_a_long_payload_previews_as_truncated_text() -> None:
    long = {"text": "x" * 600}

    shown = payload_preview(long)

    assert isinstance(shown, TruncatedText)
    assert shown.length > 500 and shown.preview.startswith('{"text":"xxx')
    assert payload_preview({"short": 1}) == ShownObject(object={"short": 1})
    assert payload_preview("plain") == "plain"
