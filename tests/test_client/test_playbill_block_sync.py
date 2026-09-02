"""Client-owned block sync preserves local authority and exact file boundaries."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from cruxible_client.authoring.blocks import sync_projection_blocks
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadRequestV1,
    PlaybillBlockSyncReadResultV1,
    PlaybillBlockSyncSuccessorCandidateV1,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    frame_projection_block,
    parse_projection_blocks,
)
from cruxible_client.contracts.projection import AcceptedCoordinate

INSTANCE_ID = "inst_block_sync"
OLD_BODY = b"status: old\n"
NEW_BODY = b"status: current\n"
OLD_COORDINATE = AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
NEW_COORDINATE = AcceptedCoordinate(
    git_oid="5" * 64,
    semantic_root="sha256:" + "6" * 64,
    generation_root="sha256:" + "7" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _stamp(*, body: bytes = OLD_BODY) -> ProjectionBlockStampV1:
    return ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="pub-example",
        declared_generation=1,
        declared_coordinate=OLD_COORDINATE,
        backing=(
            ProjectionClaimBackingV1(
                identity=ArtifactIdentity(kind="Claim", name="CLM-" + "a" * 32),
                statement_digest="sha256:" + "8" * 64,
            ),
        ),
        body_digest=_digest(body),
    )


def _workspace(root: Path, *, instance_id: str = INSTANCE_ID) -> Path:
    playbill = root / ".playbill"
    playbill.mkdir()
    (playbill / "coverage.json").write_text(
        "{"
        '"tag":"playbill-coverage-workspace-config-v2",'
        '"server_url":"https://sync.example.test",'
        f'"instance_id":"{instance_id}"'
        "}",
        encoding="utf-8",
    )
    (playbill / "sources.yaml").write_text(
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
    governance_scope: [Document:runbook]
""",
        encoding="utf-8",
    )
    source = root / "corpus" / "runbook.md"
    source.parent.mkdir()
    source.write_bytes(
        b"PREFIX\n" + frame_projection_block(stamp=_stamp(), body=OLD_BODY) + b"SUFFIX\n"
    )
    return source


class _SyncClient:
    def __init__(self, *, refusal: str | None = None) -> None:
        self.requests: list[PlaybillBlockSyncReadRequestV1] = []
        self.refusal = refusal

    def read_playbill_block_sync_backing(
        self,
        instance_id: str,
        *,
        request: PlaybillBlockSyncReadRequestV1,
    ) -> PlaybillBlockSyncReadResultV1:
        assert instance_id == INSTANCE_ID
        self.requests.append(request)
        if self.refusal is not None:
            candidates = (
                (
                    PlaybillBlockSyncSuccessorCandidateV1(
                        identity=request.stamp.backing[0].identity,
                        artifact_digest="sha256:" + "a" * 64,
                        coordinate=NEW_COORDINATE,
                        generation=2,
                    ),
                    PlaybillBlockSyncSuccessorCandidateV1(
                        identity=request.stamp.backing[0].identity,
                        artifact_digest="sha256:" + "b" * 64,
                        coordinate=NEW_COORDINATE,
                        generation=3,
                    ),
                )
                if self.refusal == "block_successor_ambiguous"
                else ()
            )
            return PlaybillBlockSyncReadResultV1(
                status="refused",
                original_artifact_digest=_digest(b"old-artifact"),
                reason=self.refusal,  # type: ignore[arg-type]
                detail="ruled refusal",
                successor_candidates=candidates,
            )
        return PlaybillBlockSyncReadResultV1(
            status="successor",
            original_artifact_digest=_digest(b"old-artifact"),
            artifact_digest=_digest(b"new-artifact"),
            coordinate=NEW_COORDINATE,
            generation=2,
            backing=ProjectionClaimBackingV1(
                identity=request.stamp.backing[0].identity,
                statement_digest="sha256:" + "9" * 64,
            ),
            body_content_base64="c3RhdHVzOiBjdXJyZW50Cg==",
            body_digest=_digest(NEW_BODY),
        )


def test_sync_is_atomic_per_file_idempotent_and_preserves_outside_bytes_and_mode(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path)
    source.chmod(0o640)
    client = _SyncClient()

    first = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=("corpus/runbook.md",),
    )

    assert [item.outcome for item in first.items] == ["synced"]
    assert (
        first.items[0].detail["outside_digest_before"]
        == (first.items[0].detail["outside_digest_after"])
    )
    content = source.read_bytes()
    assert content.startswith(b"PREFIX\n") and content.endswith(b"SUFFIX\n")
    (block,) = parse_projection_blocks(content, source_id="corpus.runbook")
    assert content[block.body_start : block.body_end] == NEW_BODY
    assert block.stamp is not None and block.stamp.declared_coordinate == NEW_COORDINATE
    assert source.stat().st_mode & 0o777 == 0o640

    second = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=("corpus/runbook.md",),
    )
    assert [item.outcome for item in second.items] == ["unchanged"]
    assert source.read_bytes() == content


def test_check_reports_change_without_writing(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    before = source.read_bytes()

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
        check=True,
    )

    assert result.would_change is True
    assert result.changed_file_count == 0
    assert [item.outcome for item in result.items] == ["would_sync"]
    assert source.read_bytes() == before


@pytest.mark.parametrize("all_sources", [False, True])
def test_source_catalog_is_optional_for_stamped_marker_discovery(
    tmp_path: Path,
    all_sources: bool,
) -> None:
    source = _workspace(tmp_path)
    (tmp_path / ".playbill" / "sources.yaml").unlink()

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=() if all_sources else ("corpus/runbook.md",),
        all_sources=all_sources,
    )

    assert [item.outcome for item in result.items] == ["synced"]
    assert NEW_BODY in source.read_bytes()


def test_local_edit_survives_and_names_both_repairs_until_explicitly_discarded(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path)
    source.write_bytes(source.read_bytes().replace(OLD_BODY, b"status: hand edited\n"))
    client = _SyncClient()

    refused = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )

    assert client.requests == []
    assert refused.items[0].reason == "block_locally_modified"
    assert len(refused.items[0].repair_commands) == 2
    assert b"hand edited" in source.read_bytes()

    discarded = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
        discard_local_paths=(source,),
    )
    assert discarded.items[0].outcome == "synced"
    assert b"hand edited" not in source.read_bytes()


def test_retired_block_refuses_then_detaches_markers_without_changing_body(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    before = source.read_bytes()
    (block,) = parse_projection_blocks(before, source_id="corpus.runbook")
    body = before[block.body_start : block.body_end]
    client = _SyncClient(refusal="block_backing_retired")

    refused = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )
    assert refused.items[0].reason == "block_backing_retired"
    assert "--detach" in refused.items[0].repair_commands[0]

    detached = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        detach_paths=(source,),
    )
    assert detached.items[0].outcome == "detached"
    assert (
        detached.items[0].detail["outside_digest_before"]
        == (detached.items[0].detail["outside_digest_after"])
    )
    assert source.read_bytes() == b"PREFIX\n" + body + b"SUFFIX\n"


def test_ambiguous_live_successors_emit_exact_repin_selections(tmp_path: Path) -> None:
    source = _workspace(tmp_path)

    result = sync_projection_blocks(
        _SyncClient(refusal="block_successor_ambiguous"),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )

    assert result.items[0].reason == "block_successor_ambiguous"
    assert result.items[0].repair_commands == (
        "cruxible playbill block repin corpus.runbook pub-example --backing sha256:" + "a" * 64,
        "cruxible playbill block repin corpus.runbook pub-example --backing sha256:" + "b" * 64,
    )


def test_malformed_marker_and_foreign_workspace_are_typed_without_daemon_calls(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path, instance_id="inst_other")
    client = _SyncClient()
    mismatch = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )
    assert mismatch.items[0].reason == "workspace_instance_mismatch"
    assert client.requests == []

    coverage = tmp_path / ".playbill" / "coverage.json"
    coverage.write_text(
        coverage.read_text(encoding="utf-8").replace("inst_other", INSTANCE_ID),
        encoding="utf-8",
    )
    source.write_bytes(b"<!-- playbill:block:broken -->\nbody\n")
    malformed = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )
    assert malformed.items[0].reason == "block_marker_malformed"
    assert malformed.items[0].detail["target"] == "corpus/runbook.md:1"


def test_catalog_optional_explicit_path_cannot_escape_workspace(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    (tmp_path / ".playbill" / "sources.yaml").unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_bytes(source.read_bytes())

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(outside,),
    )

    assert result.items[0].reason == "source_path_invalid"
    assert result.items[0].path == outside.name


def test_whole_file_cas_preserves_a_concurrent_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _workspace(tmp_path)
    concurrent = source.read_bytes() + b"CONCURRENT\n"
    from cruxible_client.authoring import blocks as block_module

    original = block_module.replace_publication_file

    def race(path: Path, *, expected: bytes, replacement: bytes) -> None:
        path.write_bytes(concurrent)
        original(path, expected=expected, replacement=replacement)

    monkeypatch.setattr(block_module, "replace_publication_file", race)
    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )

    assert result.items[0].reason == "block_concurrent_edit"
    assert source.read_bytes() == concurrent
    assert os.path.isfile(source)
