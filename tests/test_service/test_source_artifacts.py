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


def _revisions(instance: CruxibleInstance, source_artifact_id: str):
    store = instance.get_source_artifact_store()
    try:
        return store.list_artifact_revisions(source_artifact_id)
    finally:
        store.close()


def test_reregistering_changed_content_supersedes_instead_of_overwriting(
    tmp_path: Path,
) -> None:
    """The manifest prior evidence refs pinned must survive a re-registration."""
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Doc\n\nOriginal body.\n")

    first = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="pinned_evidence",
    )
    original_paragraph = next(
        chunk for chunk in first.chunks if chunk.block_selector == "paragraph:1"
    )

    source_path.write_text("# Doc\n\nRewritten body.\n")
    second = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="pinned_evidence",
    )

    assert second.already_registered is False
    assert second.revision == 2
    assert second.artifact_revision_id == "pinned_evidence@2"
    assert second.supersedes == first.artifact_revision_id
    assert second.content_hash != first.content_hash

    revisions = _revisions(instance, "pinned_evidence")
    assert [record.revision for record in revisions] == [1, 2]
    assert revisions[0].content_hash == first.content_hash
    assert revisions[0].superseded_by == "pinned_evidence@2"
    assert revisions[0].superseded_at is not None
    assert revisions[1].superseded_by is None

    store = instance.get_source_artifact_store()
    try:
        # The superseded revision keeps its own chunk index; it was not
        # deleted and rebuilt against the new content.
        stale_chunks = store.list_revision_chunks(first.artifact_revision_id)
        assert {chunk.chunk_id for chunk in stale_chunks} >= {original_paragraph.chunk_id}
        stale = next(
            chunk for chunk in stale_chunks if chunk.chunk_id == original_paragraph.chunk_id
        )
        assert stale.content_hash == original_paragraph.content_hash
        assert store.get_artifact("pinned_evidence").artifact_revision_id == "pinned_evidence@2"
    finally:
        store.close()


def test_reregistering_identical_content_is_a_noop(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Doc\n\nBody.\n")

    first = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="pinned_evidence",
    )
    second = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="pinned_evidence",
        label="ignored relabel",
    )

    assert second.already_registered is True
    assert second.revision == 1
    assert second.artifact_revision_id == first.artifact_revision_id
    assert second.supersedes is None
    # The stored manifest is untouched, so the ignored relabel is not applied.
    assert second.label is None
    assert [record.revision for record in _revisions(instance, "pinned_evidence")] == [1]


def test_registration_mints_a_receipt(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Doc\n\nBody.\n")

    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="receipted_evidence",
        actor_context=_actor(),
    )

    assert registered.receipt_id is not None
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(registered.receipt_id)
    finally:
        store.close()
    assert receipt is not None
    assert receipt.operation_type == "source_artifact_register"
    assert receipt.committed is True
    assert receipt.actor_context is not None
    assert receipt.actor_context.actor_id == "usr_source"
    detail = next(node.detail for node in receipt.nodes if node.node_type == "validation")
    assert detail["outcome"] == "registered"
    assert detail["artifact_revision_id"] == "receipted_evidence@1"


def test_dry_run_registration_mints_no_receipt(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    registered = service_register_source_artifact(
        instance,
        source_content="# Doc\n\nDry run body.\n",
        source_artifact_id="dry_run_receipt_check",
        persist=False,
    )

    assert registered.receipt_id is None
    assert registered.artifact_revision_id == "dry_run_receipt_check@1"
    store = instance.get_receipt_store()
    try:
        assert store.count_receipts(operation_type="source_artifact_register") == 0
    finally:
        store.close()


def test_detected_drift_is_persisted_and_visible_to_later_reads(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Doc\n\nOriginal body.\n")
    registered = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_artifact_id="drifting_evidence",
    )

    clean = service_get_source_artifact(instance, source_artifact_id="drifting_evidence")
    assert clean.drift_observed_hash is None

    source_path.write_text("# Doc\n\nTampered body.\n")
    drifted = service_get_source_artifact(instance, source_artifact_id="drifting_evidence")
    assert drifted.content_available is False
    assert drifted.drift_observed_hash is not None
    assert drifted.drift_observed_hash != registered.content_hash
    observed_at = drifted.drift_observed_at
    assert observed_at is not None

    # The finding survives the read that produced it: a later reader sees the
    # recorded drift without recomputing it.
    stored = _get_source_artifact(instance, "drifting_evidence")
    assert stored is not None
    assert stored.drift_observed_hash == drifted.drift_observed_hash
    assert stored.drift_observed_at == observed_at

    # Restoring the file clears the marker, so it never misreports current state.
    source_path.write_text("# Doc\n\nOriginal body.\n")
    restored = service_get_source_artifact(instance, source_artifact_id="drifting_evidence")
    assert restored.content_available is True
    assert restored.drift_observed_hash is None


# ---------------------------------------------------------------------------
# Revision-pinned dereference
# ---------------------------------------------------------------------------


def test_a_revision_1_citation_still_retrieves_revision_1_content(tmp_path: Path) -> None:
    """The whole point of keeping revisions is being able to read them back.

    ``EvidenceRef`` retained only the LOGICAL artifact id, and dereference always
    resolved to the current revision — so a citation made against revision 1
    silently returned revision 2's text once the document was re-registered,
    even though revision 1's chunks and archived bytes were still stored.
    """
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Fitment\n\nOriginal BP-1001 claim.\n")
    first = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_retention="archive",
    )
    assert first.artifact_revision_id.endswith("@1")

    source_path.write_text("# Fitment\n\nRevised BP-1001 claim.\n")
    second = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_retention="archive",
        source_artifact_id=first.source_artifact_id,
    )
    assert second.artifact_revision_id.endswith("@2")

    pinned = service_dereference_source_evidence(
        instance,
        source_artifact_id=first.source_artifact_id,
        artifact_revision_id=first.artifact_revision_id,
        heading_path=["Fitment"],
        block_selector="paragraph:1",
    )
    assert pinned.status == "available"
    assert pinned.body == "Original BP-1001 claim."
    assert pinned.artifact_revision_id == first.artifact_revision_id
    assert pinned.revision_unpinned is False


def test_an_unpinned_dereference_says_so_instead_of_pretending(tmp_path: Path) -> None:
    """Falling back to the current revision is fine; doing it silently is not."""
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Fitment\n\nOriginal BP-1001 claim.\n")
    first = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_retention="archive",
    )
    source_path.write_text("# Fitment\n\nRevised BP-1001 claim.\n")
    second = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_retention="archive",
        source_artifact_id=first.source_artifact_id,
    )

    unpinned = service_dereference_source_evidence(
        instance,
        source_artifact_id=first.source_artifact_id,
        heading_path=["Fitment"],
        block_selector="paragraph:1",
    )
    assert unpinned.status == "available"
    assert unpinned.body == "Revised BP-1001 claim."
    assert unpinned.artifact_revision_id == second.artifact_revision_id
    assert unpinned.revision_unpinned is True


def test_resolved_evidence_refs_pin_the_revision_they_were_made_against(
    tmp_path: Path,
) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Fitment\n\nOriginal BP-1001 claim.\n")
    first = service_register_source_artifact(instance, source_path=str(source_path))

    refs = resolve_evidence_refs(
        instance,
        evidence_refs=[],
        source_evidence=[
            {
                "source_artifact_id": first.source_artifact_id,
                "heading_path": ["Fitment"],
                "block_selector": "paragraph:1",
            }
        ],
    )

    assert [ref.artifact_revision_id for ref in refs] == [first.artifact_revision_id]


def test_a_pinned_read_of_an_unretained_revision_is_not_a_tamper_finding(
    tmp_path: Path,
) -> None:
    """Replaying a pinned citation must never manufacture a drift record.

    Under the default ``manifest_only`` retention a superseded revision's bytes
    are gone: the local path holds the CURRENT revision's content. Hashing it
    against revision 1's manifest is a guaranteed mismatch that says nothing
    about tampering — yet the read reported ``drifted`` and called
    ``record_content_drift``, permanently stamping the sticky
    ``first_drift_observed_hash``/``_at`` pair on a revision nobody touched.
    """
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Fitment\n\nFirst BP-1001 claim.\n")
    first = service_register_source_artifact(instance, source_path=str(source_path))
    for body in ("Second BP-1001 claim.", "Third BP-1001 claim."):
        source_path.write_text(f"# Fitment\n\n{body}\n")
        service_register_source_artifact(
            instance,
            source_path=str(source_path),
            source_artifact_id=first.source_artifact_id,
        )

    pinned = service_dereference_source_evidence(
        instance,
        source_artifact_id=first.source_artifact_id,
        artifact_revision_id=first.artifact_revision_id,
        heading_path=["Fitment"],
        block_selector="paragraph:1",
    )

    assert pinned.status == "revision_bytes_not_retained"
    assert pinned.body is None
    assert pinned.reason is not None
    assert "not retained" in pinned.reason
    assert pinned.artifact_revision_id == first.artifact_revision_id
    assert pinned.revision_unpinned is False

    # No drift record was written against the replayed revision — neither the
    # clearable pair nor the sticky one.
    store = instance.get_source_artifact_store()
    try:
        replayed = store.get_artifact_revision(first.artifact_revision_id)
    finally:
        store.close()
    assert replayed is not None
    assert replayed.drift_observed_hash is None
    assert replayed.drift_observed_at is None
    assert replayed.first_drift_observed_hash is None
    assert replayed.first_drift_observed_at is None


def test_archive_retention_still_serves_a_superseded_revision(tmp_path: Path) -> None:
    """The new status is scoped to unretained bytes; archived revisions replay."""
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Fitment\n\nFirst BP-1001 claim.\n")
    first = service_register_source_artifact(
        instance,
        source_path=str(source_path),
        source_retention="archive",
    )
    for body in ("Second BP-1001 claim.", "Third BP-1001 claim."):
        source_path.write_text(f"# Fitment\n\n{body}\n")
        service_register_source_artifact(
            instance,
            source_path=str(source_path),
            source_artifact_id=first.source_artifact_id,
            source_retention="archive",
        )

    pinned = service_dereference_source_evidence(
        instance,
        source_artifact_id=first.source_artifact_id,
        artifact_revision_id=first.artifact_revision_id,
        heading_path=["Fitment"],
        block_selector="paragraph:1",
    )

    assert pinned.status == "available"
    assert pinned.body == "First BP-1001 claim."
    assert pinned.body_origin == "archive"

    store = instance.get_source_artifact_store()
    try:
        replayed = store.get_artifact_revision(first.artifact_revision_id)
    finally:
        store.close()
    assert replayed is not None
    assert replayed.first_drift_observed_hash is None


def test_a_pin_naming_another_artifact_is_refused(tmp_path: Path) -> None:
    """A cross-artifact pin is a corrupt citation, not a stale one."""
    instance = _instance(tmp_path)
    first_path = tmp_path / "first.md"
    first_path.write_text("# One\n\nFirst claim.\n")
    first = service_register_source_artifact(instance, source_path=str(first_path))
    second_path = tmp_path / "second.md"
    second_path.write_text("# Two\n\nSecond claim.\n")
    second = service_register_source_artifact(instance, source_path=str(second_path))

    with pytest.raises(ConfigError, match="does not belong to"):
        service_dereference_source_evidence(
            instance,
            source_artifact_id=first.source_artifact_id,
            artifact_revision_id=second.artifact_revision_id,
            heading_path=["One"],
            block_selector="paragraph:1",
        )


def test_an_unknown_revision_pin_is_refused(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    source_path = tmp_path / "evidence.md"
    source_path.write_text("# Fitment\n\nOriginal BP-1001 claim.\n")
    first = service_register_source_artifact(instance, source_path=str(source_path))

    with pytest.raises(ConfigError, match="revision .* not found"):
        service_dereference_source_evidence(
            instance,
            source_artifact_id=first.source_artifact_id,
            artifact_revision_id=f"{first.source_artifact_id}@99",
            heading_path=["Fitment"],
            block_selector="paragraph:1",
        )
