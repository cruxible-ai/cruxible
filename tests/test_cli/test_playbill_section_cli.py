"""`cruxible section` reads one section; `set`/`add --section(-body)` cite or author one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.doc_sections import section_digest
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.cli.main import cli
from tests.core_support._write_support import seed_write_surface
from tests.test_cli.test_playbill_notes import _Reads
from tests.test_cli.test_playbill_write import (  # noqa: F401  (autouse fixture)
    KIND,
    PREFIX,
    WI1,
    _isolated_context,
    _run,
)


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Reads:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    client = _Reads(instance)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


NOTES = "# Notes\nPreamble of the notes.\n\n## Triage\nNot reachable.\n\n### Detail\nDeep.\n\n"


def _catalogued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.md").write_text(NOTES, encoding="utf-8")
    added = _run("files", "add", "doc-notes.md", "notes.md")
    assert added.exit_code == 0, added.output


def test_section_reads_one_section_by_path_or_catalog_name(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _catalogued(tmp_path, monkeypatch)
    text = _run("section", "notes.md#Notes/Triage")
    assert text.exit_code == 0, text.output
    # Its own body: the heading and prose, never the subsection's.
    assert text.stdout.startswith("## Triage\nNot reachable.\n")
    assert "Deep." not in text.stdout
    assert "subsections: 'notes.md#Notes/Triage/Detail'" in text.stdout
    assert reads.requests == []

    shown = _run("section", "doc-notes.md#Notes/Triage", "--json")
    assert shown.exit_code == 0, shown.output
    payload: dict[str, Any] = json.loads(shown.stdout)
    assert payload == {
        "file": "doc-notes.md",
        "path": "notes.md",
        "section": ["Notes", "Triage"],
        "digest": section_digest(b"## Triage\nNot reachable.\n\n"),
        "text": "## Triage\nNot reachable.\n\n",
        "subsections": ["Detail"],
    }
    preamble = _run("section", "notes.md#", "--json")
    assert preamble.exit_code == 0, preamble.output
    assert json.loads(preamble.stdout)["section"] == []
    assert json.loads(preamble.stdout)["subsections"] == ["Notes"]


def test_section_refuses_with_the_repair_for_each_miss(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _catalogued(tmp_path, monkeypatch)
    missing = _run("section", "notes.md#Notes/Triag")
    assert missing.exit_code == 1
    assert "cruxible.doc.section_not_found: notes.md has no section 'Notes/Triag'" in (
        missing.stderr
    )
    assert "nearest: doc-notes.md#Notes/Triage" in missing.stderr
    assert "top-level headings: Notes" in missing.stderr

    uncatalogued = tmp_path / "other.md"
    uncatalogued.write_text("# Other\n", encoding="utf-8")
    refused = _run("section", "other.md#Other")
    assert refused.exit_code == 1
    assert "cruxible.doc.file_not_catalogued" in refused.stderr
    assert "doc-notes.md" in refused.stderr and "cruxible files add" in refused.stderr

    (tmp_path / "notes.md").unlink()
    gone = _run("section", "doc-notes.md#Notes")
    assert gone.exit_code == 1 and "cruxible.doc.file_missing" in gone.stderr

    bad = _run("section", "notes.md")
    assert bad.exit_code == 1 and "cruxible.doc.locator_invalid" in bad.stderr


def test_set_section_body_appends_and_cites_the_section_in_one_write(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    reasoning = "Reasoning longer than a because. " * 12
    result = _run(
        "set",
        WI1,
        "status",
        "ready",
        "--section",
        "decisions.md#wi-1",
        "--section-body",
        reasoning,
        "--because",
        "Ready; see the decision.",
        "--json",
    )
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.stdout)
    assert outcome["status"] == "accepted"
    assert outcome["docs"][0]["written"]
    assert outcome["changes"][0]["verdict"] == "supported"
    (request,) = reads.requests
    assert isinstance(request, WriteRequest)
    (edited,) = request.docs
    assert (edited.op, edited.section) == ("append", ("wi-1",))
    assert request.changes[0].evidence.kind == "section"
    written = (tmp_path / "decisions.md").read_text()
    assert written.startswith("# wi-1\n") and reasoning.strip() in written
    # The cited section reads back with `cruxible section`.
    read = _run("section", "decisions.md#wi-1", "--json")
    assert read.exit_code == 0, read.output
    assert reasoning.strip() in json.loads(read.stdout)["text"]


def test_add_section_body_reads_stdin_and_set_section_cites_an_existing_one(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _catalogued(tmp_path, monkeypatch)
    added = CliRunner().invoke(
        cli,
        [
            *PREFIX,
            "add",
            WI1,
            "governs",
            f"{KIND}/wi-2",
            "--section",
            "notes.md#Notes/Links",
            "--section-body",
            "-",
            "--because",
            "Linked in review.",
        ],
        input="It governs wi-2.\n",
    )
    assert added.exit_code == 0, added.output
    assert "## Links\n\nIt governs wi-2.\n" in (tmp_path / "notes.md").read_text()

    cited = _run(
        "set",
        WI1,
        "status",
        "done",
        "--section",
        "doc-notes.md#Notes/Triage",
        "--own-words",
        "--because",
        "Triaged.",
        "--json",
    )
    assert cited.exit_code == 0, cited.output
    request = reads.requests[-1]
    evidence = request.changes[0].evidence
    assert (evidence.kind, evidence.section, evidence.own_words) == (
        "section",
        ("Notes", "Triage"),
        True,
    )
    assert request.docs == ()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--section-body", "x"), "--section-body needs --section"),
        (("--own-words",), "--own-words applies to --section"),
        (
            ("--section", "notes.md#A", "--capture", "CAP-0123456789ab"),
            "pass --section or --capture, not both",
        ),
        (("--section", "notes.md#", "--section-body", "x"), "the heading to append"),
    ],
)
def test_section_options_refuse_mismatched_flags(
    reads: _Reads, args: tuple[str, ...], message: str
) -> None:
    for verb in (("set", WI1, "status", "ready"), ("add", WI1, "governs", f"{KIND}/wi-2")):
        result = _run(*verb, *args, "--because", "x")
        assert result.exit_code == 2, result.output
        assert message in result.output, result.output
    assert reads.requests == []


def test_the_because_refusal_names_every_one_step_section_path(reads: _Reads) -> None:
    result = _run("set", WI1, "status", "ready", "--because", "x" * 281)
    assert result.exit_code == 2, result.output
    flat = " ".join(result.output.split())
    assert "w.section(FILE, heading=..., body=...)" in flat
    assert "--section FILE#Heading --section-body TEXT" in flat
    assert "evidence {kind: section, ref: ID}" in flat


def _note(heading: str, body: str, *subjects: str) -> None:
    args: list[str] = ["note", "notes.md", "--heading", heading, "--text", body]
    for subject in subjects:
        args += ["--about", subject]
    noted = _run(*args, "--because", "Triage context")
    assert noted.exit_code == 0, noted.output


def test_get_card_points_to_notes_and_detail_notes_names_each_read(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    secret = "Prose that stays in the workspace."
    _note("Triage", secret, WI1)
    shown = _run("get", WI1, "--json")
    assert shown.exit_code == 0, shown.output
    card = json.loads(shown.stdout)["card"]
    (pointer,) = card["notes"]
    assert pointer["file"] == "doc-notes.md" and pointer["section"] == ["Triage"]
    assert pointer["note"].startswith("NOTE-")
    # Checked against the workspace beside it; the card never carries the prose.
    assert pointer["drift"] == "unchanged"
    assert "notes_total" not in card
    assert secret not in shown.stdout
    assert f"cruxible get {WI1} --detail notes" in card["next"]

    text = _run("get", WI1)
    assert text.exit_code == 0, text.output
    assert "note notes.md#Triage  [unchanged]" in text.output
    assert secret not in text.output

    detail = _run("get", WI1, "--detail", "notes", "--json")
    assert detail.exit_code == 0, detail.output
    payload = json.loads(detail.stdout)
    assert payload["detail"] == "notes"
    (row,) = payload["notes"]["notes"]
    assert row["note"] == pointer["note"] and row["cited_digest"].startswith("sha256:")
    assert payload["notes"]["next"] == ["cruxible section 'doc-notes.md#Triage'"]
    assert secret not in detail.stdout
    detail_text = _run("get", WI1, "--detail", "notes")
    assert detail_text.exit_code == 0, detail_text.output
    assert f"{pointer['note']}  notes.md#Triage  [unchanged]" in detail_text.output
    assert "next: cruxible section 'notes.md#Triage'" in detail_text.output

    # The hint reads the section; an edit since the link reads as drift.
    read = _run("section", "notes.md#Triage")
    assert read.exit_code == 0 and secret in read.output
    (tmp_path / "notes.md").write_text("# Triage\nRewritten.\n", encoding="utf-8")
    moved = json.loads(_run("get", WI1, "--json").stdout)["card"]["notes"][0]
    assert moved["drift"] == "changed_since_cited"


def test_a_card_lists_a_few_notes_and_detail_notes_pages_them_all(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.get_reads import GET_CARD_LINKS

    monkeypatch.chdir(tmp_path)
    count = GET_CARD_LINKS + 2
    for index in range(count):
        _note(f"N{index}", f"Body {index}.", WI1)
    card = json.loads(_run("get", WI1, "--json").stdout)["card"]
    assert len(card["notes"]) == GET_CARD_LINKS and card["notes_total"] == count
    assert f"({count} notes in all)" in _run("get", WI1).output

    first = json.loads(_run("get", WI1, "--detail", "notes", "--limit", "3", "--json").stdout)
    assert len(first["notes"]["notes"]) == 3 and first["truncated"] is True
    seen = [row["note"] for row in first["notes"]["notes"]]
    cursor = first["next_cursor"]
    while cursor:
        page = json.loads(
            _run(
                "get", WI1, "--detail", "notes", "--limit", "3", "--cursor", cursor, "--json"
            ).stdout
        )
        seen += [row["note"] for row in page["notes"]["notes"]]
        cursor = page.get("next_cursor")
    assert len(seen) == len(set(seen)) == count
    # A Subject with no notes keeps the card it had: no notes field at all.
    other = json.loads(_run("get", f"{KIND}/wi-2", "--json").stdout)
    assert "notes" not in other.get("card", {})
    empty = _run("get", f"{KIND}/wi-2", "--detail", "notes")
    assert empty.exit_code == 0 and f"(no notes about {KIND}/wi-2)" in empty.output


def test_detail_notes_is_a_subject_detail(reads: _Reads) -> None:
    refused = _run("get", "ClaimType:project.work_item.status", "--detail", "notes")
    assert refused.exit_code != 0
    assert "notes" in refused.output
