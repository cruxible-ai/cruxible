from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cruxible_client.authoring.sdk_types import Duration, InsertionOperation
from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.authoring.workspace import (
    PlaybillWorkspaceError,
    observe_playbill_next_workspace,
)


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


def test_next_workspace_observes_confined_whole_source_bytes(tmp_path: Path) -> None:
    _catalog(tmp_path)
    source = tmp_path / "corpus" / "runbook.md"
    source.parent.mkdir()
    source.write_bytes(b"# Runbook\nOriginal source.\n")

    observation = observe_playbill_next_workspace(tmp_path)

    assert observation["source_observations"] == [
        {
            "source_id": "corpus.runbook",
            "observed_source_digest": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    ]
    source.write_bytes(b"# Runbook\nChanged source.\n")
    changed = observe_playbill_next_workspace(tmp_path)
    assert changed["source_observations"] != observation["source_observations"]


def test_next_workspace_without_a_catalog_does_not_claim_source_observation(
    tmp_path: Path,
) -> None:
    observation = observe_playbill_next_workspace(tmp_path)

    assert "source_observations" not in observation


def test_next_workspace_refuses_catalog_source_that_escapes_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _catalog(workspace)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "corpus").mkdir()
    (workspace / "corpus" / "runbook.md").symlink_to(outside)

    with pytest.raises((PlaybillWorkspaceError, ValueError), match="escapes the workspace"):
        observe_playbill_next_workspace(workspace)


def test_next_workspace_refuses_catalog_overlay_that_escapes_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _catalog(workspace)
    outside = tmp_path / "outside.yaml"
    outside.write_text("secret: true\n", encoding="utf-8")
    (workspace / ".playbill" / "sources.local.yaml").symlink_to(outside)

    with pytest.raises(PlaybillWorkspaceError, match="local source catalog escapes"):
        observe_playbill_next_workspace(workspace)
