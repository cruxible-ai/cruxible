"""Published state release, overlay, status, and pull service functions."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from cruxible_core.config.composer import (
    compose_runtime_config_files,
    write_runtime_composed_config,
)
from cruxible_core.config.loader import save_config
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.legacy_identity import (
    backfill_legacy_graph,
    dump_legacy_identity_map,
    legacy_identity_map_digest,
    load_legacy_identity_map,
    record_minted_identities,
)
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.kits import (
    compute_bundle_digest,
    install_verified_kit_bundle,
    resolve_verified_kit_bundle,
)
from cruxible_core.kits.state_refs import resolve_state_source
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.server.auth_managed_entities import (
    materialize_local_operator_auth_managed_entities,
)
from cruxible_core.service.execution import service_lock
from cruxible_core.service.snapshots import service_create_snapshot
from cruxible_core.service.types import (
    StateOverlayResult,
    StatePublishResult,
    StatePullApplyResult,
    StatePullPreviewResult,
    StateStatusResult,
)
from cruxible_core.snapshot.types import (
    PublishedStateManifest,
    StateCompatibility,
    UpstreamMetadata,
)
from cruxible_core.snapshot.upstream_verification import sha256_file, verify_tracked_upstream
from cruxible_core.storage.sqlite import LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY
from cruxible_core.transport.backends import (
    RELEASE_BUNDLE_MEMBERS,
    RELEASE_MEMBER_DIGESTS_FILE,
    resolve_transport,
)
from cruxible_core.transport.types import PulledReleaseBundle

if TYPE_CHECKING:
    from cruxible_core.storage.protocols import UnitOfWorkProtocol

_logger = logging.getLogger(__name__)

MEMBER_DIGESTS_FORMAT_VERSION = 1
"""Format version of the per-member digest sidecar written into release bundles."""


def service_publish_state(
    instance: InstanceProtocol,
    *,
    transport_ref: str,
    state_id: str,
    release_id: str,
    compatibility: StateCompatibility,
) -> StatePublishResult:
    """Publish a root state instance as an immutable release bundle."""
    if instance.get_upstream_metadata() is not None:
        raise ConfigError("Only root instances can publish state releases in v1")

    snapshot = service_create_snapshot(instance, label=release_id).snapshot
    bundle_dir = build_release_bundle(
        instance=instance,
        snapshot_id=snapshot.snapshot_id,
        state_id=state_id,
        release_id=release_id,
        compatibility=compatibility,
        parent_release_id=None,
    )
    transport, resolved_ref = resolve_transport(transport_ref)
    transport.publish(resolved_ref, bundle_dir)
    manifest = PublishedStateManifest.model_validate_json(
        (bundle_dir / "manifest.json").read_text()
    )
    return StatePublishResult(manifest=manifest)


def service_create_state_overlay(
    *,
    transport_ref: str | None = None,
    state_ref: str | None = None,
    kit: str | None = None,
    no_kit: bool = False,
    root_dir: str | Path,
    instance_mode: str = CruxibleInstance.DEV_MODE,
) -> StateOverlayResult:
    """Create a new local overlay instance from a published state release."""
    root = Path(root_dir)
    if (root / CruxibleInstance.INSTANCE_DIR / "instance.json").exists():
        raise ConfigError(f"Instance already exists at {root}")

    normalized_kit = (kit or "").strip() or None
    if normalized_kit is not None and no_kit:
        raise ConfigError("Provide kit or no_kit, not both")

    resolved = resolve_state_source(transport_ref=transport_ref, state_ref=state_ref)
    pulled, bundle_warnings = _pull_bundle(resolved.pull_transport_ref)
    for warning in bundle_warnings:
        _logger.warning("%s", warning)

    selected_kit = None if no_kit else (normalized_kit or resolved.default_kit)
    # Verify BEFORE writing: the release bundle's members are already verified
    # above, and resolving the kit here runs its oci pin check, its cache
    # integrity check, and its bundled-lock digest check while the target root
    # is still untouched. A refusal from any of them leaves nothing behind to
    # clean up -- no materialized upstream, no half-installed kit, no config.
    verified_kit = (
        resolve_verified_kit_bundle(
            kit=selected_kit,
            expected_role="overlay",
            target_state=pulled.manifest.state_id,
        )
        if selected_kit is not None
        else None
    )

    composed_path = root / ".cruxible" / "composed" / "config.yaml"
    upstream_dir = _materialize_upstream_bundle(root, pulled.root_dir, pulled.manifest.release_id)

    overlay_path = (
        install_verified_kit_bundle(
            verified_kit,
            root=root,
            upstream_config_path=".cruxible/upstream/current/config.yaml",
        )
        if verified_kit is not None
        else _write_default_overlay_config(root, pulled.manifest.state_id, upstream_dir)
    )
    composed = compose_runtime_config_files(
        base_path=upstream_dir / "config.yaml",
        overlay_path=overlay_path,
    )
    composed_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(composed, composed_path)

    instance = CruxibleInstance.init(
        root,
        ".cruxible/composed/config.yaml",
        instance_mode=instance_mode,
    )
    upstream_graph = _load_graph_from_bundle(upstream_dir)
    # The upstream bundle is graph+config+lock with NO receipts: any receipt_id
    # an upstream edge carries points at a receipt in the publishing instance
    # that is absent in this fresh overlay. Clear those dangling pointers and
    # stamp clone origin before the initial save so no edge in the new overlay
    # references a phantom receipt -- the same invariant the clone-from-snapshot
    # and state-pull-apply paths enforce.
    upstream_graph.relabel_clone_receipts()
    # LEGACY-IMAGE BACKFILL (overlay create). A release published before edge
    # identity has no claim ids in its graph.json, and that bundle is immutable
    # forever. Mint them in memory; the bundle bytes on disk are untouched, so
    # the members digest and release immutability still verify.
    minted = backfill_legacy_graph(upstream_graph)
    identity_map = record_minted_identities({}, minted)
    # ONE commit boundary for the backfilled graph AND the map that explains it.
    #
    # ``save_graph`` opens its own transaction when there is none, so persisting
    # the graph first and the map afterwards left a window where a crash between
    # them stranded minted ids with no reconcile map -- and the next pull of a
    # still-pre-identity upstream would then re-mint every one of them, staling
    # each record-time stamp while every content digest stayed identical. The
    # storage module states this invariant where the state key is declared: the
    # map must be written in the SAME transaction as the backfill it describes.
    # ``write_transaction`` is re-entrant, so ``save_graph`` joins THIS boundary.
    with instance.write_transaction() as uow:
        instance.save_graph(upstream_graph)
        if identity_map:
            uow.snapshots.set_instance_state(
                LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY,
                dump_legacy_identity_map(identity_map),
            )
        materialize_local_operator_auth_managed_entities(instance)
    upstream = UpstreamMetadata(
        transport_ref=resolved.tracking_transport_ref,
        requested_source_ref=resolved.source_ref,
        requested_transport_ref=resolved.pull_transport_ref,
        state_id=pulled.manifest.state_id,
        release_id=pulled.manifest.release_id,
        snapshot_id=pulled.manifest.snapshot_id,
        compatibility=pulled.manifest.compatibility,
        owned_entity_types=pulled.manifest.owned_entity_types,
        owned_relationship_types=pulled.manifest.owned_relationship_types,
        bundle_format_version=pulled.manifest.bundle_format_version,
        members_digest=pulled.manifest.members_digest,
        identity_map_digest=legacy_identity_map_digest(identity_map),
        overlay_config_path="config.yaml",
        manifest_path=str((upstream_dir / "manifest.json").relative_to(root)),
        graph_path=str((upstream_dir / "graph.json").relative_to(root)),
        upstream_config_path=str((upstream_dir / "config.yaml").relative_to(root)),
        lock_path=str((upstream_dir / "cruxible.lock.yaml").relative_to(root)),
        **_materialized_upstream_digests(upstream_dir),
    )
    instance.set_upstream_metadata(upstream)
    service_lock(instance)
    return StateOverlayResult(
        instance=instance,
        manifest=pulled.manifest,
        warnings=bundle_warnings,
    )


def service_state_status(instance: InstanceProtocol) -> StateStatusResult:
    """Return upstream tracking metadata for a release-backed overlay, if any."""
    return StateStatusResult(upstream=instance.get_upstream_metadata())


def service_pull_state_preview(
    instance: InstanceProtocol,
    *,
    force_repair: bool = False,
) -> StatePullPreviewResult:
    """Preview an upstream pull for a release-backed overlay instance.

    ``force_repair`` previews the REPAIR of a damaged materialized upstream: the
    digest verification of the local copy is what fails in that state, and it is
    also the gate this preview must pass to hand back an apply digest, so a
    repair could otherwise never be previewed and therefore never applied.
    """
    upstream = instance.get_upstream_metadata()
    if upstream is None:
        raise ConfigError("Instance is not tracking an upstream state release")

    pulled, bundle_warnings = _pull_bundle(upstream.transport_ref)
    return _build_state_pull_preview(
        instance,
        upstream=upstream,
        pulled=pulled,
        bundle_warnings=bundle_warnings,
        force_repair=force_repair,
    )


def _build_state_pull_preview(
    instance: InstanceProtocol,
    *,
    upstream: UpstreamMetadata,
    pulled: PulledReleaseBundle,
    bundle_warnings: list[str],
    force_repair: bool = False,
) -> StatePullPreviewResult:
    """Evaluate a materialized upstream bundle against the current overlay."""
    root = instance.get_root_path()
    if not force_repair:
        verify_tracked_upstream(root, upstream)
    _verify_release_immutability(root, pulled.root_dir, pulled.manifest.release_id)
    warnings: list[str] = list(bundle_warnings)
    conflicts: list[str] = []
    if pulled.manifest.release_id == upstream.release_id:
        warnings.append("Already at latest pulled release")
    if pulled.manifest.compatibility == "breaking":
        conflicts.append("Target release is marked breaking and cannot be pulled in v1")

    try:
        # The target release's content is still in the pulled temp dir, but the
        # overlay's config extends the materialized upstream path — compose the
        # pulled content under that identity, exactly as it will sit post-apply.
        compose_runtime_config_files(
            base_path=pulled.root_dir / "config.yaml",
            overlay_path=root / upstream.overlay_config_path,
            base_identity_path=root / upstream.upstream_config_path,
        )
    except Exception as exc:
        conflicts.append(f"Overlay config does not compose cleanly with target release: {exc}")

    try:
        current_upstream_graph = _load_graph_from_bundle(
            root / ".cruxible" / "upstream" / "current"
        )
    except Exception as exc:
        if not force_repair:
            raise
        # Repairing an upstream whose graph.json is the damaged member: the
        # deltas below are reported against a missing baseline, which is
        # information, not a reason to block the repair.
        warnings.append(f"Materialized upstream graph is unreadable and will be repaired: {exc}")
        current_upstream_graph = EntityGraph()
    next_graph = _load_graph_from_bundle(pulled.root_dir)
    local_graph = _extract_local_overlay_graph(instance.load_graph(), pulled.manifest)
    conflicts.extend(_find_dangling_reference_conflicts(local_graph, next_graph, pulled.manifest))
    apply_digest = _compute_state_apply_digest(
        current_release_id=upstream.release_id,
        target_release_id=pulled.manifest.release_id,
        current_graph_digest=upstream.graph_digest or "",
        next_graph_digest=sha256_file(pulled.root_dir / "graph.json"),
    )
    return StatePullPreviewResult(
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


def service_pull_state_apply(
    instance: InstanceProtocol,
    *,
    expected_apply_digest: str,
    actor_context: GovernedActorContext | None = None,
    force_repair: bool = False,
) -> StatePullApplyResult:
    """Apply a previewed upstream pull to a release-backed overlay instance.

    ``force_repair`` permits re-applying the release ALREADY tracked, which is
    otherwise refused as a no-op. It exists because re-pulling the current
    release is the documented repair for a materialized upstream that was
    damaged locally (``snapshot.upstream_verification`` sends operators here by
    name), and a blanket no-op refusal removed the only way out of that state.
    Repair still goes through the legacy reconcile map, so ids stay stable: a
    repair restores bytes, it does not re-identify claims.
    """
    upstream = instance.get_upstream_metadata()
    if upstream is None:
        raise ConfigError("Instance is not tracking an upstream state release")

    pulled, bundle_warnings = _pull_bundle(upstream.transport_ref)
    preview = _build_state_pull_preview(
        instance,
        upstream=upstream,
        pulled=pulled,
        bundle_warnings=bundle_warnings,
        force_repair=force_repair,
    )
    if preview.apply_digest != expected_apply_digest:
        raise ConfigError("State pull apply digest mismatch; rerun pull preview before apply")
    if preview.conflicts:
        raise ConfigError("State pull preview has blocking conflicts", errors=preview.conflicts)
    if pulled.manifest.release_id == upstream.release_id and not force_repair:
        # A no-op re-apply of the release already tracked. It was already
        # warning-worthy; with claim identity it is worse than useless -- it
        # re-materializes the same immutable bundle and, for a pre-identity
        # upstream, re-runs the legacy backfill for no state change at all.
        # Refuse rather than churn -- but NAME the escape, because the same
        # operation is the documented repair for a locally damaged materialized
        # upstream, and a refusal with no way through would strand that
        # instance with no read path and no supported fix.
        raise ConfigError(
            f"Instance already tracks release '{upstream.release_id}'; "
            "re-applying the same release is a no-op and is refused. To REPAIR a "
            "damaged materialized upstream, re-run with repair enabled "
            "(`cruxible state pull-apply --repair`); claim ids are preserved."
        )

    root = instance.get_root_path()
    pre_pull_snapshot_id = service_create_snapshot(
        instance,
        label=f"pre-pull-{preview.target_release_id}",
        actor_context=actor_context,
    ).snapshot.snapshot_id

    upstream_dir = _materialize_upstream_bundle(root, pulled.root_dir, pulled.manifest.release_id)
    write_runtime_composed_config(
        base_path=upstream_dir / "config.yaml",
        overlay_path=root / upstream.overlay_config_path,
        output_path=instance.get_config_path(),
    )

    current_graph = instance.load_graph()
    local_graph = _extract_local_overlay_graph(current_graph, pulled.manifest)
    next_upstream_graph = _load_graph_from_bundle(upstream_dir)
    # The upstream bundle is graph+config+lock with NO receipts: any receipt_id
    # on an upstream edge points at a receipt in the publishing instance that is
    # absent here. Clear those dangling pointers and stamp clone origin before the
    # merge so no upstream-origin edge in this overlay references a phantom
    # receipt. Local overlay edges keep their receipt_id -- it resolves locally.
    next_upstream_graph.relabel_clone_receipts()
    # LEGACY-IMAGE BACKFILL (pull apply). A pre-identity upstream release never
    # gains ids of its own, so this overlay mints them -- reusing whatever it
    # minted for the same tuples on an earlier pull, so upstream identities stay
    # stable across re-pulls instead of churning invisibly. The bundle bytes are
    # untouched; only this instance's live SQLite learns the ids.
    stored_identity_map = load_legacy_identity_map(
        instance.get_instance_state(LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY)
    )
    minted = backfill_legacy_graph(next_upstream_graph, reuse=stored_identity_map)
    identity_map = record_minted_identities(stored_identity_map, minted)
    conflicts = _find_dangling_reference_conflicts(
        local_graph,
        next_upstream_graph,
        pulled.manifest,
    )
    if conflicts:
        raise ConfigError("Local overlay references entities removed upstream", errors=conflicts)
    merged = EntityGraph.merge_graphs(next_upstream_graph, local_graph)
    # GUARD EXEMPTION (audit F4 / wi-overlay-merge-guard-pass): this
    # save_graph materializes upstream+overlay without running entity/
    # relationship mutation guards, and that is intentional and safe.
    #
    # Why guarding here adds no safety:
    #  * `local_graph` is the overlay's OWN state (types not owned by
    #    upstream). It is not a fresh write -- it is a re-materialization
    #    of state that already passed entity + relationship guards when it
    #    was authored via the guarded write paths (service.mutations
    #    batch_direct_write/add_*, workflow.apply, group_transitions).
    #    Entity guards fire only on a value transition (old != new ==
    #    guarded_value); re-materializing unchanged overlay entities has no
    #    transition to evaluate, so a guard pass here is a no-op.
    #  * `next_upstream_graph` is governed/published, snapshot-first state.
    #    Running guards over it would re-litigate already-governed upstream
    #    content -- outside this overlay's authority and the wrong layer.
    #  * There is no write actor at merge time. The pull-apply is a system
    #    reconciliation; `actor_context` here only labels the pre-pull
    #    snapshot. Feeding it to an actor-identity guard would mis-attribute
    #    or spuriously reject valid, previously-authored overlay state.
    #
    # The one genuinely novel merge-time risk -- local edges dangling onto
    # upstream entities removed in the new release -- is already enforced
    # above by `_find_dangling_reference_conflicts`, which blocks the apply
    # before this materialization. Revisit if overlay state ever becomes
    # writable OUTSIDE the guarded write paths, or if a guard kind is added
    # that evaluates static graph shape rather than per-write transitions.
    materialized_digests = _materialized_upstream_digests(upstream_dir)
    updated = UpstreamMetadata(
        transport_ref=upstream.transport_ref,
        requested_source_ref=upstream.requested_source_ref,
        requested_transport_ref=upstream.requested_transport_ref,
        state_id=pulled.manifest.state_id,
        release_id=pulled.manifest.release_id,
        snapshot_id=pulled.manifest.snapshot_id,
        compatibility=pulled.manifest.compatibility,
        owned_entity_types=pulled.manifest.owned_entity_types,
        owned_relationship_types=pulled.manifest.owned_relationship_types,
        bundle_format_version=pulled.manifest.bundle_format_version,
        members_digest=pulled.manifest.members_digest,
        identity_map_digest=legacy_identity_map_digest(identity_map),
        overlay_config_path=upstream.overlay_config_path,
        manifest_path=str((upstream_dir / "manifest.json").relative_to(root)),
        graph_path=str((upstream_dir / "graph.json").relative_to(root)),
        upstream_config_path=str((upstream_dir / "config.yaml").relative_to(root)),
        lock_path=str((upstream_dir / "cruxible.lock.yaml").relative_to(root)),
        **materialized_digests,
    )
    # ONE commit boundary for the state replacement AND its receipt.
    #
    # Before this the graph replacement committed on its own and the receipt was
    # persisted in a SECOND transaction afterwards, so a crash (or any failure)
    # between them left the state applied and unreceipted while the caller saw an
    # error — the overlay could not answer "which release put this state here"
    # for the very apply that had actually happened. ``write_transaction`` is
    # re-entrant, so ``save_graph`` and ``save_graph_delta`` below join THIS
    # boundary instead of opening their own: graph rows and receipt row now land
    # in the same commit, or neither does.
    #
    # NOTE (dispute with the review's framing): upstream metadata and the lock
    # file are NOT writes to the state store. ``set_upstream_metadata`` rewrites
    # ``.cruxible/instance.json`` and ``service_lock`` writes
    # ``cruxible.lock.yaml`` — plain filesystem writes that cannot enlist in a
    # SQLite transaction. They are therefore ordered AFTER the durable commit on
    # purpose: a crash there leaves the graph applied and RECEIPTED with stale
    # bookkeeping, which a re-run repairs, whereas doing them first would leave
    # instance.json advertising a release whose graph had been rolled back.
    with instance.write_transaction() as uow:
        instance.save_graph(merged)
        if identity_map != stored_identity_map:
            uow.snapshots.set_instance_state(
                LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY,
                dump_legacy_identity_map(identity_map),
            )
        materialize_local_operator_auth_managed_entities(instance)
        receipt_id = _record_state_pull_apply_receipt(
            instance,
            uow,
            previous_release_id=upstream.release_id,
            updated=updated,
            apply_digest=preview.apply_digest,
            pre_pull_snapshot_id=pre_pull_snapshot_id,
            materialized_digests=materialized_digests,
            actor_context=actor_context,
        )

    instance.set_upstream_metadata(updated)
    service_lock(instance)
    return StatePullApplyResult(
        release_id=updated.release_id,
        apply_digest=preview.apply_digest,
        pre_pull_snapshot_id=pre_pull_snapshot_id,
        receipt_id=receipt_id,
    )


def _record_state_pull_apply_receipt(
    instance: InstanceProtocol,
    uow: UnitOfWorkProtocol,
    *,
    previous_release_id: str | None,
    updated: UpstreamMetadata,
    apply_digest: str,
    pre_pull_snapshot_id: str,
    materialized_digests: _MaterializedUpstreamDigests,
    actor_context: GovernedActorContext | None,
) -> str:
    """Mint the audit receipt for a completed pull-apply.

    Pull-apply replaces the active config AND the whole graph from an upstream
    release. Before this it was the only write of that magnitude that left no
    receipt, so an overlay could not answer "which release put this state here"
    from its own audit trail. The receipt pins the release identity on both
    sides of the move plus every materialized member digest, which is exactly
    what ``snapshot.upstream_verification`` later re-checks reads against.

    Persisted through the CALLER'S ``uow`` — never its own
    ``write_transaction`` — so the receipt shares the commit boundary of the
    graph replacement it describes. Taking a fresh boundary here is what made
    the receipt separately losable.
    """
    builder = ReceiptBuilder(
        query_name="state_pull_apply",
        operation_type="state_pull_apply",
        parameters={
            "state_id": updated.state_id,
            "previous_release_id": previous_release_id,
            "release_id": updated.release_id,
            "snapshot_id": updated.snapshot_id,
            "transport_ref": updated.transport_ref,
            "apply_digest": apply_digest,
            "pre_pull_snapshot_id": pre_pull_snapshot_id,
            "members_digest": updated.members_digest,
            **materialized_digests,
        },
        actor_context=actor_context,
    )
    try:
        builder.stamp_state_coordinates(
            head_snapshot_id=instance.get_head_snapshot_id(),
            read_revision=instance.get_read_revision(),
        )
    except Exception:  # pragma: no cover - coordinate read is advisory
        _logger.warning("Failed to read state coordinates for pull-apply receipt", exc_info=True)
    builder.mark_committed()
    receipt = builder.build()
    uow.receipts.save_receipt(receipt)
    return receipt.receipt_id


def _pull_bundle(transport_ref: str) -> tuple[PulledReleaseBundle, list[str]]:
    """Pull a release bundle and digest-verify every member before returning it."""
    transport, resolved_ref = resolve_transport(transport_ref)
    temp_root = Path(tempfile.mkdtemp(prefix="cruxible_release_"))
    pulled = transport.pull(resolved_ref, temp_root)
    warnings = verify_release_bundle(pulled, transport_ref=transport_ref)
    return pulled, warnings


def build_release_bundle(
    *,
    instance: InstanceProtocol,
    snapshot_id: str,
    state_id: str,
    release_id: str,
    compatibility: StateCompatibility,
    parent_release_id: str | None,
) -> Path:
    snapshot = instance.get_snapshot(snapshot_id)
    if snapshot is None:
        raise ConfigError(f"Snapshot '{snapshot_id}' not found")
    snapshot_dir = instance.get_instance_dir() / "snapshots" / snapshot_id
    export_snapshot = getattr(instance, "_export_snapshot_artifacts", None)
    if callable(export_snapshot):
        snapshot_dir = export_snapshot(snapshot_id)
    bundle_dir = Path(tempfile.mkdtemp(prefix="cruxible_bundle_"))
    for name in RELEASE_BUNDLE_MEMBERS:
        source = snapshot_dir / name
        if source.exists():
            shutil.copy2(source, bundle_dir / name)
    config = instance.load_config()
    # The snapshot members are pinned first so the manifest can carry the
    # sidecar's digest; the sidecar then pins the manifest. Neither can pin the
    # other's final bytes, so the cycle is broken by scope: members_digest
    # covers every member EXCEPT the manifest, and the sidecar covers the
    # manifest too. Stripping the sidecar leaves a manifest that still declares
    # it, and swapping the sidecar leaves a members_digest that no longer
    # matches -- both detectable without either file pinning itself.
    snapshot_member_digests = {
        name: _sha256_bytes((bundle_dir / name).read_bytes())
        for name in RELEASE_BUNDLE_MEMBERS
        if (bundle_dir / name).exists()
    }
    manifest = PublishedStateManifest(
        state_id=state_id,
        release_id=release_id,
        snapshot_id=snapshot_id,
        compatibility=compatibility,
        owned_entity_types=sorted(config.entity_types.keys()),
        owned_relationship_types=sorted(rel.name for rel in config.relationships),
        parent_release_id=parent_release_id,
        bundle_format_version=MEMBER_DIGESTS_FORMAT_VERSION,
        members_digest=_compute_members_digest(snapshot_member_digests),
    )
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    _write_release_member_digests(
        bundle_dir,
        {
            **snapshot_member_digests,
            "manifest.json": _sha256_bytes(manifest_path.read_bytes()),
        },
    )
    return bundle_dir


def _members_sidecar_payload(digests: dict[str, str]) -> str:
    return json.dumps(
        {"format_version": MEMBER_DIGESTS_FORMAT_VERSION, "digests": digests},
        indent=2,
        sort_keys=True,
    )


def _compute_members_digest(digests: dict[str, str]) -> str:
    """Digest the sidecar body that pins every member other than the manifest.

    Scoped to exclude ``manifest.json`` because the manifest carries this value:
    including it would require the manifest to pin its own final bytes.
    """
    return _sha256_bytes(
        _members_sidecar_payload(
            {name: digest for name, digest in digests.items() if name != "manifest.json"}
        ).encode()
    )


def _write_release_member_digests(bundle_dir: Path, digests: dict[str, str]) -> None:
    """Emit the per-member digest sidecar that pull-side verification compares against.

    Every file already written into the bundle -- ``manifest.json`` included --
    is pinned by the sha256 of its exact bytes. The sidecar cannot pin itself,
    so it is the one member excluded; the manifest's ``members_digest`` covers
    it instead, and the manifest is what a consumer reads first.
    """
    (bundle_dir / RELEASE_MEMBER_DIGESTS_FILE).write_text(_members_sidecar_payload(digests))


def verify_release_bundle(pulled: PulledReleaseBundle, *, transport_ref: str) -> list[str]:
    """Digest-verify every member of a pulled release bundle before it is used.

    Refuses (never warns, never silently accepts) when a member's bytes do not
    match the digest the publisher recorded for it. Returns the warnings that a
    caller must surface: bundles published before ``members.json`` existed carry
    no digest for ``config.yaml``, so that one member stays unverifiable until
    the upstream re-publishes.

    The manifest's ``bundle_format_version`` makes that pre-field story
    non-downgradable. A bundle that declares the sidecar must produce it: strip
    ``members.json`` from a current bundle and the manifest still says it should
    be there, so the pull refuses instead of silently falling back to the weaker
    verification an old bundle gets.
    """
    root = pulled.root_dir
    label = f"{pulled.manifest.state_id}:{pulled.manifest.release_id}"
    warnings: list[str] = []
    sidecar_path = root / RELEASE_MEMBER_DIGESTS_FILE
    declared_format = pulled.manifest.bundle_format_version
    if declared_format is not None:
        _verify_declared_member_contract(
            root,
            sidecar_path,
            declared_format=declared_format,
            expected_members_digest=pulled.manifest.members_digest,
            label=label,
            ref=transport_ref,
        )
    elif sidecar_path.exists():
        _verify_release_member_digests(root, sidecar_path, label=label, ref=transport_ref)
    else:
        warnings.append(
            f"Release bundle {label} pulled from {transport_ref} predates per-member "
            f"digests ({RELEASE_MEMBER_DIGESTS_FILE} is absent): graph.json and "
            "cruxible.lock.yaml were still verified against snapshot.json, but "
            "config.yaml could not be verified. Re-publish the release upstream with "
            "a current Cruxible to get full bundle verification."
        )

    # snapshot.json has always recorded raw-byte digests for the graph and lock
    # members. Verify them for every bundle, pre-field ones included.
    _verify_recorded_member_digest(
        root / "graph.json",
        expected=pulled.snapshot.graph_digest,
        member="graph.json",
        source="snapshot.json",
        label=label,
        ref=transport_ref,
    )
    _verify_recorded_member_digest(
        root / "cruxible.lock.yaml",
        expected=pulled.snapshot.lock_digest,
        member="cruxible.lock.yaml",
        source="snapshot.json",
        label=label,
        ref=transport_ref,
    )
    return warnings


def _verify_declared_member_contract(
    root: Path,
    sidecar_path: Path,
    *,
    declared_format: int,
    expected_members_digest: str | None,
    label: str,
    ref: str,
) -> None:
    """Enforce the member contract a bundle's manifest declares.

    The manifest is the one member every consumer reads first, so it is where
    the bundle states what verification it supports. Declaring the sidecar and
    then not shipping it -- or shipping one whose body contradicts the digest
    the manifest recorded -- is a downgrade attempt, not a legacy bundle.
    """
    if declared_format > MEMBER_DIGESTS_FORMAT_VERSION:
        raise ConfigError(
            f"Release bundle {label} pulled from {ref} declares bundle_format_version "
            f"{declared_format}, but this Cruxible understands at most "
            f"{MEMBER_DIGESTS_FORMAT_VERSION}. It was published by a newer Cruxible and "
            "cannot be verified here; upgrade Cruxible, then pull again."
        )
    if not sidecar_path.exists():
        raise ConfigError(
            f"Release bundle {label} pulled from {ref} declares bundle_format_version "
            f"{declared_format} in its manifest, which means it was published with "
            f"{RELEASE_MEMBER_DIGESTS_FILE}, but that file is absent. The per-member "
            "digest sidecar was stripped after publication, so the bundle is refused "
            "rather than verified against the weaker pre-sidecar rules. Re-publish the "
            "release upstream and pull again."
        )
    digests = _verify_release_member_digests(root, sidecar_path, label=label, ref=ref)
    if expected_members_digest is None:
        return
    actual = _compute_members_digest(digests)
    if actual != expected_members_digest:
        raise ConfigError(
            f"Release bundle {label} pulled from {ref} has a "
            f"{RELEASE_MEMBER_DIGESTS_FILE} that its manifest does not vouch for: the "
            f"manifest records members_digest={expected_members_digest}, the sidecar "
            f"body hashes to {actual}. The sidecar was replaced after publication, so "
            "the members it pins cannot be trusted. Re-publish the release upstream "
            "and pull again."
        )


def _verify_release_member_digests(
    root: Path,
    sidecar_path: Path,
    *,
    label: str,
    ref: str,
) -> dict[str, str]:
    try:
        raw = json.loads(sidecar_path.read_text())
    except ValueError as exc:
        raise ConfigError(
            f"Release bundle {label} pulled from {ref} has an unreadable "
            f"{RELEASE_MEMBER_DIGESTS_FILE}: {exc}. The bundle is not usable as "
            "pulled; re-publish the release upstream and pull again."
        ) from exc
    digests = raw.get("digests") if isinstance(raw, dict) else None
    if not isinstance(digests, dict) or not digests:
        raise ConfigError(
            f"Release bundle {label} pulled from {ref} has a malformed "
            f"{RELEASE_MEMBER_DIGESTS_FILE}: expected a non-empty 'digests' mapping. "
            "Re-publish the release upstream and pull again."
        )

    for member in sorted(digests):
        expected = digests[member]
        path = root / member
        if not path.exists():
            raise ConfigError(
                f"Release bundle {label} pulled from {ref} is missing member "
                f"'{member}', which {RELEASE_MEMBER_DIGESTS_FILE} pins at {expected}. "
                "The bundle is incomplete or was altered in transit; re-publish the "
                "release upstream and pull again."
            )
        actual = _sha256_bytes(path.read_bytes())
        if actual != expected:
            raise ConfigError(
                f"Release bundle {label} pulled from {ref} failed digest verification "
                f"for member '{member}': expected {expected}, found {actual}. The "
                "published bundle was altered after it was published; nothing from it "
                "is applied. Re-publish the release upstream under a new release_id, "
                "then pull again -- never edit a pulled bundle in place."
            )

    unpinned = sorted(
        path.name
        for path in root.iterdir()
        if path.is_file() and path.name != RELEASE_MEMBER_DIGESTS_FILE and path.name not in digests
    )
    if unpinned:
        raise ConfigError(
            f"Release bundle {label} pulled from {ref} carries members that "
            f"{RELEASE_MEMBER_DIGESTS_FILE} does not pin: {', '.join(unpinned)}. "
            "Unpinned content is refused rather than trusted; re-publish the release "
            "upstream and pull again."
        )
    return {str(member): str(digest) for member, digest in digests.items()}


def _verify_recorded_member_digest(
    path: Path,
    *,
    expected: str | None,
    member: str,
    source: str,
    label: str,
    ref: str,
) -> None:
    if expected is None:
        return
    if not path.exists():
        raise ConfigError(
            f"Release bundle {label} pulled from {ref} is missing member '{member}', "
            f"which {source} pins at {expected}. The bundle is incomplete; re-publish "
            "the release upstream and pull again."
        )
    actual = _sha256_bytes(path.read_bytes())
    if actual != expected:
        raise ConfigError(
            f"Release bundle {label} pulled from {ref} failed digest verification for "
            f"member '{member}': {source} records {expected}, found {actual}. Nothing "
            "from the bundle is applied. Re-publish the release upstream under a new "
            "release_id, then pull again."
        )


def _verify_release_immutability(root: Path, bundle_dir: Path, release_id: str) -> None:
    """Refuse a release_id that now resolves to different content than it did.

    The transport ref an overlay tracks is a moving tag by design -- new releases
    arrive through it -- so the tag itself cannot be pinned. A release_id can:
    it names immutable published content. Resolving one to different bytes than
    were already materialized means the release was rewritten upstream, and
    overwriting the local copy would launder that rewrite into the overlay.
    """
    releases_dir = root / ".cruxible" / "upstream" / "releases" / release_id
    if not releases_dir.exists():
        return
    already = compute_bundle_digest(releases_dir)
    incoming = compute_bundle_digest(bundle_dir)
    if already == incoming:
        return
    raise ConfigError(
        f"Release '{release_id}' was already materialized at {releases_dir} with content "
        f"digest {already}, but the transport now resolves that same release_id to "
        f"{incoming}. Published releases are immutable, so the rewritten bundle is "
        "refused rather than applied. Publish the changed state upstream under a NEW "
        "release_id and pull that; if the local copy is what drifted, delete the "
        "materialized release directory and pull again."
    )


def _materialize_upstream_bundle(root: Path, bundle_dir: Path, release_id: str) -> Path:
    releases_dir = root / ".cruxible" / "upstream" / "releases" / release_id
    current_dir = root / ".cruxible" / "upstream" / "current"
    _verify_release_immutability(root, bundle_dir, release_id)
    shutil.copytree(bundle_dir, releases_dir, dirs_exist_ok=True)
    shutil.rmtree(current_dir, ignore_errors=True)
    shutil.copytree(releases_dir, current_dir)
    return current_dir


def _write_default_overlay_config(root: Path, state_id: str, upstream_dir: Path) -> Path:
    overlay_path = root / "config.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(
        "\n".join(
            [
                "version: '1.0'",
                f"name: {state_id}-overlay",
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


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class _MaterializedUpstreamDigests(TypedDict):
    manifest_digest: str | None
    graph_digest: str | None
    upstream_config_digest: str | None
    upstream_lock_digest: str | None


def _materialized_upstream_digests(upstream_dir: Path) -> _MaterializedUpstreamDigests:
    """Pin every materialized upstream member the overlay will later read back.

    Recorded at pull time, compared by
    ``cruxible_core.snapshot.upstream_verification`` on every read of the
    materialized upstream. Pinning all four -- not just the manifest and graph
    the pull delta uses -- is what lets config reload and ownership resolution
    verify ``config.yaml`` before composing against it.
    """
    return {
        "manifest_digest": sha256_file(upstream_dir / "manifest.json"),
        "graph_digest": sha256_file(upstream_dir / "graph.json"),
        "upstream_config_digest": sha256_file(upstream_dir / "config.yaml"),
        "upstream_lock_digest": sha256_file(upstream_dir / "cruxible.lock.yaml"),
    }


def _lock_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text()


def _compute_state_apply_digest(
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
    ownership: PublishedStateManifest,
) -> EntityGraph:
    """Split the overlay's own state out of the merged graph.

    ``ownership`` is the manifest whose ownership decides what "local" means,
    and on a pull it must be the NEW manifest, not the tracked (stale) upstream
    metadata. A type the incoming release has TAKEN OVER would otherwise still
    look local, be extracted, and then collide with the upstream edge of the
    same tuple at merge time -- the stale-ownership window. The merge guard
    refuses that collision loudly; passing the new ownership here means it never
    arises.
    """
    local_entity_types = [
        entity_type
        for entity_type in current_graph.list_entity_types()
        if entity_type not in set(ownership.owned_entity_types)
    ]
    local_relationship_types = [
        relationship_type
        for relationship_type in current_graph.list_relationship_types()
        if relationship_type not in set(ownership.owned_relationship_types)
    ]
    return current_graph.extract_owned_subgraph(
        entity_types=local_entity_types,
        relationship_types=local_relationship_types,
    )


def _find_dangling_reference_conflicts(
    local_graph: EntityGraph,
    next_upstream_graph: EntityGraph,
    manifest: PublishedStateManifest,
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
