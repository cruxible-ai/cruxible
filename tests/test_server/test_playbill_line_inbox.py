"""The Line inbox over HTTP: the typed SDK and the CLI read and judge what a Line posted."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from click.testing import CliRunner

from cruxible_client import AccessProfile, Cruxible, CruxibleClient
from cruxible_client.contracts.line_inbox import inbox_payload_preview as payload_preview
from cruxible_client.contracts.principals import PRINCIPAL_ID_HEADER
from cruxible_client.contracts.read_values import ShownObject, TruncatedText
from cruxible_client.contracts.shapes import Contract
from cruxible_client.errors import CoreError as CruxibleError
from cruxible_core.cli.commands import playbill as commands
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.support.inbox import contract_members, listed_inbox_procedure
from tests.support.procedures import procedure_members  # noqa: E402
from tests.test_procedures.test_manual_line_runs import manual_run
from tests.test_procedures.test_procedure_source_runs import NOW

ROWS = [{"cve": "CVE-2026-0001"}, {"cve": "CVE-2026-0002"}, {"cve": "CVE-2026-0003"}]
#: What each posted row is.
CVE = Contract.model_validate(
    {"name": "test.inbox-cve", "version": 1, "fields": {"cve": {"type": "string"}}}
)


def _inbox_world(tmp_path):
    """Real current posting and a real pending Document proposal, independent of extract."""
    from cruxible_client.contracts.documents import (
        DocumentAuthority,
        DocumentLifecycle,
        DocumentShell,
    )
    from cruxible_client.contracts.procedures.artifacts import (
        AcceptedProcedure,
        procedure_artifact_digest,
    )
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from cruxible_core.service.authoring.documents import service_propose_playbill_document
    from tests.core_support._support import initialize_local
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
    from tests.test_server.test_playbill_line_run_refusals import _served_line

    instance, owner = initialize_local(tmp_path)
    template = _slotless_procedure("http-inbox").procedure
    procedure = listed_inbox_procedure(template, ROWS, CVE)
    accepted = AcceptedProcedure(
        path="procedures/http-inbox.json",
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    line = _served_line("http-inbox", accepted=accepted).model_copy(
        update={"max_authority": "observe"}
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            **contract_members(CVE),
            **procedure_members(procedure),
            line_spec_path(line.identity.name): render_line_spec(line),
        },
        timestamp="2026-08-21T12:00:00.000000Z",
        proposal_name="http-inbox",
    )
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    body = instance.store_document_body(b"Inbox linking prerequisite\n")
    pending = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity="document:http-inbox-link",
            document_kind="design",
            title="Inbox link",
            media_type="text/markdown",
            body_digest=body.digest,
            authority=DocumentAuthority(required_tier="graph_write"),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name="http-inbox-link",
        timestamp=NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    assert pending.proposal.candidate is not None
    return instance, root, line


def _served_inbox(playbill_http, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    http, instance_id, _ = playbill_http
    manager = get_playbill_manager()
    manager.consumer_runner.close()
    (tmp_path / "inbox-world").mkdir()
    instance, root, line = _inbox_world(tmp_path / "inbox-world")
    state = manual_run(instance, line, at=NOW, root=root)
    assert state.status == "succeeded", state.terminal
    original_get = manager.get
    monkeypatch.setattr(
        manager, "get", lambda key: instance if key == instance_id else original_get(key)
    )
    # Judging an item records who judged it: act as the world's owner.
    http.headers[PRINCIPAL_ID_HEADER] = "owner"
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
    assert page.truncated and len(page.rows) == 2
    rest = inbox.list(line=f"Line:{line.identity.name}", status="unread", cursor=page.next_cursor)
    rows = (*page.rows, *rest.rows)
    assert not rest.truncated
    assert sorted(row.item_id for row in rows) == sorted({row.item_id for row in rows})
    # S1: compact rows; the payload reads as a one-line title (its fields as
    # name=value pairs when it has no title field).
    assert sorted(row.title for row in rows) == sorted(f"cve={row['cve']}" for row in ROWS)
    assert {(row.status, row.seen, row.line) for row in rows} == {
        ("unread", 1, line.identity.qualified)
    }
    first, second, third = (row.item_id for row in rows)

    whole = inbox.get(first[:16])
    assert whole.item.item_id == first and whole.payload in ROWS
    assert whole.recent_dispositions == ()

    acked = inbox.ack(first)
    assert acked.recorded and acked.item.status == "acknowledged"
    assert not inbox.ack(first).recorded
    dismissed = inbox.dismiss(second, reason="not ours")
    assert dismissed.item.status == "dismissed"
    assert dismissed.item.disposition is not None
    assert dismissed.item.disposition.disposition == "dismissed"

    proposals = sdk.proposals(limit=1).entries
    assert len(proposals) == 1
    linked = inbox.link(third, proposal=proposals[0].proposal_id[:20])
    assert linked.item.status == "linked"
    assert linked.item.disposition is not None
    assert linked.item.disposition.target.proposal_id == proposals[0].proposal_id  # type: ignore[union-attr]

    assert inbox.list(status="unread").rows == ()
    assert [item.disposition for item in inbox.get(first).recent_dispositions] == ["acknowledged"]


def test_refusals_name_the_list_repair(playbill_http, tmp_path, monkeypatch):
    _instance, _line, _transport, _instance_id, sdk = _served_inbox(
        playbill_http, tmp_path, monkeypatch
    )
    inbox = sdk.inbox()
    item = inbox.list(limit=1).rows[0].item_id

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
    items = [row["item_id"] for row in json.loads(listed.output)["rows"]]
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
    assert detail["recent_dispositions"][0]["reason"] == "noise"


def test_a_long_payload_previews_as_truncated_text() -> None:
    long = {"text": "x" * 600}

    shown = payload_preview(long)

    assert isinstance(shown, TruncatedText)
    assert shown.length > 500 and shown.preview.startswith('{"text":"xxx')
    assert payload_preview({"short": 1}) == ShownObject(object={"short": 1})
    assert payload_preview("plain") == "plain"


def test_a_compact_row_titles_its_payload_in_one_bounded_line() -> None:
    from cruxible_client.contracts.line_inbox import (
        INBOX_ROW_TITLE_MAX_CHARS,
        InboxList,
        InboxListEntry,
        compact_inbox_list,
    )

    def entry(payload: object, index: int) -> dict[str, object]:
        sighting = {"run_id": f"run-{index}", "posted_at": "2026-10-01T00:00:00Z"}
        digest = "sha256:" + f"{index}" * 64
        return InboxListEntry.model_validate(
            {
                "item": {
                    "item_id": "INB-" + f"{index}" * 64,
                    "status": "unread",
                    "line": "Line:triage",
                    "procedure": "Procedure:triage",
                    "procedure_artifact_digest": digest,
                    "node_id": "ask",
                    "occurrences": 3,
                    "first_sighting": sighting,
                    "recent_sightings": [sighting],
                    "dedupe_key": digest,
                    "payload_digest": digest,
                    "payload_bytes": 10,
                },
                "payload_preview": payload_preview(payload),
            }
        ).model_dump(mode="json")

    page = InboxList.model_validate(
        {
            "entries": [
                entry({"title": "Patch lodash\\n now", "cve": "CVE-1"}, 1),
                entry("x" * 500, 2),
                # No title field: the fields read as name=value pairs, not JSON.
                entry({"component": "pkg:pypi/django@4.2", "cve_id": "CVE-2024-39614"}, 3),
                # Past the summary bound the preview is cut JSON; a title field
                # among its leading members still titles the row.
                entry({"name": "Big notice", "zbody": "y" * 600}, 4),
                entry({"count": 10, "matches": "sha256:" + "8" * 64, "zrows": ["z" * 600]}, 5),
            ]
        }
    )
    first, second, third, fourth, fifth = compact_inbox_list(page).rows
    assert (first.title, first.kind, first.seen) == ("Patch lodash\\n now", "ask", 3)
    assert len(second.title) == INBOX_ROW_TITLE_MAX_CHARS and second.title.endswith("…")
    assert third.title == "component=pkg:pypi/django@4.2 cve_id=CVE-2024-39614"
    assert fourth.title == "Big notice"
    assert fifth.title.startswith("count=10 matches=sha256:888") and "{" not in fifth.title
