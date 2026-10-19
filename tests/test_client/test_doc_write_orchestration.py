"""Admission precedes local writes; CAS rebases only unchanged target sections."""

from pathlib import Path

import pytest
import yaml

from cruxible_client.authoring.doc_writes import write_workspace
from cruxible_client.contracts.doc_sections import parse_sections
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.get_reads import GetCoordinate
from cruxible_client.contracts.write import AppendSection, ReplaceSection, WriteOutcome, WriteRequest


def _answer(status="accepted"):
    return WriteOutcome(status=status, coordinate=GetCoordinate(git_oid="a" * 12, generation=1))


def test_create_is_written_after_acceptance_and_catalog_retry_is_unique(tmp_path: Path) -> None:
    request = WriteRequest(because="Prose", docs=(AppendSection(file="chosen/notes.md", text="# A\nwords"),))
    def submit(observed):
        assert not (tmp_path / "chosen/notes.md").exists()
        assert observed.docs[0].source_id == "doc-chosen-notes.md"
        assert "chosen/notes.md" not in observed.model_dump_json()
        return _answer()
    result = write_workspace(request, workspace=tmp_path, submit=submit)
    assert result.docs[0].written
    assert (tmp_path / "chosen/notes.md").read_bytes() == b"# A\nwords\n"
    retried = write_workspace(request, workspace=tmp_path, submit=lambda _: _answer())
    assert retried.docs[0].written
    catalog = yaml.safe_load((tmp_path / ".cruxible/files.yaml").read_text())
    assert len(catalog["entries"]) == 1


@pytest.mark.parametrize("status,dry_run", [("refused", False), ("would_accept", True), ("awaiting_approval", False)])
def test_refusal_and_dry_run_leave_bytes_while_awaiting_approval_writes(tmp_path: Path, status, dry_run) -> None:
    request = WriteRequest(because="Prose", docs=(AppendSection(file="notes.md", text="# A"),), dry_run=dry_run)
    result = write_workspace(request, workspace=tmp_path, submit=lambda _: _answer(status))
    assert result.docs[0].written == (status == "awaiting_approval")
    assert (tmp_path / "notes.md").exists() == (status == "awaiting_approval")


@pytest.mark.parametrize("same", [False, True])
def test_whole_file_cas_rebases_only_unrelated_edits(tmp_path: Path, same: bool) -> None:
    path = tmp_path / "notes.md"
    path.write_bytes(b"# A\nold\n# B\nother\n")
    request = WriteRequest(because="Prose", docs=(ReplaceSection(file="notes.md", section=("A",), text="# A\nnew"),))
    def submit(_):
        path.write_bytes(b"# A\nconcurrent\n# B\nother\n" if same else b"# A\nold\n# B\nconcurrent\n")
        return _answer()
    result = write_workspace(request, workspace=tmp_path, submit=submit)
    assert result.docs[0].written is not same
    if same:
        assert result.docs[0].refusal.code == "cruxible.doc.doc_write_conflict"
        assert result.docs[0].refusal.repair
        assert b"concurrent" in path.read_bytes()
    else:
        assert path.read_bytes() == b"# A\nnew\n# B\nconcurrent\n"


def test_stale_explicit_section_cas_refuses_before_submit(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_bytes(b"# A\nold\n")
    digest = parse_sections(path.read_bytes()).section(("A",)).digest
    path.write_bytes(b"# A\nchanged\n")
    with pytest.raises(WriteRefusalError) as caught:
        write_workspace(WriteRequest(because="Prose", docs=(ReplaceSection(
            file="notes.md", section=("A",), text="# A\nnew", expect=digest,
        ),)), workspace=tmp_path, submit=lambda _: pytest.fail("must not submit a stale edit"))
    assert caught.value.error_code == "cruxible.doc.section_changed"
    assert caught.value.repair_line


@pytest.mark.parametrize("path", [".cruxible/floor/notes.md", ".CrUxIbLe/notes.md", ".git/notes.md"])
def test_protected_paths_refuse_before_submit(tmp_path: Path, path: str) -> None:
    with pytest.raises(WriteRefusalError, match="protected_path"):
        write_workspace(WriteRequest(because="Prose", docs=(AppendSection(file=path, text="# A"),)),
            workspace=tmp_path, submit=lambda _: pytest.fail("must refuse locally"))


def test_symlink_into_floor_refuses(tmp_path: Path) -> None:
    floor = tmp_path / ".cruxible/floor"
    floor.mkdir(parents=True)
    (floor / "notes.md").write_text("# A\n")
    (tmp_path / "alias").symlink_to(floor, target_is_directory=True)
    with pytest.raises(WriteRefusalError, match="protected_path"):
        write_workspace(WriteRequest(because="Prose", docs=(AppendSection(file="alias/notes.md", text="# B"),)),
            workspace=tmp_path, submit=lambda _: pytest.fail("must refuse locally"))
