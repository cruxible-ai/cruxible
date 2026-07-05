"""Tests for source-backed evidence artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.service import (
    GroupMemberInput,
    GroupSignalInput,
    service_dereference_source_evidence,
    service_get_source_artifact,
    service_list_source_artifacts,
    service_propose_group_inputs,
    service_register_source_artifact,
)
from cruxible_core.service.evidence import resolve_evidence_refs

SOURCE_CONFIG_YAML = """\
version: "1.0"
name: source_evidence_demo

entity_types:
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      source:
        type: string
        optional: true
"""


def _instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(SOURCE_CONFIG_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="usr_source",
        org_id="org_1",
        operation_id="op_source",
        timestamp="2026-06-05T12:00:00Z",
    )


def _list_source_artifacts(instance: CruxibleInstance):
    store = instance.get_source_artifact_store()
    try:
        return store.list_artifacts()
    finally:
        store.close()


def _get_source_artifact(instance: CruxibleInstance, source_artifact_id: str):
    store = instance.get_source_artifact_store()
    try:
        return store.get_artifact(source_artifact_id)
    finally:
        store.close()


def test_register_source_content_happy_path(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    registered = service_register_source_artifact(
        instance,
        source_content="# Fitment\n\nInline BP-1001 evidence.\n",
        source_artifact_id="inline_fitment_doc",
        original_uri="memory:inline-fitment",
        label="inline fitment",
    )

    assert registered.source_artifact_id == "inline_fitment_doc"
    assert registered.original_uri == "memory:inline-fitment"
    assert registered.label == "inline fitment"
    assert registered.chunks
    stored = _get_source_artifact(instance, "inline_fitment_doc")
    assert stored is not None
    assert stored.local_path is None
    assert stored.original_uri == "memory:inline-fitment"
    assert stored.label == "inline fitment"
    assert stored.content_hash == registered.content_hash


def test_register_source_content_rejects_empty_content(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    with pytest.raises(ConfigError, match="did not produce any addressable chunks"):
        service_register_source_artifact(
            instance,
            source_content="",
            source_artifact_id="empty_inline_doc",
        )

    assert _list_source_artifacts(instance) == []


def test_register_source_artifact_persist_false_returns_record_without_writing(
    tmp_path: Path,
) -> None:
    instance = _instance(tmp_path)

    registered = service_register_source_artifact(
        instance,
        source_content="# Fitment\n\nDry-run BP-1001 evidence.\n",
        source_artifact_id="dry_run_fitment_doc",
        source_retention="archive",
        original_uri="memory:dry-run",
        label="dry run",
        persist=False,
    )

    assert registered.source_artifact_id == "dry_run_fitment_doc"
    assert registered.archived is True
    assert registered.archive_content_hash == registered.content_hash
    assert registered.original_uri == "memory:dry-run"
    assert registered.label == "dry run"
    assert registered.chunks
    assert _get_source_artifact(instance, "dry_run_fitment_doc") is None
    assert _list_source_artifacts(instance) == []
def test_list_source_artifacts_empty_and_paginated(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    empty = service_list_source_artifacts(instance, limit=10, offset=0)

    assert empty.items == []
    assert empty.total == 0
    assert empty.limit == 10
    assert empty.offset == 0
    assert empty.truncated is False

    first_path = tmp_path / "first.md"
    first_path.write_text("# First\n\nFirst source text.\n")
    second_path = tmp_path / "second.md"
    second_path.write_text("# Second\n\nSecond source text.\n")
    service_register_source_artifact(
        instance,
        source_path=str(second_path),
        source_artifact_id="source_b",
        label="second",
    )
    service_register_source_artifact(
        instance,
        source_path=str(first_path),
        source_artifact_id="source_a",
        label="first",
    )

    page = service_list_source_artifacts(instance, limit=1, offset=1)

    assert page.total == 2
    assert page.limit == 1
    assert page.offset == 1
    assert page.truncated is False
    assert [item.source_artifact_id for item in page.items] == ["source_b"]
    assert page.items[0].kind == "markdown"
    assert page.items[0].retention == "manifest_only"
    assert page.items[0].chunk_count > 0
    assert page.items[0].byte_count == second_path.stat().st_size


def test_get_source_artifact_returns_ordered_chunks_with_text(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Evidence\n\nFirst paragraph.\n\nSecond paragraph.\n")
    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="readable_source",
        label="readable",
    )

    result = service_get_source_artifact(
        instance,
        source_artifact_id=registered.source_artifact_id,
    )

    assert result.source_artifact_id == "readable_source"
    assert result.content_available is True
    assert result.body_origin == "local_path"
    assert result.chunk_count == len(registered.chunks)
    assert [chunk.line_start for chunk in result.chunks] == sorted(
        chunk.line_start for chunk in result.chunks
    )
    paragraph = next(chunk for chunk in result.chunks if chunk.block_selector == "paragraph:1")
    assert paragraph.text == "First paragraph."


def test_get_source_artifact_manifest_only_missing_file_omits_text(
    tmp_path: Path,
) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "missing.md"
    source_path.write_text("# Evidence\n\nTransient source text.\n")
    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_retention="manifest_only",
        source_artifact_id="missing_source",
    )
    source_path.unlink()

    result = service_get_source_artifact(
        instance,
        source_artifact_id=registered.source_artifact_id,
    )

    assert result.content_available is False
    assert result.content_unavailable_reason == "local source path is unavailable"
    assert result.chunks
    assert all(chunk.text is None for chunk in result.chunks)


def test_manifest_only_source_artifact_reports_local_drift(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Fitment\n\nBrake pad BP-1001 fits Civic 2024.\n")

    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_retention="manifest_only",
        label="fitment table",
    )
    paragraph = next(chunk for chunk in registered.chunks if chunk.block_selector == "paragraph:1")

    dereferenced = service_dereference_source_evidence(
        instance,
        source_artifact_id=registered.source_artifact_id,
        chunk_id=paragraph.chunk_id,
    )
    assert dereferenced.status == "available"
    assert dereferenced.body_origin == "local_path"
    assert dereferenced.body == "Brake pad BP-1001 fits Civic 2024."

    source_path.write_text("# Fitment\n\nBrake pad BP-1001 no longer fits Civic 2024.\n")
    drifted = service_dereference_source_evidence(
        instance,
        source_artifact_id=registered.source_artifact_id,
        chunk_id=paragraph.chunk_id,
    )
    assert drifted.status == "drifted"
    assert drifted.reason == "local source content hash does not match registered manifest"


def test_archive_source_artifact_dereferences_after_local_file_changes(
    tmp_path: Path,
) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Fitment\n\nArchived BP-1001 evidence.\n")

    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_retention="archive",
    )
    paragraph = next(chunk for chunk in registered.chunks if chunk.block_selector == "paragraph:1")
    source_path.write_text("# Fitment\n\nChanged local evidence.\n")

    dereferenced = service_dereference_source_evidence(
        instance,
        source_artifact_id=registered.source_artifact_id,
        heading_path=["Fitment"],
        block_selector="paragraph:1",
    )
    assert dereferenced.status == "available"
    assert dereferenced.body_origin == "archive"
    assert dereferenced.chunk is not None
    assert dereferenced.chunk.chunk_id == paragraph.chunk_id
    assert dereferenced.body == "Archived BP-1001 evidence."


def test_resolve_evidence_refs_merges_explicit_and_source_refs(
    tmp_path: Path,
) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "fitment.md"
    source_path.write_text("# Fitment\n\nBP-1001 evidence row.\n")
    registered = service_register_source_artifact(instance, source_path=str(source_path))
    paragraph = next(chunk for chunk in registered.chunks if chunk.block_selector == "paragraph:1")

    refs = resolve_evidence_refs(
        instance,
        evidence_refs=[
            {"source": "doc", "source_record_id": "section-1"},
            {"source": "doc", "source_record_id": "section-1"},
        ],
        source_evidence=[
            {
                "source_artifact_id": registered.source_artifact_id,
                "chunk_id": paragraph.chunk_id,
            }
        ],
    )

    assert [ref.source for ref in refs] == ["doc", "source_artifact"]
    assert refs[0].source_record_id == "section-1"
    assert refs[1].artifact_id == registered.source_artifact_id
    assert refs[1].source_record_id == paragraph.chunk_id


def test_source_evidence_resolves_to_stored_group_evidence_refs(
    tmp_path: Path,
) -> None:
    instance = _instance(tmp_path)
    actor = _actor()
    source_path = tmp_path / "fitment.md"
    source_path.write_text("# Fitment\n\nBP-1001 evidence row.\n")
    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        actor_context=actor,
    )
    paragraph = next(chunk for chunk in registered.chunks if chunk.block_selector == "paragraph:1")

    result = service_propose_group_inputs(
        instance,
        "fits",
        [
            GroupMemberInput(
                from_type="Part",
                from_id="BP-1001",
                to_type="Vehicle",
                to_id="V-2024-CIVIC",
                relationship_type="fits",
                signals=[
                    GroupSignalInput(
                        signal_source="catalog",
                        signal="support",
                        source_evidence=[
                            {
                                "source_artifact_id": registered.source_artifact_id,
                                "chunk_id": paragraph.chunk_id,
                                "label": "catalog row",
                            }
                        ],
                    )
                ],
                source_evidence=[
                    {
                        "source_artifact_id": registered.source_artifact_id,
                        "heading_path": ["Fitment"],
                        "block_selector": "paragraph:1",
                    }
                ],
            )
        ],
        thesis_facts={"source": "catalog"},
        actor_context=actor,
    )

    assert result.group_id is not None
    artifact_store = instance.get_source_artifact_store()
    try:
        stored_artifact = artifact_store.get_artifact(registered.source_artifact_id)
    finally:
        artifact_store.close()
    assert stored_artifact is not None
    assert stored_artifact.registered_actor_context is not None
    assert stored_artifact.registered_actor_context.actor_id == "usr_source"

    group_store = instance.get_group_store()
    try:
        group = group_store.get_group(result.group_id)
        members = group_store.get_members(result.group_id)
    finally:
        group_store.close()

    assert group is not None
    assert group.proposed_actor_context is not None
    assert group.proposed_actor_context.operation_id == "op_source"

    assert len(members) == 1
    member_ref = members[0].evidence_refs[0]
    assert member_ref.source == "source_artifact"
    assert member_ref.artifact_id == registered.source_artifact_id
    assert member_ref.source_record_id == paragraph.chunk_id
    assert member_ref.metadata["content_hash"] == paragraph.content_hash
    assert member_ref.metadata["operation_id"] == "op_source"
    assert member_ref.metadata["actor_context"]["actor_id"] == "usr_source"

    signal_ref = members[0].signals[0].evidence_refs[0]
    assert signal_ref.source == "source_artifact"
    assert signal_ref.artifact_id == registered.source_artifact_id
    assert signal_ref.source_record_id == paragraph.chunk_id
    assert signal_ref.label == "catalog row"
    assert signal_ref.metadata["operation_id"] == "op_source"


def test_absolute_source_path_outside_instance_root_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proven exploit: absolute path outside the instance root, no allowed-roots."""
    monkeypatch.delenv("CRUXIBLE_ALLOWED_ROOTS", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    instance = _instance(project)

    outside = tmp_path / "outside-secret.md"
    outside.write_text("# Secret\n\nMust not be readable.\n")

    with pytest.raises(ConfigError, match="must stay within the registered workspace"):
        service_register_source_artifact(instance, source_path=str(outside.resolve()))


def test_symlink_escape_from_instance_root_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink inside the instance root resolving outside is rejected."""
    monkeypatch.delenv("CRUXIBLE_ALLOWED_ROOTS", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    instance = _instance(project)

    outside = tmp_path / "outside-secret.md"
    outside.write_text("# Secret\n\nReached via symlink.\n")
    link = project / "link.md"
    link.symlink_to(outside)

    with pytest.raises(ConfigError, match="must stay within the registered workspace"):
        service_register_source_artifact(instance, source_path="link.md")
    with pytest.raises(ConfigError, match="must stay within the registered workspace"):
        service_register_source_artifact(instance, source_path=str(link))


def test_absolute_source_path_inside_instance_root_allowed(tmp_path: Path) -> None:
    """A legitimate absolute path within the instance root still registers."""
    project = tmp_path / "project"
    project.mkdir()
    instance = _instance(project)

    evidence = project / "evidence.md"
    evidence.write_text("# Fitment\n\nIn-workspace absolute path.\n")

    registered = service_register_source_artifact(instance, source_path=str(evidence.resolve()))
    assert registered.chunks


def test_explicit_allowed_root_permits_out_of_workspace_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit CRUXIBLE_ALLOWED_ROOTS entry permits out-of-instance reads."""
    project = tmp_path / "project"
    project.mkdir()
    instance = _instance(project)

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    evidence = allowed / "evidence.md"
    evidence.write_text("# Fitment\n\nExplicitly allowed root.\n")

    monkeypatch.setenv("CRUXIBLE_ALLOWED_ROOTS", str(allowed.resolve()))
    registered = service_register_source_artifact(instance, source_path=str(evidence.resolve()))
    assert registered.chunks


def test_explicit_allowed_root_rejects_relative_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative CRUXIBLE_ALLOWED_ROOTS entry is a config error, not a silent allow."""
    project = tmp_path / "project"
    project.mkdir()
    instance = _instance(project)
    evidence = project / "evidence.md"
    evidence.write_text("# Fitment\n\nWorkspace evidence.\n")

    monkeypatch.setenv("CRUXIBLE_ALLOWED_ROOTS", "relative/dir")
    with pytest.raises(ConfigError, match="contains relative path"):
        service_register_source_artifact(instance, source_path=str(evidence.resolve()))


def test_register_with_caller_supplied_id_roundtrips(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "opinion.md"
    source_path.write_text("# Holding\n\nChevron is overruled.\n")

    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="opinion_text_op_loper_bright",
    )
    assert registered.source_artifact_id == "opinion_text_op_loper_bright"

    paragraph = next(chunk for chunk in registered.chunks if chunk.block_selector == "paragraph:1")
    dereferenced = service_dereference_source_evidence(
        instance,
        source_artifact_id="opinion_text_op_loper_bright",
        chunk_id=paragraph.chunk_id,
    )
    assert dereferenced.status == "available"
    assert dereferenced.body == "Chevron is overruled."


def test_register_refuses_invalid_caller_supplied_id(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Doc\n\nBody.\n")

    for bad in ("ab", ".starts-with-dot", "has space", "x" * 65, "path/../traversal"):
        with pytest.raises(ConfigError, match="source_artifact_id must be"):
            service_register_source_artifact(
                instance,
                source_path=str(source_path),
                source_artifact_id=bad,
            )


def test_register_refuses_duplicate_caller_supplied_id(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Doc\n\nBody.\n")

    service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="pinned_evidence",
    )
    with pytest.raises(ConfigError, match="already registered"):
        service_register_source_artifact(
            instance,
            source_path=str(source_path),
            source_artifact_id="pinned_evidence",
        )
