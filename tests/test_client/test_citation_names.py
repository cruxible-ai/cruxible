"""A citation names a workspace file by its path or its catalog name, through one resolver."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.authoring.selectors import catalogued_path, workspace_files
from cruxible_client.authoring.write_evidence import observe_evidence, rebind_evidence
from cruxible_client.contracts.write import FileEvidence, SectionEvidence

_CATALOG = """\
catalog_kind: portable
entries:
  - name: repo.notes
    locator: docs/notes.md
"""


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / ".cruxible").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("# Notes\n\nTitle: one\n\n## Review\n\nLooked.\n")
    (tmp_path / ".cruxible" / "files.yaml").write_text(_CATALOG)
    return tmp_path


def test_a_path_wins_and_a_name_maps_to_its_file(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert catalogued_path(workspace, "docs/notes.md") == "docs/notes.md"
    assert catalogued_path(workspace, "repo.notes") == "docs/notes.md"
    # Neither a file nor a name: unchanged, for the caller to refuse as a path.
    assert catalogued_path(workspace, "missing.md") == "missing.md"
    files = workspace_files(workspace)
    assert files.select("repo.notes").path == files.select("docs/notes.md").path


def test_section_and_file_evidence_by_name_observe_as_by_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    for by_name, by_path in (
        (
            SectionEvidence(file="repo.notes", section=("Notes", "Review")),
            SectionEvidence(file="docs/notes.md", section=("Notes", "Review")),
        ),
        (FileEvidence(file="repo.notes#Title: one"), FileEvidence(file="docs/notes.md#Title: one")),
    ):
        named = observe_evidence(by_name, workspace=workspace)
        pathed = observe_evidence(by_path, workspace=workspace)
        assert named is not None and pathed is not None
        assert named.observation == pathed.observation  # type: ignore[union-attr]
        assert named.observation.source_id == "repo.notes"  # type: ignore[union-attr]


def test_rebind_and_attest_evidence_resolve_a_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    section = rebind_evidence(section="repo.notes#Notes/Review", workspace=workspace)
    assert isinstance(section, SectionEvidence) and section.file == "docs/notes.md"
