"""`cruxible note` and `cruxible backlinks`: one write, and the pointers it leaves."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.backlinks import BacklinksRequest, BacklinksResult
from cruxible_core.service.discovery.backlinks import service_playbill_backlinks
from tests.core_support._write_support import seed_write_surface
from tests.test_cli.test_playbill_write import (  # noqa: F401  (autouse fixture)
    WI1,
    _isolated_context,
    _run,
    _ServiceClient,
)


class _Reads(_ServiceClient):
    def backlinks(self, instance_id: str, *, request: BacklinksRequest) -> BacklinksResult:
        assert instance_id == "inst_write"
        return service_playbill_backlinks(self.instance, request=request)

    def get(self, instance_id: str, *, request: Any) -> Any:
        from cruxible_core.service.discovery.get import service_playbill_get
        from cruxible_core.storage.cas import BodyAccessContext

        assert instance_id == "inst_write"
        return service_playbill_get(
            self.instance,
            request=request,
            access=BodyAccessContext(principal_id="owner", can_read_body=True),
        )


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Reads:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    client = _Reads(instance)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def test_note_appends_and_links_then_backlinks_list_it_with_drift(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    noted = _run(
        "note",
        "notes.md",
        "--text",
        "# Triage\nNot reachable.\n\n",
        "--about",
        WI1,
        "--because",
        "Triage context",
    )
    assert noted.exit_code == 0, noted.output
    # Text names the file by its path; the JSON keeps the catalog name it is stored under.
    assert f"note notes.md#Triage about {WI1}" in noted.output
    assert (tmp_path / "notes.md").read_text() == "# Triage\nNot reachable.\n\n"
    shown = _run("backlinks", WI1, "--json")
    assert shown.exit_code == 0, shown.output
    result: dict[str, Any] = json.loads(shown.stdout)
    (link,) = result["links"]
    assert (link["relation"], link["source_kind"], link["drift"]) == ("about", "Note", "unchanged")
    # sec N8: get --detail backlinks said [unknown] for the note backlinks called unchanged.
    detail = _run("get", WI1, "--detail", "backlinks")
    assert detail.exit_code == 0, detail.output
    assert "[unchanged]" in detail.output and "[unknown]" not in detail.output
    (tmp_path / "notes.md").write_text("# Triage\nReachable after all.\n")
    text = _run("backlinks", WI1)
    assert text.exit_code == 0, text.output
    assert "about Note NOTE-" in text.output and "changed_since_cited" in text.output
    detail = _run("get", WI1, "--detail", "backlinks")
    assert "changed_since_cited" in detail.output, detail.output
    section = _run("backlinks", "--section", "notes.md#Triage", "--json")
    assert section.exit_code == 0, section.output
    assert json.loads(section.stdout)["links"] == []


def test_backlinks_text_names_a_cited_file_by_its_current_path(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog name is the stored identity; text reads the path the catalog maps it to now."""

    monkeypatch.chdir(tmp_path)
    noted = _run(
        "note",
        "notes.md",
        "--text",
        "# Triage\nNot reachable.\n\n",
        "--about",
        WI1,
        "--because",
        "Triage context",
    )
    assert noted.exit_code == 0, noted.output
    shown = json.loads(_run("backlinks", WI1, "--json").stdout)
    assert shown["links"][0]["file"] == "doc-notes.md"
    text = _run("backlinks", WI1)
    assert text.exit_code == 0, text.output
    assert " notes.md#Triage" in text.output and "doc-notes.md" not in text.output

    # The file moves and its catalog entry follows: text reads the new path.
    (tmp_path / "triage").mkdir()
    (tmp_path / "notes.md").rename(tmp_path / "triage" / "notes.md")
    moved = _run("files", "add", "doc-notes.md", "triage/notes.md", "--replace")
    assert moved.exit_code == 0, moved.output
    text = _run("backlinks", WI1)
    assert " triage/notes.md#Triage" in text.output, text.output
    section = _run("backlinks", "--section", "triage/notes.md#Triage")
    assert section.exit_code == 0, section.output
    assert "backlinks of triage/notes.md#Triage" in section.output

    # No catalog entry maps the name any more: text shows the stored name.
    removed = _run("files", "remove", "doc-notes.md")
    assert removed.exit_code == 0, removed.output
    text = _run("backlinks", WI1)
    assert text.exit_code == 0, text.output
    assert " doc-notes.md#Triage" in text.output


def test_backlinks_needs_one_target(reads: _Reads) -> None:
    result = _run("backlinks")
    assert result.exit_code != 0 and "REF" in result.output


def test_the_note_handle_link_prints_reads_with_get(
    reads: _Reads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sec N8: `get NOTE-...` and `get Note:NOTE-...` answered ref_not_found."""

    monkeypatch.chdir(tmp_path)
    noted = _run(
        "note",
        "notes.md",
        "--text",
        "# Triage\nNot reachable.\n\n",
        "--about",
        WI1,
        "--because",
        "Triage context",
        "--json",
    )
    assert noted.exit_code == 0, noted.output
    shown = json.loads(_run("backlinks", WI1, "--json").stdout)
    (link,) = shown["links"]
    handle = link["source"].removeprefix("Note:")
    assert handle.startswith("NOTE-")

    for ref in (handle, f"Note:{handle}"):
        card = _run("get", ref)
        assert card.exit_code == 0, card.output
        assert f"note: Note:{handle}" in card.output
        assert f"about: {WI1}" in card.output and "section: Triage" in card.output
        assert "capture: CAP-" in card.output
    for detail in ("history", "proof"):
        read = _run("get", handle, "--detail", detail, "--json")
        assert read.exit_code == 0, read.output
        assert json.loads(read.stdout)["kind"] == "note"
