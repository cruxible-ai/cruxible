"""Compact pages remain portable, bounded, and excluded from independent evidence."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cruxible_client import Playbill
from cruxible_client.authoring.projection_package import ProjectionPackage
from cruxible_client.contracts.declared_blocks import (
    ProjectionMarkerError,
    frame_projection_block,
    parse_projection_blocks,
    projection_manifest,
    stamped_projection_windows,
)
from cruxible_core.coverage.adapter import WorkingSourceObservationV1, observed_commitment
from tests.test_client.test_playbill_block_sync import OLD_BODY, _stamp
from tests.test_client.test_playbill_projection_repin import NOW, _RepinClient, _workspace


def test_compact_package_roundtrip_and_refusals(tmp_path: Path) -> None:
    stamp = _stamp()
    digest, manifest = projection_manifest(stamp)
    page = frame_projection_block(stamp=stamp, body=OLD_BODY, compact=True)
    assert len(page.splitlines()[0]) < 130
    assert stamped_projection_windows(page)
    with pytest.raises(ProjectionMarkerError, match="unavailable"):
        parse_projection_blocks(page, source_id=stamp.source_id, allow_bootstrap=True)
    with pytest.raises(ProjectionMarkerError, match="digest"):
        parse_projection_blocks(page, source_id=stamp.source_id, manifests={digest: b"corrupt"})
    with pytest.raises(ProjectionMarkerError, match="source differs"):
        parse_projection_blocks(page, source_id="another.source", manifests={digest: manifest})
    package = ProjectionPackage(page, {digest: manifest})
    restored = ProjectionPackage.from_bytes(package.to_bytes())
    restored.install(tmp_path, "view.md")
    assert ProjectionPackage.read(tmp_path, "view.md").to_bytes() == package.to_bytes()
    assert package.retention_value().content == package.to_bytes()
    # Unavailable sidecars cannot turn the body into independently citable evidence.
    (tmp_path / ".playbill/manifests" / (digest[7:] + ".json")).unlink()
    with pytest.raises(ProjectionMarkerError, match="unavailable"):
        ProjectionPackage.read(tmp_path, "view.md")
    assert stamped_projection_windows((tmp_path / "view.md").read_bytes())
    restored.install(tmp_path, "view.md")
    assert ProjectionPackage.read(tmp_path, "view.md").content == page


def test_manifest_limits_and_server_observation() -> None:
    stamp = _stamp()
    digest, manifest = projection_manifest(stamp)
    page = frame_projection_block(stamp=stamp, body=OLD_BODY, compact=True)
    with pytest.raises(ProjectionMarkerError, match="ceiling"):
        parse_projection_blocks(
            page, source_id=stamp.source_id, manifests={str(i): b"x" for i in range(129)}
        )
    observation = WorkingSourceObservationV1(
        source={"plane": "external", "identity": stamp.source_id},
        content_base64=base64.b64encode(page).decode(),
        content_digest=observed_commitment(page),
        byte_length=len(page),
        projection_manifests={digest: base64.b64encode(manifest).decode()},
    )
    assert (
        parse_projection_blocks(
            observation.content, source_id=stamp.source_id, manifests=observation.manifest_bytes
        )[0].stamp
        == stamp
    )
    with pytest.raises(ValueError):
        WorkingSourceObservationV1.model_validate(
            {**observation.model_dump(), "projection_manifests": {digest: "eA=="}}
        )


def test_sdk_body_refresh_compact_package_and_failed_cas(tmp_path: Path) -> None:
    path = _workspace(tmp_path)
    client = _RepinClient()
    pb = Playbill._from_client(
        client, instance_id="inst_projection", workspace=tmp_path, clock=lambda: NOW
    )
    first = pb.block.repin(
        "corpus.runbook",
        "summary",
        claims=("CLM-first",),
        body="A complete agent-authored view.\n",
        compact=True,
        evaluation_time=NOW,
    )
    package = pb.block.package("corpus.runbook")
    assert package.content.startswith(b"prefix\n") and package.content.endswith(b"suffix\n")
    assert b":ref:sha256:" in package.content
    second = pb.block.repin(
        "corpus.runbook", "summary", body="Updated view.\n", evaluation_time=NOW
    )
    assert second.backing == first.backing
    assert b"Updated view." in pb.block.package("corpus.runbook").content
    before = path.read_bytes()

    def edit():
        path.write_bytes(before + b"concurrent edit\n")

    client.on_claim = edit
    with pytest.raises(Exception, match="changed|concurrent"):
        pb.block.repin("corpus.runbook", "summary", body="Must not land.\n", evaluation_time=NOW)
    assert path.read_bytes() == before + b"concurrent edit\n"


def test_compact_sync_and_coverage_preserve_manifest_binding(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from cruxible_client.authoring.blocks import repin_projection_block, sync_projection_blocks
    from cruxible_client.authoring.workspace import observe_playbill_projection_coverage
    from tests.test_client.test_playbill_block_sync import (
        INSTANCE_ID,
        NEW_COORDINATE,
        _SyncClient,
    )
    from tests.test_client.test_playbill_block_sync import (
        _workspace as sync_workspace,
    )

    path = sync_workspace(tmp_path)
    client = _SyncClient(status="current")
    repin_projection_block(
        client,
        INSTANCE_ID,
        workspace=tmp_path,
        source_id="corpus.runbook",
        block_id="pub-example",
        backing_digest="sha256:" + "b" * 64,
        compact=True,
        evaluation_time=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    before = path.read_bytes()
    result = sync_projection_blocks(
        client, INSTANCE_ID, workspace=tmp_path, paths=(path,), check=True
    )
    assert result.items[0].outcome == "unchanged"
    assert path.read_bytes() == before
    coverage = observe_playbill_projection_coverage(
        tmp_path, coordinate=NEW_COORDINATE.model_dump()
    )
    assert coverage is not None and "Claim" in coverage["complete_kinds"]
    assert len(coverage["bindings"]) == 1
    package = ProjectionPackage.read(tmp_path, path)
    key = next(iter(package.manifests))
    (tmp_path / ".playbill/manifests" / (key[7:] + ".json")).unlink()
    missing = sync_projection_blocks(
        client, INSTANCE_ID, workspace=tmp_path, paths=(path,), check=True
    )
    assert missing.items[0].outcome == "refused"
    coverage = observe_playbill_projection_coverage(
        tmp_path, coordinate=NEW_COORDINATE.model_dump()
    )
    assert coverage is not None and "Claim" not in coverage["complete_kinds"]


def test_package_refuses_noncanonical_archive_and_manifest_path_escape(tmp_path: Path) -> None:
    import json

    from cruxible_client.authoring.projection_package import retain_local_manifests
    from cruxible_client.contracts.canonical import canonical_bytes

    stamp = _stamp()
    digest, manifest = projection_manifest(stamp)
    page = frame_projection_block(stamp=stamp, body=OLD_BODY, compact=True)
    package = ProjectionPackage(page, {digest: manifest})
    archive = json.loads(package.to_bytes())
    archive["content_base64"] += "===="
    with pytest.raises(ProjectionMarkerError):
        ProjectionPackage.from_bytes(canonical_bytes(archive))
    with pytest.raises(ProjectionMarkerError, match="block differs"):
        parse_projection_blocks(
            page.replace(b"pub-example", b"another-block"),
            source_id=stamp.source_id,
            manifests={digest: manifest},
        )
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".playbill").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectionMarkerError, match="escapes"):
        retain_local_manifests(workspace, {digest: manifest})
    assert not (outside / "manifests").exists()


def test_compact_observation_allows_adjacent_unstamped_draft() -> None:
    stamp = _stamp()
    digest, manifest = projection_manifest(stamp)
    page = frame_projection_block(stamp=stamp, body=OLD_BODY, compact=True)
    page += b"<!-- playbill:block:draft -->\nUnfinished prose.\n<!-- /playbill:block:draft -->\n"
    observation = WorkingSourceObservationV1(
        source={"plane": "external", "identity": stamp.source_id},
        content_base64=base64.b64encode(page).decode(),
        content_digest=observed_commitment(page),
        byte_length=len(page),
        projection_manifests={digest: base64.b64encode(manifest).decode()},
    )
    blocks = parse_projection_blocks(
        observation.content,
        source_id=stamp.source_id,
        manifests=observation.manifest_bytes,
        allow_bootstrap=True,
    )
    assert blocks[0].stamp == stamp
    assert blocks[1].stamp is None
