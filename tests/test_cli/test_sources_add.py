"""`sources add` / `sources remove`: typed edits of the workspace catalog, no hand-written YAML."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.authoring.sources import SourceCatalogEditError, add_catalog_entry
from cruxible_core.cli.main import cli


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "standup.md").write_text("# Standup\n", encoding="utf-8")
    (root / "notes" / "status.md").write_text("# Status\n", encoding="utf-8")
    return root


def _add(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["sources", "add", *args, "--root", str(root)])


def test_add_creates_the_catalog_and_the_workspace_reads_it(workspace: Path) -> None:
    result = _add(workspace, "standup", str(workspace / "notes" / "standup.md"))
    assert result.exit_code == 0, result.output
    assert "Added source standup -> notes/standup.md (evidence-only)" in result.output
    catalog = workspace / ".cruxible" / "sources.yaml"
    assert yaml.safe_load(catalog.read_text()) == {
        "catalog_kind": "portable",
        "entries": [{"name": "standup", "locator": "notes/standup.md"}],
    }
    assert (
        WorkspaceSources(workspace).path_for_source("standup")
        == (workspace / "notes" / "standup.md").resolve()
    )


def test_add_declares_a_document_and_keeps_entries_sorted(workspace: Path) -> None:
    assert _add(workspace, "standup", str(workspace / "notes" / "standup.md")).exit_code == 0
    result = CliRunner().invoke(
        cli,
        [
            "sources",
            "add",
            "status-page",
            str(workspace / "notes" / "status.md"),
            "--document-id",
            "status",
            "--document-kind",
            "page",
            "--title",
            "Status",
            "--scope",
            "team",
            "--root",
            str(workspace),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "added"
    assert payload["entry"]["media_type"] == "text/markdown"
    entries = yaml.safe_load((workspace / ".cruxible" / "sources.yaml").read_text())["entries"]
    assert [entry["name"] for entry in entries] == ["standup", "status-page"]
    assert entries[1]["governance_scope"] == ["team"]


def test_a_duplicate_name_refuses_unless_replace(workspace: Path) -> None:
    path = str(workspace / "notes" / "standup.md")
    assert _add(workspace, "standup", path).exit_code == 0
    before = (workspace / ".cruxible" / "sources.yaml").read_bytes()
    refused = _add(workspace, "standup", str(workspace / "notes" / "status.md"))
    assert refused.exit_code != 0
    assert "cruxible.sources.catalog_edit_refused (entry_exists)" in refused.output
    assert (workspace / ".cruxible" / "sources.yaml").read_bytes() == before
    replaced = _add(workspace, "standup", str(workspace / "notes" / "status.md"), "--replace")
    assert replaced.exit_code == 0, replaced.output
    assert "Replaced source standup -> notes/status.md" in replaced.output


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"document_id": "status"}, "entry_invalid"),
        ({"name": "Bad Name"}, "entry_invalid"),
    ],
    ids=["partial-document", "malformed-name"],
)
def test_an_invalid_entry_refuses_typed_and_writes_nothing(
    workspace: Path, fields: dict[str, object], reason: str
) -> None:
    name = str(fields.pop("name", "status-page"))
    with pytest.raises(SourceCatalogEditError) as raised:
        add_catalog_entry(
            workspace, name=name, path=workspace / "notes" / "status.md", fields=fields
        )
    assert raised.value.reason == reason
    assert not (workspace / ".cruxible" / "sources.yaml").exists()


def test_a_duplicate_document_target_refuses(workspace: Path) -> None:
    document = {
        "document_id": "status",
        "document_kind": "page",
        "title": "Status",
        "media_type": "text/markdown",
        "governance_scope": ("team",),
    }
    add_catalog_entry(
        workspace, name="one", path=workspace / "notes" / "status.md", fields=document
    )
    with pytest.raises(SourceCatalogEditError) as raised:
        add_catalog_entry(
            workspace, name="two", path=workspace / "notes" / "standup.md", fields=document
        )
    assert raised.value.reason == "entry_invalid"


def test_paths_outside_the_workspace_or_missing_refuse(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x\n", encoding="utf-8")
    with pytest.raises(SourceCatalogEditError) as raised:
        add_catalog_entry(workspace, name="out", path=outside, fields={})
    assert raised.value.reason == "path_outside_workspace"
    with pytest.raises(SourceCatalogEditError) as missing:
        add_catalog_entry(workspace, name="gone", path=workspace / "notes" / "gone.md", fields={})
    assert missing.value.reason == "path_not_a_file"


def test_remove_drops_one_entry_and_the_file_with_the_last(workspace: Path) -> None:
    assert _add(workspace, "standup", str(workspace / "notes" / "standup.md")).exit_code == 0
    assert _add(workspace, "status-page", str(workspace / "notes" / "status.md")).exit_code == 0
    runner = CliRunner()
    first = runner.invoke(cli, ["sources", "remove", "standup", "--root", str(workspace)])
    assert first.exit_code == 0, first.output
    entries = yaml.safe_load((workspace / ".cruxible" / "sources.yaml").read_text())["entries"]
    assert [entry["name"] for entry in entries] == ["status-page"]
    missing = runner.invoke(cli, ["sources", "remove", "standup", "--root", str(workspace)])
    assert missing.exit_code != 0
    assert "entry_missing" in missing.output
    last = runner.invoke(cli, ["sources", "remove", "status-page", "--root", str(workspace)])
    assert last.exit_code == 0, last.output
    assert "the catalog file is gone" in last.output
    assert not (workspace / ".cruxible" / "sources.yaml").exists()
