"""`cruxible set | retire | write`: outcomes as text, refusals with their repair."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.write import (
    FileEvidence,
    RetireRequest,
    SetRequest,
    WriteOutcome,
    WriteRequest,
    as_write_request,
)
from cruxible_core.cli.main import cli
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._write_support import (
    KIND,
    REPORTS,
    caller,
    cited_captures,
    report_evidence,
    seed_write_surface,
)

PREFIX = ["--server-url", "http://server", "--instance-id", "inst_write"]
WI1 = f"{KIND}/wi-1"


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))


class _ServiceClient:
    """Answers the write verbs from the real service over a seeded instance."""

    def __init__(self, instance: PlaybillInstance) -> None:
        self.instance = instance
        self.requests: list[Any] = []

    def _answer(self, instance_id: str, request: Any) -> WriteOutcome:
        assert instance_id == "inst_write"
        self.requests.append(request)
        return service_playbill_write(
            self.instance, request=as_write_request(request), caller=caller()
        )

    def set(self, instance_id: str, *, request: SetRequest) -> WriteOutcome:
        return self._answer(instance_id, request)

    def retire(self, instance_id: str, *, request: RetireRequest) -> WriteOutcome:
        return self._answer(instance_id, request)

    def write(self, instance_id: str, *, request: WriteRequest) -> WriteOutcome:
        return self._answer(instance_id, request)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _ServiceClient:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    client = _ServiceClient(instance)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, [*PREFIX, *args])


def test_write_doc_payload_creates_file_and_cites_section(
    served: _ServiceClient, tmp_path: Path
) -> None:
    payload = {
        "because": "The checks passed",
        "docs": [{"op": "append", "id": "basis", "file": "notes.md", "text": "# Decision\nReady"}],
        "changes": [
            {
                "op": "set",
                "subject": WI1,
                "field": "status",
                "value": "ready",
                "evidence": {"kind": "section", "ref": "basis"},
            }
        ],
    }
    result = CliRunner().invoke(
        cli,
        [*PREFIX, "write", "-", "--workspace-root", str(tmp_path), "--json"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.stdout)
    assert outcome["docs"][0]["written"] and outcome["changes"][0]["verdict"] == "supported"
    assert (tmp_path / "notes.md").read_text() == "# Decision\nReady\n"
    schema = _run("write", "--schema")
    assert "docs" in json.loads(schema.stdout)["properties"]


def test_set_prints_before_and_after_then_revises_on_the_next_set(served: _ServiceClient) -> None:
    first = _run("set", WI1, "status", "ready", "--because", "Checked.")
    assert first.exit_code == 0, first.output
    assert first.stdout.startswith("accepted (generation")
    assert "target: inst_write" in first.stderr
    assert "set project.work_item/wi-1 status: ready" in first.output
    assert "verdict supported" in first.output
    assert "next: cruxible get project.work_item/wi-1" in first.output
    request = served.requests[0]
    assert isinstance(request, SetRequest)
    assert request.surface == "cli" and request.accept == "if_allowed"

    second = _run("set", WI1, "status", "done", "--because", "Shipped.", "--json")
    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert payload["changes"][0]["before"] == "ready" and payload["changes"][0]["after"] == "done"
    assert payload["changes"][0]["revises"] == payload["changes"][0]["claim"]


def test_a_number_given_as_text_is_read_by_the_field_type(served: _ServiceClient) -> None:
    result = _run("set", WI1, "measured", "3", "--because", "Counted.", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["changes"][0]["after"] == 3
    # The field admits only captured evidence: the write lands uncovered and says so.
    assert payload["changes"][0]["verdict"] == "uncovered"
    assert payload["warnings"][0]["code"] == "cruxible.write.verdict_not_supported"
    assert "--capture" in payload["next"]


def test_a_refused_set_prints_its_code_nearest_names_and_repair_and_exits_1(
    served: _ServiceClient,
) -> None:
    result = _run("set", WI1, "status", "dne", "--because", "x")
    assert result.exit_code == 1
    assert "refused (generation" in result.output
    assert "cruxible.write.value_not_member (change 0)" in result.output
    assert "nearest: done" in result.output
    assert "repair: Use one of: blocked, done, ready" in result.output


def test_dry_run_no_accept_and_at_reach_the_request(served: _ServiceClient) -> None:
    preview = _run("set", WI1, "status", "ready", "--because", "x", "--dry-run", "--json")
    assert preview.exit_code == 0, preview.output
    at = json.loads(preview.stdout)["coordinate"]["git_oid"]
    assert json.loads(preview.stdout)["status"] == "would_accept"
    proposed = _run("set", WI1, "status", "ready", "--because", "x", "--no-accept", "--at", at)
    assert proposed.exit_code == 0, proposed.output
    assert "awaiting approval" in proposed.output
    assert "next: cruxible proposal activate" in proposed.output
    request = served.requests[-1]
    assert request.accept == "never" and request.at == at


def test_expect_compares_the_value_on_set_and_retire(served: _ServiceClient) -> None:
    first = _run("set", WI1, "status", "ready", "--because", "x", "--expect-absent")
    assert first.exit_code == 0, first.output
    assert served.requests[-1].expect == ()
    taken = _run("set", WI1, "status", "done", "--because", "x", "--expect-absent")
    assert taken.exit_code == 1
    assert "holds 'ready', not [] as expected" in taken.output
    both = _run(
        "set", WI1, "status", "done", "--because", "x", "--expect", "ready", "--expect-absent"
    )
    assert both.exit_code != 0 and "not both" in both.output
    stale = _run("set", WI1, "status", "done", "--because", "x", "--expect", "blocked")
    assert stale.exit_code == 1
    assert "cruxible.write.slot_changed (change 0)" in stale.output
    assert "holds 'ready', not 'blocked' as expected" in stale.output
    assert served.requests[-1].expect == "blocked"
    done = _run("set", WI1, "status", "done", "--because", "x", "--expect", "ready")
    assert done.exit_code == 0, done.output
    assert "set project.work_item/wi-1 status: ready -> done" in done.output

    many = _run("retire", WI1, "status", "--because", "x", "--expect", "done", "--expect", "ready")
    assert many.exit_code == 1 and "holds 'done'" in many.output
    assert served.requests[-1].expect == ("done", "ready")
    ended = _run("retire", WI1, "status", "--because", "x", "--expect", "done")
    assert ended.exit_code == 0, ended.output


def test_add_puts_one_more_value_in_a_many_valued_field(served: _ServiceClient) -> None:
    first = _run("add", WI1, "governs", f"{KIND}/wi-2", "--because", "Linked.")
    assert first.exit_code == 0, first.output
    assert "target: inst_write" in first.stderr
    assert "add project.work_item/wi-1 governs: project.work_item/wi-2" in first.output
    request = served.requests[-1]
    assert isinstance(request, WriteRequest) and request.surface == "cli"
    (change,) = request.changes
    assert change.op == "add" and not change.expect_absent  # type: ignore[union-attr]

    again = _run("add", WI1, "governs", f"{KIND}/wi-2", "--because", "Linked.")
    assert again.exit_code == 0 and "already live" in again.output
    # Other words are new evidence: the live Claim's evidence is refreshed.
    refreshed = _run("add", WI1, "governs", f"{KIND}/wi-2", "--because", "Again.")
    assert refreshed.exit_code == 0 and "already live" not in refreshed.output
    absent = _run("add", WI1, "governs", f"{KIND}/wi-2", "--because", "Again.", "--expect-absent")
    assert absent.exit_code == 1
    assert "cruxible.write.value_already_present (change 0)" in absent.output

    preview = _run("add", WI1, "governs", f"{KIND}/wi-3", "--because", "x", "--dry-run", "--json")
    assert preview.exit_code == 0, preview.output
    at = json.loads(preview.stdout)["coordinate"]["git_oid"]
    proposed = _run(
        "add", WI1, "governs", f"{KIND}/wi-3", "--because", "x", "--no-accept", "--at", at
    )
    assert proposed.exit_code == 0 and "awaiting approval" in proposed.output
    assert served.requests[-1].accept == "never" and served.requests[-1].at == at

    single = _run("add", WI1, "status", "done", "--because", "x")
    assert single.exit_code == 1 and "cruxible.write.field_is_single" in single.output
    # Captured-only field: the repair is the add command itself, with --capture.
    labels = _run("add", WI1, "labels", "urgent", "--because", "x", "--json")
    assert labels.exit_code == 0, labels.output
    repair = json.loads(labels.stdout)["next"]
    assert repair.startswith("cruxible add project.work_item/wi-1 labels urgent")
    assert "--capture" in repair
    both = _run(
        "add",
        WI1,
        "labels",
        "x",
        "--because",
        "x",
        "--capture",
        "sha256:" + "a" * 64,
        "--evidence-file",
        "notes.md#x",
    )
    assert both.exit_code != 0 and "not both" in both.output
    role = _run("add", WI1, "governs", f"{KIND}/wi-3", "--because", "x", "--role", "observation")
    assert role.exit_code == 1 and "cruxible.write.role_not_permitted" in role.output


def test_retire_takes_a_claim_id_or_a_subject_and_field(served: _ServiceClient) -> None:
    status = json.loads(_run("set", WI1, "status", "ready", "--because", "x", "--json").stdout)
    title = json.loads(_run("set", WI1, "title", "Old", "--because", "x", "--json").stdout)
    by_id = _run("retire", status["changes"][0]["claim"], "--because", "Withdrawn.")
    assert by_id.exit_code == 0, by_id.output
    assert "retire project.work_item/wi-1 status: ready" in by_id.output
    by_slot = _run("retire", WI1, "title", "--because", "Gone.", "--reason", "was-wrong")
    assert by_slot.exit_code == 0, by_slot.output
    request = served.requests[-1]
    assert isinstance(request, RetireRequest) and request.reason == "was-wrong"
    assert title["changes"][0]["claim"] in by_slot.output


def test_retire_names_one_value_of_a_many_valued_field(served: _ServiceClient) -> None:
    _run("add", WI1, "governs", f"{KIND}/wi-2", "--because", "Linked.")
    _run("add", WI1, "governs", f"{KIND}/wi-3", "--because", "Linked.")
    by_value = _run("retire", WI1, "governs", f"{KIND}/wi-2", "--because", "Unlinked.")
    assert by_value.exit_code == 0, by_value.output
    assert "retire project.work_item/wi-1 governs: project.work_item/wi-2" in by_value.output
    request = served.requests[-1]
    assert isinstance(request, RetireRequest)
    assert request.target.value == f"{KIND}/wi-2"  # type: ignore[union-attr]
    missing = _run("retire", WI1, "governs", f"{KIND}/wi-2", "--because", "Again.")
    assert missing.exit_code == 1 and "cruxible.write.value_not_present" in missing.output


def test_write_applies_a_file_of_changes_and_prints_its_schema(
    served: _ServiceClient, tmp_path: Path
) -> None:
    schema = _run("write", "--schema")
    assert schema.exit_code == 0, schema.output
    printed = json.loads(schema.stdout)
    assert set(printed["properties"]) == {"because", "subject", "changes", "docs"}

    changes = tmp_path / "changes.yaml"
    changes.write_text(
        f"""\
- op: add
  subject: {WI1}
  field: governs
  value: {KIND}/wi-2
- op: add
  subject: {WI1}
  field: governs
  value: {KIND}/wi-3
""",
        encoding="utf-8",
    )
    missing = _run("write", str(changes))
    assert missing.exit_code != 0 and "--because" in missing.output
    result = _run("write", str(changes), "--because", "Linked.")
    assert result.exit_code == 0, result.output
    assert result.output.count("add project.work_item/wi-1 governs") == 2
    # The same words again restate both values on the same evidence: no-ops.
    again = _run("write", str(changes), "--because", "Linked.")
    assert again.exit_code == 0, again.output
    assert again.output.count("already live") == 2

    bad = tmp_path / "bad.yaml"
    bad.write_text("because: x\nchanges:\n  - op: move\n", encoding="utf-8")
    refused = _run("write", str(bad))
    assert refused.exit_code != 0
    assert "not a valid write file" in refused.output and "--schema" in refused.output


def test_a_write_file_names_its_subject_once(served: _ServiceClient, tmp_path: Path) -> None:
    schema = json.loads(_run("write", "--schema").stdout)
    assert "subject" in schema["properties"]
    changes = tmp_path / "about-wi-1.yaml"
    changes.write_text(
        f"""\
because: Triaged.
subject: {WI1}
changes:
  - op: set
    field: status
    value: ready
  - op: add
    field: governs
    value: {KIND}/wi-2
  - op: set
    subject: {KIND}/wi-3
    field: status
    value: done
""",
        encoding="utf-8",
    )
    result = _run("write", str(changes))
    assert result.exit_code == 0, result.output
    assert "set project.work_item/wi-1 status: ready" in result.output
    assert "add project.work_item/wi-1 governs" in result.output
    assert "set project.work_item/wi-3 status: done" in result.output
    assert served.requests[-1].subject == WI1

    orphan = tmp_path / "orphan.yaml"
    orphan.write_text("because: x\nchanges:\n  - {op: set, field: status, value: done}\n")
    refused = _run("write", str(orphan))
    assert refused.exit_code == 1 and "cruxible.write.subject_required" in refused.output


def test_capture_handles_and_contract_evidence_on_set_and_add(
    served: _ServiceClient, tmp_path: Path
) -> None:
    from cruxible_client.contracts.write import WriteRequest as Write
    from cruxible_client.contracts.write import capture_handle

    seeded = service_playbill_write(
        served.instance,
        request=Write.model_validate(
            {
                "because": "The report counts it.",
                "changes": [
                    {
                        "op": "set",
                        "subject": WI1,
                        "field": "measured",
                        "value": 3,
                        "evidence": report_evidence(tmp_path / "ws", "Count: 3"),
                    }
                ],
            }
        ),
        caller=caller(),
    )
    (digest,) = cited_captures(served.instance, seeded.changes[0].claim or "")
    handle = capture_handle(digest)

    cited = _run("set", f"{KIND}/wi-2", "measured", "3", "--because", "x", "--capture", handle)
    assert cited.exit_code == 0, cited.output
    assert f"evidence {handle}" in cited.output and "verdict supported" in cited.output
    by_contract = _run(
        "add",
        WI1,
        "labels",
        "counted",
        "--because",
        "x",
        "--evidence-contract",
        REPORTS.identity.name,
        "--json",
    )
    assert by_contract.exit_code == 0, by_contract.output
    assert json.loads(by_contract.stdout)["changes"][0]["capture"] == handle
    change = served.requests[-1].changes[0]
    assert change.evidence.kind == "contract"
    missing = _run(
        "set",
        f"{KIND}/wi-3",
        "measured",
        "3",
        "--because",
        "x",
        "--evidence-contract",
        REPORTS.identity.name,
    )
    assert missing.exit_code == 1
    assert "cruxible.write.contract_capture_not_found" in missing.output
    both = _run(
        "set",
        WI1,
        "title",
        "x",
        "--because",
        "x",
        "--capture",
        handle,
        "--evidence-contract",
        REPORTS.identity.name,
    )
    assert both.exit_code != 0 and "not both" in both.output


def test_evidence_file_is_read_from_the_workspace(served: _ServiceClient, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".cruxible").mkdir(parents=True)
    (workspace / "notes.md").write_text("# Notes\n\nTitle: Tidy the CLI\n", encoding="utf-8")
    (workspace / ".cruxible" / "files.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: repo.notes
    locator: notes.md
    document_id: notes
    document_kind: note
    title: Notes
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:notes]
""",
        encoding="utf-8",
    )
    result = _run(
        "set",
        WI1,
        "title",
        "Tidy the CLI",
        "--because",
        "The notes name it.",
        "--evidence-file",
        "notes.md#Title: Tidy the CLI",
        "--workspace-root",
        str(workspace),
        "--dry-run",
    )
    request = served.requests[-1]
    assert isinstance(request.evidence, FileEvidence)
    assert request.evidence.observation is not None
    assert request.evidence.observation.source_id == "repo.notes"
    assert "would" in result.output
    both = _run(
        "set",
        WI1,
        "title",
        "x",
        "--because",
        "x",
        "--capture",
        "sha256:" + "a" * 64,
        "--evidence-file",
        "notes.md#Title",
    )
    assert both.exit_code != 0 and "not both" in both.output


def test_write_reads_its_changes_from_stdin(served: _ServiceClient) -> None:
    """`write -` takes a heredoc or a pipe: no throwaway file lands on disk."""

    result = CliRunner().invoke(
        cli,
        [*PREFIX, "write", "-"],
        input=f"because: Triaged.\nsubject: {WI1}\nchanges:\n  - op: set\n    field: status\n"
        "    value: ready\n",
    )
    assert result.exit_code == 0, result.output
    assert "set project.work_item/wi-1 status: ready" in result.output

    refused = CliRunner().invoke(cli, [*PREFIX, "write", "-"], input="because: x\nchanges: 3\n")
    assert refused.exit_code != 0
    assert "stdin is not a valid write file" in refused.output


def test_a_subject_named_by_key_text_reaches_core_as_written(served: _ServiceClient) -> None:
    """`set kind/<KEY TEXT>`: no CLI id pattern; core resolves the key in its canonical form."""

    result = _run("set", f"{KIND}/WI-1", "status", "ready", "--because", "Checked.")

    assert result.exit_code == 0, result.output
    assert served.requests[0].subject == f"{KIND}/WI-1"
    assert "set project.work_item/wi-1 status: ready" in result.output
    assert "+ subject" not in result.output

    # A Subject-valued VALUE by key text names the Subject holding that key.
    linked = _run("add", WI1, "governs", f"{KIND}/WI-2", "--because", "Linked.", "--json")
    assert linked.exit_code == 0, linked.output
    assert json.loads(linked.stdout)["changes"][0]["after"] == f"{KIND}/wi-2"


def test_docs_only_hint_in_cli_text_and_json(served, tmp_path):
    from cruxible_client.contracts.write import DOCS_ONLY_HINT

    payload = json.dumps(
        {
            "because": "A note",
            "docs": [
                {"op": "append", "id": "note", "file": "notes.md", "text": "# Note\nDraft"},
            ],
        }
    )
    for output in ([], ["--json"]):
        result = CliRunner().invoke(
            cli, [*PREFIX, "write", "-", "--workspace-root", str(tmp_path), *output], input=payload
        )
        assert result.exit_code == 0, result.output
        if output:
            assert json.loads(result.stdout)["hint"] == DOCS_ONLY_HINT
        else:
            assert DOCS_ONLY_HINT in result.stdout.splitlines()
            assert "warning" not in result.stdout


# -- first-run (slice A): awaiting wording, Subject retirement, rebind --------------------


def test_awaiting_names_approvers_only_for_an_independent_approval() -> None:
    from cruxible_client.contracts.write import ApprovalNeeded
    from cruxible_core.cli.commands.playbill import _awaiting_text

    def needed(reason: str, approvers: tuple[str, ...] = ()) -> ApprovalNeeded:
        return ApprovalNeeded(
            reason=reason,  # type: ignore[arg-type]
            minimum_approvals=1,
            eligible_approvers=approvers,
            activate="cruxible proposal activate p",
        )

    assert _awaiting_text(needed("activation_not_permitted")) == (
        "activation; your tier cannot activate"
    )
    assert _awaiting_text(needed("accept_never")) == "kept (accept=never)"
    assert "no eligible approver; add one: cruxible principal add" in _awaiting_text(
        needed("independent_approval_required")
    )
    assert _awaiting_text(needed("independent_approval_required", ("reviewer",))) == (
        "independent approval; approvers: reviewer"
    )
    assert all(
        "you, with a tier" not in _awaiting_text(needed(reason))
        for reason in ("activation_not_permitted", "accept_never", "independent_approval_required")
    )


def test_retire_with_claims_retires_a_subject(served: _ServiceClient) -> None:
    assert _run("set", WI1, "title", "Tidy", "--because", "Named.").exit_code == 0
    refused = _run("retire", WI1, "--because", "Merged.")
    assert refused.exit_code == 1
    assert "cruxible.write.subject_has_claims" in refused.output
    assert "--with-claims" in refused.output
    retired = _run("retire", WI1, "--with-claims", "--reason", "superseded", "--because", "Merged.")
    assert retired.exit_code == 0, retired.output
    assert f"retire Subject {WI1}" in retired.stdout
    misuse = _run("retire", WI1, "title", "--with-claims", "--because", "x")
    assert misuse.exit_code == 2 and "--with-claims retires a Subject" in misuse.output


def test_rebind_re_cites_a_claim_on_its_new_text_by_catalog_name(
    served: _ServiceClient, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    first = service_playbill_write(
        served.instance,
        request=WriteRequest.model_validate(
            {
                "because": "Counted.",
                "changes": [
                    {
                        "op": "set",
                        "subject": WI1,
                        "field": "measured",
                        "value": 5,
                        "evidence": report_evidence(workspace, "measured five"),
                    }
                ],
            }
        ),
        caller=caller(),
    )
    claim = first.changes[0].claim
    assert claim is not None
    (workspace / "reports.md").write_text("# Reports\n\nfive, counted again\n", encoding="utf-8")
    result = _run(
        "rebind",
        claim,
        "--file",
        "repo.reports#five, counted again",
        "--because",
        "Re-counted.",
        "--workspace-root",
        str(workspace),
    )
    assert result.exit_code == 0, result.output
    assert "rebind" in result.stdout and "(value unchanged)" in result.stdout
    (request,) = [item for item in served.requests if isinstance(item, WriteRequest)]
    (change,) = request.changes
    assert change.op == "rebind" and change.evidence.observation is not None  # type: ignore[union-attr]
    both = _run("rebind", claim, "--because", "x")
    assert both.exit_code == 2 and "pass one of file" in both.output
