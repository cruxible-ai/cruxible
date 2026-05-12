"""Published world release, overlay, status, and pull service functions."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from cruxible_core.config.composer import (
    compose_runtime_config_files,
    write_runtime_composed_config,
)
from cruxible_core.errors import ConfigError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.kits import materialize_kit
from cruxible_core.kits.world_refs import resolve_world_source
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service.execution import service_lock
from cruxible_core.service.snapshots import service_create_snapshot
from cruxible_core.service.types import (
    WorldOverlayResult,
    WorldPublishResult,
    WorldPullApplyResult,
    WorldPullPreviewResult,
    WorldStatusResult,
)
from cruxible_core.snapshot.types import (
    PublishedWorldManifest,
    UpstreamMetadata,
    WorldCompatibility,
)
from cruxible_core.transport.backends import resolve_transport
from cruxible_core.transport.types import PulledReleaseBundle


def service_publish_world(
    instance: InstanceProtocol,
    *,
    transport_ref: str,
    world_id: str,
    release_id: str,
    compatibility: WorldCompatibility,
) -> WorldPublishResult:
    """Publish a root world-model instance as an immutable release bundle."""
    if instance.get_upstream_metadata() is not None:
        raise ConfigError("Only root instances can publish world releases in v1")
    if instance.load_config().kind != "world_model":
        raise ConfigError("Only kind: world_model instances can publish world releases")

    snapshot = service_create_snapshot(instance, label=release_id).snapshot
    bundle_dir = build_release_bundle(
        instance=instance,
        snapshot_id=snapshot.snapshot_id,
        world_id=world_id,
        release_id=release_id,
        compatibility=compatibility,
        parent_release_id=None,
    )
    transport, resolved_ref = resolve_transport(transport_ref)
    transport.publish(resolved_ref, bundle_dir)
    manifest = PublishedWorldManifest.model_validate_json(
        (bundle_dir / "manifest.json").read_text()
    )
    return WorldPublishResult(manifest=manifest)


def service_create_world_overlay(
    *,
    transport_ref: str | None = None,
    world_ref: str | None = None,
    kit: str | None = None,
    no_kit: bool = False,
    root_dir: str | Path,
    instance_mode: str = CruxibleInstance.DEV_MODE,
) -> WorldOverlayResult:
    """Create a new local overlay instance from a published world release."""
    root = Path(root_dir)
    if (root / CruxibleInstance.INSTANCE_DIR / "instance.json").exists():
        raise ConfigError(f"Instance already exists at {root}")

    resolved = resolve_world_source(transport_ref=transport_ref, world_ref=world_ref)
    pulled = _pull_bundle(resolved.pull_transport_ref)
    upstream_dir = _materialize_upstream_bundle(root, pulled.root_dir, pulled.manifest.release_id)

    normalized_kit = (kit or "").strip() or None
    if normalized_kit is not None and no_kit:
        raise ConfigError("Provide kit or no_kit, not both")
    selected_kit = None if no_kit else (normalized_kit or resolved.default_kit)

    overlay_path = (
        materialize_kit(
            kit=selected_kit,
            root=root,
            expected_role="overlay",
            target_world=pulled.manifest.world_id,
            upstream_config_path=".cruxible/upstream/current/config.yaml",
        )
        if selected_kit is not None
        else _write_default_overlay_config(root, pulled.manifest.world_id, upstream_dir)
    )
    composed_path = root / ".cruxible" / "composed" / "config.yaml"
    write_runtime_composed_config(
        base_path=upstream_dir / "config.yaml",
        overlay_path=overlay_path,
        output_path=composed_path,
    )

    instance = CruxibleInstance.init(
        root,
        ".cruxible/composed/config.yaml",
        instance_mode=instance_mode,
    )
    instance.save_graph(_load_graph_from_bundle(upstream_dir))
    upstream = UpstreamMetadata(
        transport_ref=resolved.tracking_transport_ref,
        requested_source_ref=resolved.source_ref,
        requested_transport_ref=resolved.pull_transport_ref,
        world_id=pulled.manifest.world_id,
        release_id=pulled.manifest.release_id,
        snapshot_id=pulled.manifest.snapshot_id,
        compatibility=pulled.manifest.compatibility,
        owned_entity_types=pulled.manifest.owned_entity_types,
        owned_relationship_types=pulled.manifest.owned_relationship_types,
        overlay_config_path="config.yaml",
        manifest_path=str((upstream_dir / "manifest.json").relative_to(root)),
        graph_path=str((upstream_dir / "graph.json").relative_to(root)),
        upstream_config_path=str((upstream_dir / "config.yaml").relative_to(root)),
        lock_path=str((upstream_dir / "cruxible.lock.yaml").relative_to(root)),
        manifest_digest=_sha256_file(upstream_dir / "manifest.json"),
        graph_digest=_sha256_file(upstream_dir / "graph.json"),
    )
    instance.set_upstream_metadata(upstream)
    service_lock(instance)
    return WorldOverlayResult(instance=instance, manifest=pulled.manifest)


def service_world_status(instance: InstanceProtocol) -> WorldStatusResult:
    """Return upstream tracking metadata for a release-backed overlay, if any."""
    return WorldStatusResult(upstream=instance.get_upstream_metadata())


def service_pull_world_preview(instance: InstanceProtocol) -> WorldPullPreviewResult:
    """Preview an upstream pull for a release-backed overlay instance."""
    upstream = instance.get_upstream_metadata()
    if upstream is None:
        raise ConfigError("Instance is not tracking an upstream world release")

    pulled = _pull_bundle(upstream.transport_ref)
    return _build_world_pull_preview(instance, upstream=upstream, pulled=pulled)


def _build_world_pull_preview(
    instance: InstanceProtocol,
    *,
    upstream: UpstreamMetadata,
    pulled: PulledReleaseBundle,
) -> WorldPullPreviewResult:
    """Evaluate a materialized upstream bundle against the current overlay."""
    warnings: list[str] = []
    conflicts: list[str] = []
    if pulled.manifest.release_id == upstream.release_id:
        warnings.append("Already at latest pulled release")
    if pulled.manifest.compatibility == "breaking":
        conflicts.append("Target release is marked breaking and cannot be pulled in v1")

    root = instance.get_root_path()
    try:
        compose_runtime_config_files(
            base_path=pulled.root_dir / "config.yaml",
            overlay_path=root / upstream.overlay_config_path,
        )
    except Exception as exc:
        conflicts.append(f"Overlay config does not compose cleanly with target release: {exc}")

    current_upstream_graph = _load_graph_from_bundle(root / ".cruxible" / "upstream" / "current")
    next_graph = _load_graph_from_bundle(pulled.root_dir)
    local_graph = _extract_local_overlay_graph(instance.load_graph(), upstream)
    conflicts.extend(_find_dangling_reference_conflicts(local_graph, next_graph, pulled.manifest))
    apply_digest = _compute_world_apply_digest(
        current_release_id=upstream.release_id,
        target_release_id=pulled.manifest.release_id,
        current_graph_digest=upstream.graph_digest or "",
        next_graph_digest=_sha256_file(pulled.root_dir / "graph.json"),
    )
    return WorldPullPreviewResult(
        current_release_id=upstream.release_id,
        target_release_id=pulled.manifest.release_id,
        compatibility=pulled.manifest.compatibility,
        apply_digest=apply_digest,
        warnings=warnings,
        conflicts=conflicts,
        lock_changed=_lock_text(root / upstream.lock_path)
        != _lock_text(pulled.root_dir / "cruxible.lock.yaml"),
        upstream_entity_delta=next_graph.entity_count() - current_upstream_graph.entity_count(),
        upstream_edge_delta=next_graph.edge_count() - current_upstream_graph.edge_count(),
    )


def service_pull_world_apply(
    instance: InstanceProtocol,
    *,
    expected_apply_digest: str,
) -> WorldPullApplyResult:
    """Apply a previewed upstream pull to a release-backed overlay instance."""
    upstream = instance.get_upstream_metadata()
    if upstream is None:
        raise ConfigError("Instance is not tracking an upstream world release")

    pulled = _pull_bundle(upstream.transport_ref)
    preview = _build_world_pull_preview(instance, upstream=upstream, pulled=pulled)
    if preview.apply_digest != expected_apply_digest:
        raise ConfigError("World pull apply digest mismatch; rerun pull preview before apply")
    if preview.conflicts:
        raise ConfigError("World pull preview has blocking conflicts", errors=preview.conflicts)

    pre_pull_snapshot_id = service_create_snapshot(
        instance,
        label=f"pre-pull-{preview.target_release_id}",
    ).snapshot.snapshot_id

    root = instance.get_root_path()
    upstream_dir = _materialize_upstream_bundle(root, pulled.root_dir, pulled.manifest.release_id)
    write_runtime_composed_config(
        base_path=upstream_dir / "config.yaml",
        overlay_path=root / upstream.overlay_config_path,
        output_path=instance.get_config_path(),
    )

    current_graph = instance.load_graph()
    local_graph = _extract_local_overlay_graph(current_graph, upstream)
    next_upstream_graph = _load_graph_from_bundle(upstream_dir)
    conflicts = _find_dangling_reference_conflicts(
        local_graph,
        next_upstream_graph,
        pulled.manifest,
    )
    if conflicts:
        raise ConfigError("Local overlay references entities removed upstream", errors=conflicts)
    merged = EntityGraph.merge_graphs(next_upstream_graph, local_graph)
    instance.save_graph(merged)

    updated = UpstreamMetadata(
        transport_ref=upstream.transport_ref,
        requested_source_ref=upstream.requested_source_ref,
        requested_transport_ref=upstream.requested_transport_ref,
        world_id=pulled.manifest.world_id,
        release_id=pulled.manifest.release_id,
        snapshot_id=pulled.manifest.snapshot_id,
        compatibility=pulled.manifest.compatibility,
        owned_entity_types=pulled.manifest.owned_entity_types,
        owned_relationship_types=pulled.manifest.owned_relationship_types,
        overlay_config_path=upstream.overlay_config_path,
        manifest_path=str((upstream_dir / "manifest.json").relative_to(root)),
        graph_path=str((upstream_dir / "graph.json").relative_to(root)),
        upstream_config_path=str((upstream_dir / "config.yaml").relative_to(root)),
        lock_path=str((upstream_dir / "cruxible.lock.yaml").relative_to(root)),
        manifest_digest=_sha256_file(upstream_dir / "manifest.json"),
        graph_digest=_sha256_file(upstream_dir / "graph.json"),
    )
    instance.set_upstream_metadata(updated)
    service_lock(instance)
    return WorldPullApplyResult(
        release_id=updated.release_id,
        apply_digest=preview.apply_digest,
        pre_pull_snapshot_id=pre_pull_snapshot_id,
    )


def _pull_bundle(transport_ref: str) -> PulledReleaseBundle:
    transport, resolved_ref = resolve_transport(transport_ref)
    temp_root = Path(tempfile.mkdtemp(prefix="cruxible_release_"))
    return transport.pull(resolved_ref, temp_root)


def build_release_bundle(
    *,
    instance: InstanceProtocol,
    snapshot_id: str,
    world_id: str,
    release_id: str,
    compatibility: WorldCompatibility,
    parent_release_id: str | None,
) -> Path:
    snapshot = instance.get_snapshot(snapshot_id)
    if snapshot is None:
        raise ConfigError(f"Snapshot '{snapshot_id}' not found")
    snapshot_dir = instance.get_instance_dir() / "snapshots" / snapshot_id
    bundle_dir = Path(tempfile.mkdtemp(prefix="cruxible_bundle_"))
    for name in ("snapshot.json", "config.yaml", "graph.json", "cruxible.lock.yaml"):
        source = snapshot_dir / name
        if source.exists():
            shutil.copy2(source, bundle_dir / name)
    config = instance.load_config()
    manifest = PublishedWorldManifest(
        world_id=world_id,
        release_id=release_id,
        snapshot_id=snapshot_id,
        compatibility=compatibility,
        owned_entity_types=sorted(config.entity_types.keys()),
        owned_relationship_types=sorted(rel.name for rel in config.relationships),
        parent_release_id=parent_release_id,
    )
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True)
    )
    return bundle_dir


def _materialize_upstream_bundle(root: Path, bundle_dir: Path, release_id: str) -> Path:
    releases_dir = root / ".cruxible" / "upstream" / "releases" / release_id
    current_dir = root / ".cruxible" / "upstream" / "current"
    shutil.copytree(bundle_dir, releases_dir, dirs_exist_ok=True)
    shutil.rmtree(current_dir, ignore_errors=True)
    shutil.copytree(releases_dir, current_dir)
    return current_dir


def _write_default_overlay_config(root: Path, world_id: str, upstream_dir: Path) -> Path:
    overlay_path = root / "config.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(
        "\n".join(
            [
                "version: '1.0'",
                f"name: {world_id}-overlay",
                f"extends: {str((upstream_dir / 'config.yaml').relative_to(root))}",
                "entity_types: {}",
                "relationships: []",
            ]
        )
        + "\n"
    )
    return overlay_path


def _load_graph_from_bundle(bundle_dir: Path) -> EntityGraph:
    return EntityGraph.from_dict(json.loads((bundle_dir / "graph.json").read_text()))


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _lock_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text()


def _compute_world_apply_digest(
    *,
    current_release_id: str | None,
    target_release_id: str,
    current_graph_digest: str,
    next_graph_digest: str | None,
) -> str:
    payload = {
        "current_release_id": current_release_id,
        "target_release_id": target_release_id,
        "current_graph_digest": current_graph_digest,
        "next_graph_digest": next_graph_digest,
    }
    blob = json.dumps(payload, indent=2, sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


def _extract_local_overlay_graph(
    current_graph: EntityGraph,
    upstream: UpstreamMetadata,
) -> EntityGraph:
    local_entity_types = [
        entity_type
        for entity_type in current_graph.list_entity_types()
        if entity_type not in set(upstream.owned_entity_types)
    ]
    local_relationship_types = [
        relationship_type
        for relationship_type in current_graph.list_relationship_types()
        if relationship_type not in set(upstream.owned_relationship_types)
    ]
    return current_graph.extract_owned_subgraph(
        entity_types=local_entity_types,
        relationship_types=local_relationship_types,
    )


def _find_dangling_reference_conflicts(
    local_graph: EntityGraph,
    next_upstream_graph: EntityGraph,
    manifest: PublishedWorldManifest,
) -> list[str]:
    upstream_entity_types = set(manifest.owned_entity_types)
    conflicts: list[str] = []
    for edge in local_graph.iter_edges():
        if edge["from_type"] in upstream_entity_types and not next_upstream_graph.has_entity(
            edge["from_type"], edge["from_id"]
        ):
            conflicts.append(
                "Local relationship "
                f"{edge['relationship_type']} references missing upstream entity "
                f"{edge['from_type']}:{edge['from_id']}"
            )
        if edge["to_type"] in upstream_entity_types and not next_upstream_graph.has_entity(
            edge["to_type"], edge["to_id"]
        ):
            conflicts.append(
                "Local relationship "
                f"{edge['relationship_type']} references missing upstream entity "
                f"{edge['to_type']}:{edge['to_id']}"
            )
    return sorted(set(conflicts))
