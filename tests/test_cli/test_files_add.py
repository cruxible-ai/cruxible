"""`files add` / `files remove`: typed edits of the workspace catalog, no hand-written YAML."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from cruxible_client.authoring.files import FileCatalogEditError, add_catalog_entry
from cruxible_client.authoring.selectors import WorkspaceFiles
from cruxible_core.cli.main import cli


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "standup.md").write_text("# Standup\n", encoding="utf-8")
    (root / "notes" / "status.md").write_text("# Status\n", encoding="utf-8")
    return root


def _add(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["files", "add", *args, "--root", str(root)])


def test_add_creates_the_catalog_and_the_workspace_reads_it(workspace: Path) -> None:
    result = _add(workspace, "standup", str(workspace / "notes" / "standup.md"))
    assert result.exit_code == 0, result.output
    assert (
        "Added file standup -> notes/standup.md (no Document: cite it, or project blocks "
        "into it)" in result.output
    )
    catalog = workspace / ".cruxible" / "files.yaml"
    assert yaml.safe_load(catalog.read_text()) == {
        "catalog_kind": "portable",
        "entries": [{"name": "standup", "locator": "notes/standup.md"}],
    }
    assert (
        WorkspaceFiles(workspace).path_for_file("standup")
        == (workspace / "notes" / "standup.md").resolve()
    )


def test_add_declares_a_document_and_keeps_entries_sorted(workspace: Path) -> None:
    assert _add(workspace, "standup", str(workspace / "notes" / "standup.md")).exit_code == 0
    result = CliRunner().invoke(
        cli,
        [
            "files",
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
    entries = yaml.safe_load((workspace / ".cruxible" / "files.yaml").read_text())["entries"]
    assert [entry["name"] for entry in entries] == ["standup", "status-page"]
    assert entries[1]["governance_scope"] == ["team"]


def test_a_duplicate_name_refuses_unless_replace(workspace: Path) -> None:
    path = str(workspace / "notes" / "standup.md")
    assert _add(workspace, "standup", path).exit_code == 0
    before = (workspace / ".cruxible" / "files.yaml").read_bytes()
    refused = _add(workspace, "standup", str(workspace / "notes" / "status.md"))
    assert refused.exit_code != 0
    assert "cruxible.files.catalog_edit_refused (entry_exists)" in refused.output
    assert (workspace / ".cruxible" / "files.yaml").read_bytes() == before
    replaced = _add(workspace, "standup", str(workspace / "notes" / "status.md"), "--replace")
    assert replaced.exit_code == 0, replaced.output
    assert "Replaced file standup -> notes/status.md" in replaced.output


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"document_id": "status"}, "entry_invalid"),
        ({"name": "Bad Name"}, "name_invalid"),
    ],
    ids=["partial-document", "malformed-name"],
)
def test_an_invalid_entry_refuses_typed_and_writes_nothing(
    workspace: Path, fields: dict[str, object], reason: str
) -> None:
    name = str(fields.pop("name", "status-page"))
    with pytest.raises(FileCatalogEditError) as raised:
        add_catalog_entry(
            workspace, name=name, path=workspace / "notes" / "status.md", fields=fields
        )
    assert raised.value.reason == reason
    assert not (workspace / ".cruxible" / "files.yaml").exists()


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
    with pytest.raises(FileCatalogEditError) as raised:
        add_catalog_entry(
            workspace, name="two", path=workspace / "notes" / "standup.md", fields=document
        )
    assert raised.value.reason == "entry_invalid"


def test_paths_outside_the_workspace_or_missing_refuse(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x\n", encoding="utf-8")
    with pytest.raises(FileCatalogEditError) as raised:
        add_catalog_entry(workspace, name="out", path=outside, fields={})
    assert raised.value.reason == "path_outside_workspace"
    with pytest.raises(FileCatalogEditError) as missing:
        add_catalog_entry(workspace, name="gone", path=workspace / "notes" / "gone.md", fields={})
    assert missing.value.reason == "path_not_a_file"


def test_remove_drops_one_entry_and_the_file_with_the_last(workspace: Path) -> None:
    assert _add(workspace, "standup", str(workspace / "notes" / "standup.md")).exit_code == 0
    assert _add(workspace, "status-page", str(workspace / "notes" / "status.md")).exit_code == 0
    runner = CliRunner()
    first = runner.invoke(cli, ["files", "remove", "standup", "--root", str(workspace)])
    assert first.exit_code == 0, first.output
    entries = yaml.safe_load((workspace / ".cruxible" / "files.yaml").read_text())["entries"]
    assert [entry["name"] for entry in entries] == ["status-page"]
    missing = runner.invoke(cli, ["files", "remove", "standup", "--root", str(workspace)])
    assert missing.exit_code != 0
    assert "entry_missing" in missing.output
    last = runner.invoke(cli, ["files", "remove", "status-page", "--root", str(workspace)])
    assert last.exit_code == 0, last.output
    assert "the catalog file is gone" in last.output
    assert not (workspace / ".cruxible" / "files.yaml").exists()


@pytest.mark.parametrize(
    "relative",
    [
        ".cruxible/sources.yaml",
        "sources.yaml",
        ".cruxible/sources.local.yaml",
        "sources.local.yaml",
    ],
)
def test_old_catalog_names_refuse_with_a_rename_repair_and_no_reads(
    workspace: Path, relative: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.authoring.files import load_file_catalog
    from cruxible_client.contracts.file_catalog import FileCatalogRenameRequired

    old = workspace / relative
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"invalid YAML; must never be read")
    new = old.with_name(old.name.replace("sources", "files", 1))

    def no_read(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("legacy catalog contents must not be read")

    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "read_bytes", no_read)
        for action in (
            lambda: WorkspaceFiles(workspace),
            lambda: add_catalog_entry(
                workspace, name="standup", path=workspace / "notes/standup.md", fields={}
            ),
            lambda: load_file_catalog(old, None),
        ):
            with pytest.raises(FileCatalogRenameRequired) as refused:
                action()
            assert refused.value.error_code == "cruxible.files.catalog_rename_required"
            assert refused.value.renames == ((old, new),)
            assert f"rename {old} to {new}" in str(refused.value)
    assert old.read_bytes() == b"invalid YAML; must never be read"
    assert not new.exists()
    result = _add(workspace, "standup", str(workspace / "notes/standup.md"))
    assert result.exit_code != 0
    assert "cruxible.files.catalog_rename_required" in result.output
    assert str(new) in result.output


def test_old_overlay_is_not_silently_ignored_with_a_new_portable_catalog(workspace: Path) -> None:
    from cruxible_client.contracts.file_catalog import FileCatalogRenameRequired

    assert _add(workspace, "standup", str(workspace / "notes/standup.md")).exit_code == 0
    old = workspace / ".cruxible/sources.local.yaml"
    old.write_text("not read")
    with pytest.raises(FileCatalogRenameRequired):
        WorkspaceFiles(workspace)


def test_public_catalog_cli_cuts_old_names() -> None:
    runner = CliRunner()
    old = runner.invoke(cli, ["sources", "--help"])
    assert old.exit_code != 0
    assert "No such command 'sources'" in old.output
    repin = runner.invoke(cli, ["block", "repin", "--help"])
    assert repin.exit_code == 0
    assert "FILE BLOCK_ID" in repin.output
    propose = runner.invoke(cli, ["files", "propose", "--help"])
    assert propose.exit_code == 0
    assert "--file" in propose.output
    assert "--source" not in propose.output


def test_depublish_help_names_the_stored_file_identity() -> None:
    result = CliRunner().invoke(cli, ["block", "depublish", "--help"])
    assert result.exit_code == 0
    assert "FILE_ID BLOCK_ID" in result.output
    assert "SOURCE_ID" not in result.output
    assert (
        "the stored projection file identity; the file and catalog entry need not still exist"
        in " ".join(result.output.split())
    )


def test_add_text_names_the_document_an_entry_declares(workspace: Path) -> None:
    result = _add(
        workspace,
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
    )
    assert result.exit_code == 0, result.output
    assert "Added file status-page -> notes/status.md (Document status)" in result.output


def test_the_sdk_catalogues_a_file_exactly_as_the_cli_does(workspace: Path, tmp_path: Path) -> None:
    """F23: ``cx.files.add`` shares ``cruxible files add``'s implementation."""

    from typing import Any, cast

    from cruxible_client.authoring.sdk import Cruxible

    twin = tmp_path / "twin"
    (twin / "notes").mkdir(parents=True)
    (twin / "notes" / "status.md").write_text("# Status\n", encoding="utf-8")
    cli_result = CliRunner().invoke(
        cli,
        [
            "files", "add", "status-page", str(workspace / "notes" / "status.md"),
            "--document-id", "status", "--document-kind", "page", "--title", "Status",
            "--scope", "team", "--scope", "ops", "--root", str(workspace), "--json",
        ],
    )  # fmt: skip
    assert cli_result.exit_code == 0, cli_result.output
    cx = Cruxible(
        client=cast(Any, None),
        instance_id="inst",
        workspace=twin,
        access_profile=cast(Any, None),
        clock=None,
    )
    edit = cx.files.add(
        "status-page",
        "notes/status.md",
        document_id="status",
        document_kind="page",
        title="Status",
        governance_scope=("team", "ops"),
    )
    assert edit.model_dump(mode="json") == json.loads(cli_result.output)
    assert (twin / ".cruxible" / "files.yaml").read_bytes() == (
        workspace / ".cruxible" / "files.yaml"
    ).read_bytes()
    with pytest.raises(FileCatalogEditError) as refused:
        cx.files.add("status-page", "notes/status.md")
    assert refused.value.reason == "entry_exists"
    assert cx.files.remove("status-page").catalog_removed


def test_name_is_optional_and_a_bad_one_refuses_with_a_valid_example(
    workspace: Path, tmp_path: Path
) -> None:
    """Walkthrough S15: PATH alone takes the name a citing write gives it."""

    from cruxible_client.authoring.files import WorkspaceCatalog

    refused = _add(workspace, "notes/standup.md", str(workspace / "notes" / "standup.md"))
    assert refused.exit_code != 0
    assert "'notes/standup.md' is not a catalog name" in refused.output
    assert "e.g. doc-notes-standup.md" in refused.output
    assert "pattern" not in refused.output and "pydantic" not in refused.output
    assert not (workspace / ".cruxible" / "files.yaml").exists()

    added = _add(workspace, str(workspace / "notes" / "standup.md"))
    assert added.exit_code == 0, added.output
    assert "Added file doc-notes-standup.md -> notes/standup.md" in added.output
    assert _add(workspace, "a", "b", "c").exit_code != 0

    # The SDK takes the same one-argument form.
    edit = WorkspaceCatalog(workspace).add("notes/status.md")
    assert (edit.name, edit.entry.locator) == ("doc-notes-status.md", "notes/status.md")
