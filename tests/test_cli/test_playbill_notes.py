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
        "# Triage\nNot reachable.\n",
        "--about",
        WI1,
        "--because",
        "Triage context",
    )
    assert noted.exit_code == 0, noted.output
    assert f"note doc-notes.md#Triage about {WI1}" in noted.output
    assert (tmp_path / "notes.md").read_text() == "# Triage\nNot reachable.\n"
    shown = _run("backlinks", WI1, "--json")
    assert shown.exit_code == 0, shown.output
    result: dict[str, Any] = json.loads(shown.stdout)
    (link,) = result["links"]
    assert (link["relation"], link["source_kind"], link["drift"]) == ("about", "Note", "unchanged")
    (tmp_path / "notes.md").write_text("# Triage\nReachable after all.\n")
    text = _run("backlinks", WI1)
    assert text.exit_code == 0, text.output
    assert "about Note NOTE-" in text.output and "changed_since_cited" in text.output
    section = _run("backlinks", "--section", "notes.md#Triage", "--json")
    assert section.exit_code == 0, section.output
    assert json.loads(section.stdout)["links"] == []


def test_backlinks_needs_one_target(reads: _Reads) -> None:
    result = _run("backlinks")
    assert result.exit_code != 0 and "REF" in result.output
