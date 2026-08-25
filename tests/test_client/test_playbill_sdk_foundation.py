from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from cruxible_client.authoring.sdk_types import Duration, InsertionOperation
from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.authoring.workspace import observe_playbill_next_workspace


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


def test_next_workspace_observes_sorted_archival_presentation_policy(tmp_path: Path) -> None:
    _catalog(tmp_path)
    policy_path = tmp_path / ".playbill" / "presentation-policy.json"
    policy_path.write_text(
        '{"tag":"playbill-presentation-policy-v1","archival_source_ids":["corpus.runbook"]}',
        encoding="utf-8",
    )

    observation = observe_playbill_next_workspace(tmp_path)

    assert observation["presentation_policy"] == {
        "tag": "playbill-presentation-policy-v1",
        "archival_source_ids": ["corpus.runbook"],
    }
    assert observation["presentation_policy_notes"] == []


@pytest.mark.parametrize(
    ("payload", "expected_note"),
    [
        (
            '{"tag":"playbill-presentation-policy-v1","archival_source_ids":["unknown"]}',
            "presentation_policy_unknown_source_id",
        ),
        (
            "not-json",
            "presentation_policy_malformed",
        ),
    ],
)
def test_next_workspace_degrades_invalid_presentation_policy(
    tmp_path: Path,
    payload: str,
    expected_note: str,
) -> None:
    _catalog(tmp_path)
    (tmp_path / ".playbill" / "presentation-policy.json").write_text(payload, encoding="utf-8")

    observation = observe_playbill_next_workspace(tmp_path)

    assert observation["presentation_policy"] is None
    assert observation["presentation_policy_notes"] == [expected_note]


def test_next_workspace_degrades_unreadable_presentation_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog(tmp_path)
    policy_path = tmp_path / ".playbill" / "presentation-policy.json"
    policy_path.write_text('{"tag":"playbill-presentation-policy-v1"}', encoding="utf-8")
    original = Path.read_text

    def unreadable(path: Path, *args: object, **kwargs: object) -> str:
        if path == policy_path:
            raise PermissionError("policy denied")
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", unreadable)

    observation = observe_playbill_next_workspace(tmp_path)

    assert observation["presentation_policy"] is None
    assert observation["presentation_policy_notes"] == ["presentation_policy_unreadable"]


def test_next_workspace_degrades_escaping_presentation_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _catalog(workspace)
    outside = tmp_path / "outside-policy.json"
    outside.write_text('{"tag":"playbill-presentation-policy-v1"}', encoding="utf-8")
    os.symlink(outside, workspace / ".playbill" / "presentation-policy.json")

    observation = observe_playbill_next_workspace(workspace)

    assert observation["presentation_policy"] is None
    assert observation["presentation_policy_notes"] == ["presentation_policy_path_escape"]


def test_next_workspace_without_a_catalog_does_not_claim_source_observation(
    tmp_path: Path,
) -> None:
    observation = observe_playbill_next_workspace(tmp_path)

    assert "source_observations" not in observation


def test_next_workspace_omits_portable_catalog_source_that_escapes_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _catalog(workspace)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "corpus").mkdir()
    (workspace / "corpus" / "runbook.md").symlink_to(outside)

    observation = observe_playbill_next_workspace(workspace)

    assert observation["source_observations"] == []


def test_next_workspace_degrades_when_catalog_overlay_escapes_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _catalog(workspace)
    outside = tmp_path / "outside.yaml"
    outside.write_text("secret: true\n", encoding="utf-8")
    (workspace / ".playbill" / "sources.local.yaml").symlink_to(outside)

    observation = observe_playbill_next_workspace(workspace)

    assert "source_observations" not in observation


def test_next_workspace_degrades_when_local_overlay_ambiguously_retargets_source(
    tmp_path: Path,
) -> None:
    _catalog(tmp_path)
    portable = (tmp_path / ".playbill" / "sources.yaml").read_text(encoding="utf-8")
    (tmp_path / ".playbill" / "sources.local.yaml").write_text(
        portable.replace("catalog_kind: portable", "catalog_kind: local").replace(
            "document_id: runbook", "document_id: different-runbook"
        ),
        encoding="utf-8",
    )

    observation = observe_playbill_next_workspace(tmp_path)

    assert "source_observations" not in observation


def test_next_workspace_observes_absolute_source_from_explicit_local_overlay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _catalog(workspace)
    external = tmp_path / "external.md"
    external.write_bytes(b"Local overlay authorizes this absolute source.\n")
    portable = (workspace / ".playbill" / "sources.yaml").read_text(encoding="utf-8")
    (workspace / ".playbill" / "sources.local.yaml").write_text(
        portable.replace("catalog_kind: portable", "catalog_kind: local").replace(
            "locator: corpus/runbook.md", f"locator: {external}"
        ),
        encoding="utf-8",
    )

    observation = observe_playbill_next_workspace(workspace)

    assert observation["source_observations"] == [
        {
            "source_id": "corpus.runbook",
            "observed_source_digest": "sha256:" + hashlib.sha256(external.read_bytes()).hexdigest(),
        }
    ]


def test_next_workspace_omits_unresolved_root_alias_without_failing(tmp_path: Path) -> None:
    _catalog(tmp_path)
    portable = (tmp_path / ".playbill" / "sources.yaml").read_text(encoding="utf-8")
    (tmp_path / ".playbill" / "sources.local.yaml").write_text(
        portable.replace("catalog_kind: portable", "catalog_kind: local").replace(
            "locator: corpus/runbook.md", "locator: corpus/runbook.md\n    root_alias: external"
        ),
        encoding="utf-8",
    )

    observation = observe_playbill_next_workspace(tmp_path)

    assert observation["source_observations"] == []


def test_next_workspace_omits_missing_catalog_source_without_failing(tmp_path: Path) -> None:
    _catalog(tmp_path)

    observation = observe_playbill_next_workspace(tmp_path)

    assert observation["source_observations"] == []


def test_next_workspace_observes_root_level_source_catalog(tmp_path: Path) -> None:
    _catalog(tmp_path)
    (tmp_path / ".playbill" / "sources.yaml").rename(tmp_path / "sources.yaml")
    source = tmp_path / "corpus" / "runbook.md"
    source.parent.mkdir()
    source.write_bytes(b"Root-level portable source catalog.\n")

    observation = observe_playbill_next_workspace(tmp_path)

    assert observation["source_observations"] == [
        {
            "source_id": "corpus.runbook",
            "observed_source_digest": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    ]


def test_next_workspace_degrades_when_both_catalog_layouts_exist(tmp_path: Path) -> None:
    _catalog(tmp_path)
    portable = (tmp_path / ".playbill" / "sources.yaml").read_text(encoding="utf-8")
    (tmp_path / "sources.yaml").write_text(portable, encoding="utf-8")

    observation = observe_playbill_next_workspace(tmp_path)

    assert "source_observations" not in observation
