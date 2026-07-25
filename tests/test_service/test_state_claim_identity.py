"""Claim identity across the release/overlay boundary.

The pull path is where identity used to be destroyed: ``edge_key`` is a per-load
counter, so pull-apply re-keyed local overlay edges (twice), keys could go DOWN,
and freed numbers were reused by DIFFERENT edges. These tests pin that a claim's
identity now survives the whole loop -- publish, overlay create, pull, re-pull --
and that the churn a legacy upstream would otherwise cause is bounded and
visible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.graph.legacy_identity import load_legacy_identity_map
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_add_relationships,
    service_create_state_overlay,
    service_publish_state,
    service_pull_state_apply,
    service_pull_state_preview,
    service_reload_config,
    service_state_status,
)
from cruxible_core.storage.sqlite import LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY
from tests.test_service.test_state import (  # noqa: F401 - fixture import
    STATE_MODEL_YAML,
    _case,
    _replace_release_dir,
    _write_overlay_config,
    published_release_fixture,
)


def _overlay_with_local_edge(release_dir: Path, overlay_root: Path) -> CruxibleInstance:
    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=overlay_root,
    ).instance
    _write_overlay_config(overlay_root)
    service_reload_config(overlay)
    service_add_relationships(
        overlay,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="follow_up",
                to_type="Case",
                to_id="CASE-B",
                properties={"reason": "watch"},
            )
        ],
        source="test",
        source_ref="model-test",
    )
    return overlay


def _publish_successor(
    root_instance: CruxibleInstance,
    release_dir: Path,
    tmp_path: Path,
    *,
    release_id: str,
) -> None:
    root_graph = root_instance.load_graph()
    root_graph.add_entity(_case(f"CASE-{release_id}", "Extra"))
    root_instance.save_graph(root_graph)
    successor_dir = tmp_path / "releases" / release_id
    service_publish_state(
        root_instance,
        transport_ref=f"file://{successor_dir}",
        state_id="case-law",
        release_id=release_id,
        compatibility="data_only",
    )
    _replace_release_dir(successor_dir, release_dir)


def _local_edge(instance: CruxibleInstance) -> RelationshipInstance:
    edge = instance.load_graph().get_relationship("Case", "CASE-A", "Case", "CASE-B", "follow_up")
    assert edge is not None
    return edge


def test_local_overlay_claim_id_survives_pull_apply(
    published_release_fixture: tuple[CruxibleInstance, Path],  # noqa: F811
    tmp_path: Path,
) -> None:
    """The core regression: pull-apply must not re-identify local edges.

    edge_key is free to move (extract + merge re-key it); ``claim_id`` is not.
    """
    root_instance, release_dir = published_release_fixture
    overlay = _overlay_with_local_edge(release_dir, tmp_path / "overlay")
    before = _local_edge(overlay)
    assert before.claim_id is not None

    _publish_successor(root_instance, release_dir, tmp_path, release_id="v1.1.0")
    preview = service_pull_state_preview(overlay)
    service_pull_state_apply(overlay, expected_apply_digest=preview.apply_digest)

    overlay.invalidate_graph_cache()
    after = _local_edge(overlay)
    assert after.claim_id == before.claim_id


def test_second_pull_still_preserves_the_local_claim_id(
    published_release_fixture: tuple[CruxibleInstance, Path],  # noqa: F811
    tmp_path: Path,
) -> None:
    """The double re-key scenario: two pulls, one stable identity."""
    root_instance, release_dir = published_release_fixture
    overlay = _overlay_with_local_edge(release_dir, tmp_path / "overlay")
    original = _local_edge(overlay).claim_id

    for release_id in ("v1.1.0", "v1.2.0"):
        _publish_successor(root_instance, release_dir, tmp_path, release_id=release_id)
        preview = service_pull_state_preview(overlay)
        service_pull_state_apply(overlay, expected_apply_digest=preview.apply_digest)
        overlay.invalidate_graph_cache()

    assert _local_edge(overlay).claim_id == original


def test_same_release_reapply_is_refused(
    published_release_fixture: tuple[CruxibleInstance, Path],  # noqa: F811
    tmp_path: Path,
) -> None:
    """A no-op re-apply churns state for no state change; refuse it."""
    root_instance, release_dir = published_release_fixture
    overlay = _overlay_with_local_edge(release_dir, tmp_path / "overlay")

    preview = service_pull_state_preview(overlay)
    with pytest.raises(ConfigError, match="already tracks release"):
        service_pull_state_apply(overlay, expected_apply_digest=preview.apply_digest)


def test_upstream_claim_ids_are_stable_and_the_map_digest_is_recorded(
    published_release_fixture: tuple[CruxibleInstance, Path],  # noqa: F811
    tmp_path: Path,
) -> None:
    """Upstream edges published WITH ids need no reconcile entries.

    The released bundle is post-identity here, so the legacy map stays empty and
    its digest is the empty-map digest -- proving the reconcile machinery only
    engages for genuinely pre-identity images.
    """
    root_instance, release_dir = published_release_fixture
    root_graph = root_instance.load_graph()
    service_add_relationships(
        root_instance,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="cites",
                to_type="Case",
                to_id="CASE-B",
            )
        ],
        source="test",
        source_ref="root",
    )
    del root_graph
    _publish_successor(root_instance, release_dir, tmp_path, release_id="v1.1.0")

    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=tmp_path / "overlay",
    ).instance
    upstream_edge = overlay.load_graph().get_relationship(
        "Case", "CASE-A", "Case", "CASE-B", "cites"
    )
    assert upstream_edge is not None
    assert upstream_edge.claim_id is not None

    status = service_state_status(overlay)
    assert status.upstream is not None
    assert status.upstream.identity_map_digest is not None
    assert status.upstream.identity_map_digest.startswith("sha256:")
    # Nothing needed minting, so nothing was recorded.
    assert (
        load_legacy_identity_map(overlay.get_instance_state(LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY))
        == {}
    )


def test_legacy_upstream_bundle_is_backfilled_without_rewriting_its_bytes(
    published_release_fixture: tuple[CruxibleInstance, Path],  # noqa: F811
    tmp_path: Path,
) -> None:
    """A pre-identity release: ids are minted locally, the bundle is untouched."""
    root_instance, release_dir = published_release_fixture
    service_add_relationships(
        root_instance,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="cites",
                to_type="Case",
                to_id="CASE-B",
            )
        ],
        source="test",
        source_ref="root",
    )
    _publish_successor(root_instance, release_dir, tmp_path, release_id="v1.1.0")

    # Strip the ids from the published graph, making it a LEGACY image. The
    # member digests are recomputed to match, exactly as a pre-identity
    # publisher would have written them.
    graph_path = release_dir / "graph.json"
    payload = json.loads(graph_path.read_text())
    for edge in payload["edges"]:
        edge.pop("claim_id", None)
    graph_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    _rewrite_member_digests(release_dir)
    legacy_bytes = graph_path.read_bytes()

    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=tmp_path / "overlay",
    ).instance

    edge = overlay.load_graph().get_relationship("Case", "CASE-A", "Case", "CASE-B", "cites")
    assert edge is not None
    assert edge.claim_id is not None

    # ARTIFACT BYTES ARE IMMUTABLE: the bundle we read is byte-identical after.
    assert graph_path.read_bytes() == legacy_bytes
    # And the mint was recorded so a later re-pull can reuse it.
    reconcile = load_legacy_identity_map(
        overlay.get_instance_state(LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY)
    )
    assert reconcile[("cites", "Case", "CASE-A", "Case", "CASE-B")] == edge.claim_id


def _rewrite_member_digests(release_dir: Path) -> None:
    """Recompute the release's member digest sidecar + manifest after an edit.

    Mirrors what a PRE-IDENTITY publisher would have written: every bundle
    member pinned by the sha256 of its exact bytes, the sidecar excluded (it
    cannot pin itself) and covered by the manifest's members_digest instead.
    """
    import hashlib

    from cruxible_core.service.state import (
        RELEASE_MEMBER_DIGESTS_FILE,
        _compute_members_digest,
        _write_release_member_digests,
    )

    def digest_of(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    # snapshot.json declares the graph digest independently of the sidecar, so
    # a pre-identity publisher's snapshot has to agree with its graph.
    snapshot_path = release_dir / "snapshot.json"
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["graph_digest"] = digest_of(release_dir / "graph.json")
        snapshot_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))

    manifest_path = release_dir / "manifest.json"
    members = {
        path.name: digest_of(path)
        for path in sorted(release_dir.iterdir())
        if path.is_file() and path.name not in {RELEASE_MEMBER_DIGESTS_FILE, "manifest.json"}
    }
    manifest = json.loads(manifest_path.read_text())
    manifest["members_digest"] = _compute_members_digest(members)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    _write_release_member_digests(
        release_dir,
        {**members, "manifest.json": digest_of(manifest_path)},
    )
