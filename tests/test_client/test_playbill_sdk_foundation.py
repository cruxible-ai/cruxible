from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cruxible_client.authoring.sdk_types import Duration, InsertionOperation
from cruxible_client.authoring.selectors import WorkspaceSources


def _catalog(path: Path) -> None:
    (path / ".playbill").mkdir()
    (path / ".playbill" / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: corpus.runbook
    locator: corpus/runbook.md
    document_id: runbook
    document_kind: runbook
    title: Runbook
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    approval_roles: [owner]
    governance_scope: [Document:runbook]
""",
        encoding="utf-8",
    )


def test_duration_uses_the_frozen_integer_microsecond_wire() -> None:
    assert Duration.days(count=2).model_dump() == {
        "tag": "playbill-duration-v1",
        "microseconds": 172_800_000_000,
    }
    with pytest.raises(ValueError, match="nonnegative integer"):
        Duration(value=1.5)  # type: ignore[arg-type]


def test_workspace_selector_lowers_evidence_and_insertion_from_exact_bytes(
    tmp_path: Path,
) -> None:
    _catalog(tmp_path)
    source = tmp_path / "corpus" / "runbook.md"
    source.parent.mkdir()
    content = b"# Runbook\n\nPatch critical systems within 48 hours.\n"
    source.write_bytes(content)

    selected = WorkspaceSources(tmp_path).select("corpus/runbook.md")
    observation = selected.anchor("within 48 hours").observation()
    assert observation.source_id == "corpus.runbook"
    assert observation.selected_content == b"within 48 hours"
    assert observation.coordinate.source_content_digest == (
        "sha256:" + hashlib.sha256(content).hexdigest()
    )

    insertion = selected.insertion(
        operation=InsertionOperation.AFTER,
        anchor="within 48 hours",
    ).target(b" (governed)")
    assert insertion.operation == "insert_after"
    assert insertion.selector.insertion_offset == content.index(b"within 48 hours") + 15


def test_selector_refuses_ambiguous_and_unmapped_paths(tmp_path: Path) -> None:
    _catalog(tmp_path)
    source = tmp_path / "corpus" / "runbook.md"
    source.parent.mkdir()
    source.write_text("same same", encoding="utf-8")
    workspace = WorkspaceSources(tmp_path)

    with pytest.raises(ValueError, match="2 occurrences"):
        workspace.select(source).anchor("same")
    with pytest.raises(ValueError, match="maps to 0 logical sources"):
        workspace.select("corpus/other.md")
