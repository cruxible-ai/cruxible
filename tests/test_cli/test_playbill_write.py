"""`cruxible playbill set | retire | write`: outcomes as text, refusals with their repair."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.write import (
    FileEvidence,
    PlaybillRetireRequestV1,
    PlaybillSetRequestV1,
    PlaybillWriteRequestV1,
    WriteOutcome,
    as_write_request,
)
from cruxible_core.cli.main import cli
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._write_support import KIND, caller, seed_write_surface

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

    def playbill_set(self, instance_id: str, *, request: PlaybillSetRequestV1) -> WriteOutcome:
        return self._answer(instance_id, request)

    def playbill_retire(
        self, instance_id: str, *, request: PlaybillRetireRequestV1
    ) -> WriteOutcome:
        return self._answer(instance_id, request)

    def playbill_write(self, instance_id: str, *, request: PlaybillWriteRequestV1) -> WriteOutcome:
        return self._answer(instance_id, request)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _ServiceClient:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    client = _ServiceClient(instance)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, [*PREFIX, "playbill", *args])


def test_set_prints_before_and_after_then_revises_on_the_next_set(served: _ServiceClient) -> None:
    first = _run("set", WI1, "status", "ready", "--because", "Checked.")
    assert first.exit_code == 0, first.output
    assert first.output.startswith("accepted (generation")
    assert "set project.work_item/wi-1 status: ready" in first.output
    assert "verdict supported" in first.output
    assert "next: cruxible playbill get project.work_item/wi-1" in first.output
    request = served.requests[0]
    assert isinstance(request, PlaybillSetRequestV1)
    assert request.surface == "cli" and request.accept == "if_allowed"

    second = _run("set", WI1, "status", "done", "--because", "Shipped.", "--json")
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["changes"][0]["before"] == "ready" and payload["changes"][0]["after"] == "done"
    assert payload["changes"][0]["revises"] == payload["changes"][0]["claim"]


def test_a_number_given_as_text_is_read_by_the_field_type(served: _ServiceClient) -> None:
    result = _run("set", WI1, "measured", "3", "--because", "Counted.", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["changes"][0]["after"] == 3
    # The field admits only captured evidence: the write lands uncovered and says so.
    assert payload["changes"][0]["verdict"] == "uncovered"
    assert payload["warnings"][0]["code"] == "playbill.write.verdict_not_supported"
    assert "--capture" in payload["next"]


def test_a_refused_set_prints_its_code_nearest_names_and_repair_and_exits_1(
    served: _ServiceClient,
) -> None:
    result = _run("set", WI1, "status", "dne", "--because", "x")
    assert result.exit_code == 1
    assert "refused (generation" in result.output
    assert "playbill.write.value_not_member (change 0)" in result.output
    assert "nearest: done" in result.output
    assert "repair: Use one of: blocked, done, ready" in result.output


def test_dry_run_no_accept_and_at_reach_the_request(served: _ServiceClient) -> None:
    preview = _run("set", WI1, "status", "ready", "--because", "x", "--dry-run", "--json")
    assert preview.exit_code == 0, preview.output
    at = json.loads(preview.output)["coordinate"]["git_oid"]
    assert json.loads(preview.output)["status"] == "would_accept"
    proposed = _run("set", WI1, "status", "ready", "--because", "x", "--no-accept", "--at", at)
    assert proposed.exit_code == 0, proposed.output
    assert "awaiting approval" in proposed.output
    assert "next: cruxible playbill proposal activate" in proposed.output
    request = served.requests[-1]
    assert request.accept == "never" and request.at == at


def test_retire_takes_a_claim_id_or_a_subject_and_field(served: _ServiceClient) -> None:
    status = json.loads(_run("set", WI1, "status", "ready", "--because", "x", "--json").output)
    title = json.loads(_run("set", WI1, "title", "Old", "--because", "x", "--json").output)
    by_id = _run("retire", status["changes"][0]["claim"], "--because", "Withdrawn.")
    assert by_id.exit_code == 0, by_id.output
    assert "retire project.work_item/wi-1 status: ready" in by_id.output
    by_slot = _run("retire", WI1, "title", "--because", "Gone.", "--reason", "was-wrong")
    assert by_slot.exit_code == 0, by_slot.output
    request = served.requests[-1]
    assert isinstance(request, PlaybillRetireRequestV1) and request.reason == "was-wrong"
    assert title["changes"][0]["claim"] in by_slot.output


def test_write_applies_a_file_of_changes_and_prints_its_schema(
    served: _ServiceClient, tmp_path: Path
) -> None:
    schema = _run("write", "--schema")
    assert schema.exit_code == 0, schema.output
    printed = json.loads(schema.output)
    assert set(printed["properties"]) == {"because", "changes"}

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

    bad = tmp_path / "bad.yaml"
    bad.write_text("because: x\nchanges:\n  - op: move\n", encoding="utf-8")
    refused = _run("write", str(bad))
    assert refused.exit_code != 0
    assert "not a valid write file" in refused.output and "--schema" in refused.output


def test_evidence_file_is_read_from_the_workspace(served: _ServiceClient, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".playbill").mkdir(parents=True)
    (workspace / "notes.md").write_text("# Notes\n\nTitle: Tidy the CLI\n", encoding="utf-8")
    (workspace / ".playbill" / "sources.yaml").write_text(
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
