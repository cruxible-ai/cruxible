"""Lifecycle service functions."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import yaml

from cruxible_core.config.composer import (
    ResolvedConfigLayer,
    compose_config_sequence,
    compose_runtime_config_files,
    rebase_artifact_uri,
    resolve_config_layers,
    resolve_overlay_kit_base_layer,
)
from cruxible_core.config.loader import load_config, load_config_from_string, save_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.kits import (
    KIT_MANIFEST_FILE,
    KitBundle,
    config_yaml_has_kit_provider_refs,
    copy_kit_runtime_files,
    is_kit_provider_ref,
    load_kit_manifest,
    materialize_kit,
    namespace_kit_provider_ref,
    resolve_kit_ref,
    write_materialized_kit_metadata,
)
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.server.auth_managed_entities import (
    materialize_local_operator_auth_managed_entities,
)
from cruxible_core.service.types import (
    InitResult,
    ReloadConfigResult,
    ValidateServiceResult,
)
from cruxible_core.workflow.compiler import (
    LOCK_FILE_NAME,
    build_lock,
    compute_lock_config_digest,
    compute_lock_digest,
    get_lock_path,
    load_lock,
    write_lock,
)
from cruxible_core.workflow.types import LockedArtifact, LockedProvider, WorkflowLock

_INSTANCE_KITS_DIR = "kits"

_MANAGED_CONFIG_RELATIVE_PATH = Path(CruxibleInstance.INSTANCE_DIR) / "configs" / "active.yaml"


def ensure_auth_managed_runtime_identity(instance: InstanceProtocol) -> list[str]:
    """Ensure auth-managed identity matches the daemon auth tier.

    Auth-on daemons use credential-backed identity: runtime credential minting is
    the source of truth and materializes auth-managed entities for credential
    labels. Auth-off daemons have no credentials, so configs that declare
    auth-managed types get a declared local operator identity, visible as
    ``operator`` in graph state and write provenance.
    """
    return materialize_local_operator_auth_managed_entities(instance)


def service_validate(
    config_path: str | None = None,
    config_yaml: str | None = None,
) -> ValidateServiceResult:
    """Validate a config file or inline YAML string.

    If the config uses ``extends``, the base config is resolved and
    composed in memory before validation.  For file-based configs the
    base path is resolved relative to the config file's directory.  For
    inline ``config_yaml``, ``extends`` must be an absolute path or a
    ``ConfigError`` is raised.
    """
    sources = sum(value is not None for value in (config_path, config_yaml))
    if sources == 0:
        raise ConfigError("Provide exactly one of config_path or config_yaml")
    if sources > 1:
        raise ConfigError("Provide exactly one of config_path or config_yaml")

    if config_yaml is not None:
        config = load_config_from_string(config_yaml)
        config_source_path: Path | None = None
    else:
        assert config_path is not None
        config_source_path = Path(config_path).resolve()
        config = load_config(config_source_path)

    config = compose_config_sequence(
        resolve_config_layers(config, config_path=config_source_path),
    )

    warnings = validate_config(config)
    return ValidateServiceResult(config=config, warnings=warnings)


def service_init(
    root_dir: str | Path,
    config_path: str | None = None,
    config_yaml: str | None = None,
    data_dir: str | None = None,
    kits: Sequence[str] | None = None,
    *,
    instance_mode: str = CruxibleInstance.DEV_MODE,
) -> InitResult:
    """Initialize a new cruxible instance (create-only).

    Inline YAML is normalized into an instance-managed active config.  If
    the source config uses ``extends``, the composed config is flattened into
    the same managed path so the initialized instance has a self-contained
    config without assuming a caller-provided filename.  Kit-backed inits
    materialize each bundle under ``kits/<kit_id>/`` and flatten the ordered
    composition of all kit layers into the managed config.
    """
    root = Path(root_dir)
    normalized_kits = [value.strip() for value in (kits or []) if value.strip()]
    sources = sum(
        value is not None for value in (config_path, config_yaml, normalized_kits or None)
    )
    if sources != 1:
        raise ConfigError("Provide exactly one of config_path, config_yaml, or kits")

    wrote_managed_config = False
    materialized_kit_dirs: list[tuple[str, Path]] = []

    if normalized_kits:
        bundles = [resolve_kit_ref(value) for value in normalized_kits]
        _validate_kit_sequence(normalized_kits, bundles)
        try:
            layers: list[ResolvedConfigLayer] = []
            for kit_ref, bundle in zip(normalized_kits, bundles):
                kit_id = bundle.manifest.kit_id
                kit_dir = root / _INSTANCE_KITS_DIR / kit_id
                if kit_dir.exists():
                    raise ConfigError(
                        f"Kit directory already exists at {kit_dir}; refusing to overwrite"
                    )
                # Recorded before materialization so a partial copy is swept on
                # failure; otherwise the leftover dir blocks every retry.
                materialized_kit_dirs.append((kit_id, kit_dir))
                entry_config = materialize_kit(
                    kit=kit_ref,
                    root=kit_dir,
                    expected_role=bundle.manifest.role,
                )
                layer_config = load_config(entry_config)
                _namespace_config_kit_provider_refs(layer_config, kit_id)
                layers.append(ResolvedConfigLayer(config=layer_config, config_path=entry_config))
            composed = compose_config_sequence(layers)
            config_path = _save_managed_config(root, composed)
            wrote_managed_config = True
        except Exception:
            _cleanup_managed_config(root)
            _cleanup_materialized_kits(root, materialized_kit_dirs)
            raise

    if config_yaml is not None:
        config = load_config_from_string(config_yaml)
        config = compose_config_sequence(
            resolve_config_layers(config, config_dir=root),
        )
        config_path = _save_managed_config(root, config)
        wrote_managed_config = True

    assert config_path is not None
    resolved = Path(config_path)
    if not resolved.is_absolute():
        resolved = root / resolved

    # Compose extends overlay before init so the instance gets a self-contained config.
    config = load_config(resolved)
    if config.extends is not None:
        try:
            composed = compose_config_sequence(
                resolve_config_layers(config, config_path=resolved),
            )
            config_path = _save_managed_config(root, composed)
            wrote_managed_config = True
        except Exception:
            if wrote_managed_config:
                _cleanup_managed_config(root)
            raise

    effective_config_path = Path(config_path)
    if not effective_config_path.is_absolute():
        effective_config_path = root / effective_config_path

    try:
        instance = CruxibleInstance.init(
            root,
            config_path,
            data_dir,
            instance_mode=instance_mode,
        )
        ensure_auth_managed_runtime_identity(instance)
    except Exception:
        if wrote_managed_config:
            _cleanup_managed_config(root)
        _cleanup_materialized_kits(root, materialized_kit_dirs)
        raise

    if materialized_kit_dirs:
        _install_instance_lock_from_composed_kits(instance, materialized_kit_dirs)

    loaded = instance.load_config()
    warnings = validate_config(loaded)

    return InitResult(instance=instance, warnings=warnings)


def _validate_kit_sequence(kit_refs: Sequence[str], bundles: Sequence[KitBundle]) -> None:
    """Validate a composed init kit sequence: standalone base, overlays on earlier kits."""
    first = bundles[0].manifest
    if first.role != "standalone":
        raise ConfigError(
            f"The first kit in an init sequence must be role: standalone, but "
            f"'{first.kit_id}' is role: {first.role}"
            + (
                f" targeting state '{first.target_state}'. List its base kit first, "
                f"e.g. `cruxible init --kit {first.target_state} --kit {first.kit_id}`, "
                "or use `cruxible state create-overlay --kit` for a published state."
                if first.role == "overlay"
                else ""
            )
        )
    seen_kit_ids = [first.kit_id]
    for kit_ref, bundle in zip(kit_refs[1:], bundles[1:]):
        manifest = bundle.manifest
        if manifest.role != "overlay":
            raise ConfigError(
                f"Kit '{manifest.kit_id}' has role '{manifest.role}'; every kit after "
                "the first in an init sequence must be role: overlay"
            )
        if manifest.kit_id in seen_kit_ids:
            raise ConfigError(f"Kit '{manifest.kit_id}' appears more than once in the sequence")
        if manifest.target_state not in seen_kit_ids:
            raise ConfigError(
                f"Kit '{manifest.kit_id}' targets state '{manifest.target_state}', which "
                f"is not an earlier kit in the sequence [{', '.join(seen_kit_ids)}]"
            )
        seen_kit_ids.append(manifest.kit_id)


def _namespace_config_kit_provider_refs(config: CoreConfig, kit_id: str) -> None:
    """Rewrite a kit layer's kit:// provider refs to their kit-scoped form in place."""
    for provider in config.providers.values():
        if is_kit_provider_ref(provider.ref):
            provider.ref = namespace_kit_provider_ref(provider.ref, kit_id)


def _cleanup_materialized_kits(root: Path, kit_dirs: list[tuple[str, Path]]) -> None:
    for _kit_id, kit_dir in kit_dirs:
        shutil.rmtree(kit_dir, ignore_errors=True)
    kits_root = root / _INSTANCE_KITS_DIR
    try:
        if kits_root.exists() and not any(kits_root.iterdir()):
            kits_root.rmdir()
    except OSError:
        pass


def service_init_governed_upload(
    root_dir: str | Path,
    *,
    workspace_root: str | Path,
    config_yaml: str | None = None,
    data_dir: str | None = None,
    kits: Sequence[str] | None = None,
) -> InitResult:
    """Initialize a governed instance from caller-owned uploaded config content."""
    governed_root = Path(root_dir)
    caller_workspace = Path(workspace_root)
    copied_kit_runtime_files = False

    if config_yaml is not None:
        has_kit_refs = config_yaml_has_kit_provider_refs(config_yaml)
        config_yaml = _normalize_uploaded_config_yaml(
            config_yaml,
            config_base_dir=caller_workspace,
        )
        has_kit_refs = has_kit_refs or config_yaml_has_kit_provider_refs(config_yaml)
        if has_kit_refs and not (caller_workspace / KIT_MANIFEST_FILE).exists():
            raise ConfigError(
                "Uploaded config contains kit:// provider refs, but the workspace root "
                "does not contain cruxible-kit.yaml. Use `cruxible init --kit` for "
                "standalone kits, or `cruxible state create-overlay --kit` for overlay kits."
            )
        if (caller_workspace / KIT_MANIFEST_FILE).exists():
            copy_kit_runtime_files(
                caller_workspace,
                governed_root,
                include_entry_config=False,
            )
            copied_kit_runtime_files = True

    result = service_init(
        governed_root,
        config_path=None,
        config_yaml=config_yaml,
        data_dir=data_dir,
        kits=kits,
        instance_mode=CruxibleInstance.GOVERNED_MODE,
    )
    if copied_kit_runtime_files:
        write_materialized_kit_metadata(governed_root)
        _install_instance_lock_from_materialized_kit(result.instance)
    return result


def _install_instance_lock_from_composed_kits(
    instance: InstanceProtocol,
    kit_dirs: Sequence[tuple[str, Path]],
) -> None:
    """Install the instance lock by merging the materialized kits' bundled locks.

    Workflows/providers/artifacts are append-only across layers, so entry name
    collisions across bundled locks are refused as errors. Provider ``kit://``
    refs are rewritten to their kit-scoped form and relative artifact URIs are
    rebased against each kit root so the merged entries match the composed
    managed config. If the merged lock does not cover the composed config
    exactly (stale or placeholder bundled locks), the instance lock is
    regenerated from the composed config instead, mirroring the single-kit
    regeneration fallback.
    """
    config = instance.load_config()
    instance_lock_path = get_lock_path(instance)
    instance_lock_path.parent.mkdir(parents=True, exist_ok=True)

    merged = _merge_kit_locks(config, kit_dirs)
    if merged is not None:
        write_lock(merged, instance_lock_path)
        return

    try:
        regenerated_lock = build_lock(
            config,
            instance.get_config_path().parent,
        )
        write_lock(regenerated_lock, instance_lock_path)
    except Exception as exc:
        raise ConfigError(
            "Bundled kit locks do not cover the composed instance config, and "
            f"regenerating the instance-local lock failed: {exc}"
        ) from exc


def _merge_kit_locks(
    config: CoreConfig,
    kit_dirs: Sequence[tuple[str, Path]],
) -> WorkflowLock | None:
    """Merge per-kit bundled locks; return None when the merge cannot stand as-is."""
    merged_artifacts: dict[str, LockedArtifact] = {}
    merged_providers: dict[str, LockedProvider] = {}
    for kit_id, kit_dir in kit_dirs:
        bundled_lock_path = kit_dir / LOCK_FILE_NAME
        try:
            bundled_lock = load_lock(bundled_lock_path)
        except Exception as exc:
            raise ConfigError(f"Bundled kit lock is invalid: {bundled_lock_path}") from exc
        if bundled_lock.lock_digest is None or bundled_lock.lock_digest != compute_lock_digest(
            bundled_lock
        ):
            return None
        layer_config = load_config(kit_dir / load_kit_manifest(kit_dir).entry_config)
        if bundled_lock.config_digest != compute_lock_config_digest(layer_config):
            return None
        for name, artifact in bundled_lock.artifacts.items():
            if name in merged_artifacts:
                raise ConfigError(
                    f"Kit '{kit_id}' lock redefines artifact '{name}' already locked "
                    "by an earlier kit in the sequence"
                )
            merged_artifacts[name] = artifact.model_copy(
                update={"uri": rebase_artifact_uri(artifact.uri, kit_dir)}
            )
        for name, provider in bundled_lock.providers.items():
            if name in merged_providers:
                raise ConfigError(
                    f"Kit '{kit_id}' lock redefines provider '{name}' already locked "
                    "by an earlier kit in the sequence"
                )
            merged_ref = (
                namespace_kit_provider_ref(provider.ref, kit_id)
                if is_kit_provider_ref(provider.ref)
                else provider.ref
            )
            merged_providers[name] = provider.model_copy(update={"ref": merged_ref})

    if set(merged_providers) != set(config.providers) or set(merged_artifacts) != set(
        config.artifacts
    ):
        return None
    for name, provider_schema in config.providers.items():
        if merged_providers[name].ref != provider_schema.ref:
            return None
    for name, artifact_schema in config.artifacts.items():
        if merged_artifacts[name].uri != artifact_schema.uri:
            return None

    lock = WorkflowLock(
        config_digest=compute_lock_config_digest(config),
        artifacts=merged_artifacts,
        providers=merged_providers,
    )
    lock.lock_digest = compute_lock_digest(lock)
    return lock


def _install_instance_lock_from_materialized_kit(instance: InstanceProtocol) -> None:
    root = instance.get_root_path()
    bundled_lock_path = root / LOCK_FILE_NAME
    if not bundled_lock_path.exists():
        return

    instance_lock_path = get_lock_path(instance)
    instance_lock_path.parent.mkdir(parents=True, exist_ok=True)
    config = instance.load_config()
    config_digest = compute_lock_config_digest(config)

    try:
        bundled_lock = load_lock(bundled_lock_path)
    except Exception as exc:
        raise ConfigError(f"Bundled kit lock is invalid: {bundled_lock_path}") from exc

    if (
        bundled_lock.config_digest == config_digest
        and bundled_lock.lock_digest == compute_lock_digest(bundled_lock)
    ):
        instance_lock_path.write_bytes(bundled_lock_path.read_bytes())
        return

    try:
        regenerated_lock = build_lock(
            config,
            instance.get_config_path().parent,
        )
        write_lock(regenerated_lock, instance_lock_path)
    except Exception as exc:
        raise ConfigError(
            "Bundled kit lock does not match the active instance config or lock "
            "digest, and regenerating the instance-local lock failed"
        ) from exc


def _save_managed_config(root: Path, config: CoreConfig) -> str:
    """Persist the active config under instance-owned metadata."""
    managed_path = root / _MANAGED_CONFIG_RELATIVE_PATH
    try:
        managed_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Failed to create directory {managed_path.parent}: {exc}") from exc
    save_config(config, managed_path)
    return str(_MANAGED_CONFIG_RELATIVE_PATH)


def _cleanup_managed_config(root: Path) -> None:
    try:
        (root / _MANAGED_CONFIG_RELATIVE_PATH).unlink(missing_ok=True)
    except Exception:
        pass


def _normalize_uploaded_config_yaml(
    config_yaml: str,
    *,
    config_base_dir: str | Path,
) -> str:
    """Compose raw uploaded YAML against the caller-side config base directory."""
    base_dir = Path(config_base_dir)
    uploaded_from_overlay_kit = _dir_is_overlay_kit(base_dir)
    config = load_config_from_string(
        config_yaml,
        partial_layer=uploaded_from_overlay_kit,
    )
    layers = resolve_config_layers(config, config_dir=base_dir)
    if uploaded_from_overlay_kit and config.extends is None and len(layers) == 1:
        # The uploaded content is the overlay kit's own layer; resolve its base
        # from the manifest's target_state since extends-less overlay kits
        # declare composition through the manifest.
        base_layer = resolve_overlay_kit_base_layer(config_dir=base_dir.resolve())
        if base_layer is not None:
            layers = [base_layer, *layers]
    config = compose_config_sequence(layers)
    data = config.model_dump(mode="python", by_alias=True, exclude_none=True)
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)


def _dir_is_overlay_kit(directory: Path) -> bool:
    if not (directory / KIT_MANIFEST_FILE).exists():
        return False
    try:
        return load_kit_manifest(directory).role == "overlay"
    except ConfigError:
        return False


def service_reload_config(
    instance: InstanceProtocol,
    config_path: str | None = None,
    config_yaml: str | None = None,
    *,
    config_base_dir: str | Path | None = None,
) -> ReloadConfigResult:
    """Validate, replace, or repoint the active config for an existing instance."""
    if config_path is not None and config_yaml is not None:
        raise ConfigError("Provide config_path or config_yaml, not both")
    if config_yaml is not None and config_base_dir is not None:
        config_yaml = _normalize_uploaded_config_yaml(
            config_yaml,
            config_base_dir=config_base_dir,
        )

    upstream = instance.get_upstream_metadata()
    if upstream is not None:
        # Release-backed overlays keep the upstream config immutable and track a
        # local overlay file. Reload always regenerates the composed active
        # config that the instance actually reads.
        root = instance.get_root_path()
        overlay_path = root / (config_path or upstream.overlay_config_path)
        if not overlay_path.is_absolute():
            overlay_path = root / overlay_path
        if config_yaml is None and not overlay_path.exists():
            raise ConfigError(f"Overlay config not found: {overlay_path}")

        base_path = root / upstream.upstream_config_path
        active_path = instance.get_config_path()
        if config_yaml is not None:
            # Raw uploaded overlay YAML has no source filename; use the tracked
            # overlay path only as the base directory for relative extends and
            # artifact references before writing it to disk.
            overlay = load_config_from_string(config_yaml)
            composed = compose_config_sequence(
                resolve_config_layers(overlay, config_path=overlay_path),
                runtime=True,
            )
            overlay_path.parent.mkdir(parents=True, exist_ok=True)
            overlay_path.write_text(config_yaml)
            active_path.parent.mkdir(parents=True, exist_ok=True)
            save_config(composed, active_path)
        else:
            composed = compose_runtime_config_files(
                base_path=base_path,
                overlay_path=overlay_path,
            )
            active_path.parent.mkdir(parents=True, exist_ok=True)
            save_config(composed, active_path)
        warnings = validate_config(composed)
        if config_path is not None:
            # A new overlay path changes which local file is tracked, but the
            # active config remains the generated upstream+overlay composition.
            try:
                overlay_config_path = str(overlay_path.relative_to(root))
            except ValueError:
                overlay_config_path = str(overlay_path)
            updated = upstream.model_copy(update={"overlay_config_path": overlay_config_path})
            instance.set_upstream_metadata(updated)
        ensure_auth_managed_runtime_identity(instance)
        return ReloadConfigResult(
            config_path=str(instance.get_config_path()),
            updated=True,
            warnings=warnings,
        )

    if config_yaml is not None:
        # Non-upstream raw YAML replaces the instance's active config in place.
        # This is the daemon/server sync path for anonymous uploaded configs.
        validation = service_validate(config_yaml=config_yaml)
        target_path = instance.get_config_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        save_config(validation.config, target_path)
        ensure_auth_managed_runtime_identity(instance)
        return ReloadConfigResult(
            config_path=str(target_path),
            updated=True,
            warnings=validation.warnings,
        )

    if config_path is not None:
        # Non-upstream config_path reload repoints the instance to a caller-owned
        # file after validating the effective config. If the file uses extends,
        # composition is for validation only; the stored pointer remains the file.
        resolved = Path(config_path).expanduser().resolve()
        if not resolved.is_file():
            raise ConfigError(f"Config path '{resolved}' does not exist or is not a file")
        config = load_config(resolved)
        if config.extends is not None:
            config = compose_config_sequence(
                resolve_config_layers(config, config_path=resolved.resolve()),
            )
        warnings = validate_config(config)
        instance.set_config_path(str(resolved))
        ensure_auth_managed_runtime_identity(instance)
        return ReloadConfigResult(
            config_path=str(instance.get_config_path()),
            updated=True,
            warnings=warnings,
        )

    # No replacement was requested: validate whatever the instance currently
    # points at. Extend-based configs are composed in memory so validation sees
    # the effective surface.
    config = instance.load_config()
    if config.extends is not None:
        config_file = instance.get_config_path()
        if not isinstance(config_file, Path):
            config_file = Path(str(config_file))
        if not config_file.is_absolute():
            config_file = instance.get_root_path() / config_file
        config = compose_config_sequence(
            resolve_config_layers(config, config_path=config_file.resolve()),
        )
    warnings = validate_config(config)
    ensure_auth_managed_runtime_identity(instance)
    return ReloadConfigResult(
        config_path=str(instance.get_config_path()),
        updated=False,
        warnings=warnings,
    )
