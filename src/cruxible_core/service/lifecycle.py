"""Lifecycle service functions."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.config.composer import (
    compose_config_sequence,
    resolve_config_layers,
    write_runtime_composed_config,
)
from cruxible_core.config.loader import load_config, load_config_from_string, save_config
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.kits import materialize_kit
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service.types import (
    InitResult,
    ReloadConfigResult,
    ValidateServiceResult,
)


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
    kit: str | None = None,
    *,
    instance_mode: str = CruxibleInstance.DEV_MODE,
) -> InitResult:
    """Initialize a new cruxible instance (create-only).

    If the config uses ``extends``, the base is resolved and the composed
    config is written to ``{root_dir}/config.yaml``.  The instance then
    references the flattened file — future base/overlay edits require
    re-init or the full overlay flow.
    """
    root = Path(root_dir)
    normalized_kit = (kit or "").strip() or None
    sources = sum(value is not None for value in (config_path, config_yaml, normalized_kit))
    if sources != 1:
        raise ConfigError("Provide exactly one of config_path, config_yaml, or kit")

    if normalized_kit is not None:
        materialized_config = materialize_kit(
            kit=normalized_kit,
            root=root,
            expected_role="standalone",
        )
        config_path = str(materialized_config.relative_to(root))

    wrote_inline = False

    if config_yaml is not None:
        load_config_from_string(config_yaml)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"Failed to create directory {root}: {exc}") from exc
        disk_config = root / "config.yaml"
        if disk_config.exists():
            raise ConfigError(
                f"config.yaml already exists at {root}. "
                "Use config_path to reference the existing file, or remove it first."
            )
        try:
            disk_config.write_text(config_yaml)
        except OSError as exc:
            raise ConfigError(f"Failed to write config.yaml: {exc}") from exc
        config_path = "config.yaml"
        wrote_inline = True

    assert config_path is not None
    resolved = Path(config_path)
    if not resolved.is_absolute():
        resolved = root / resolved

    # Compose extends overlay before init so the instance gets a self-contained config.
    config = load_config(resolved)
    if config.extends is not None:
        try:
            root.mkdir(parents=True, exist_ok=True)
            composed_path = root / "config.yaml"
            composed = compose_config_sequence(
                resolve_config_layers(config, config_path=resolved),
            )
            save_config(composed, composed_path)
        except Exception:
            if wrote_inline:
                (root / "config.yaml").unlink(missing_ok=True)
            raise
        config_path = "config.yaml"

    try:
        instance = CruxibleInstance.init(
            root,
            config_path,
            data_dir,
            instance_mode=instance_mode,
        )
    except Exception:
        if wrote_inline or config.extends is not None:
            try:
                (root / "config.yaml").unlink(missing_ok=True)
            except Exception:
                pass
        raise

    loaded = instance.load_config()
    warnings = validate_config(loaded)

    return InitResult(instance=instance, warnings=warnings)


def service_reload_config(
    instance: InstanceProtocol,
    config_path: str | None = None,
    config_yaml: str | None = None,
) -> ReloadConfigResult:
    """Validate the active config or repoint the instance to a new config path."""
    if config_path is not None and config_yaml is not None:
        raise ConfigError("Provide config_path or config_yaml, not both")

    upstream = instance.get_upstream_metadata()
    if upstream is not None:
        root = instance.get_root_path()
        overlay_path = root / (config_path or upstream.overlay_config_path)
        if not overlay_path.is_absolute():
            overlay_path = root / overlay_path
        if config_yaml is None and not overlay_path.exists():
            raise ConfigError(f"Overlay config not found: {overlay_path}")

        base_path = root / upstream.config_path
        active_path = root / upstream.active_config_path
        if config_yaml is not None:
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
            composed = write_runtime_composed_config(
                base_path=base_path,
                overlay_path=overlay_path,
                output_path=active_path,
            )
        warnings = validate_config(composed)
        if config_path is not None:
            try:
                overlay_config_path = str(overlay_path.relative_to(root))
            except ValueError:
                overlay_config_path = str(overlay_path)
            updated = upstream.model_copy(
                update={"overlay_config_path": overlay_config_path}
            )
            instance.set_upstream_metadata(updated)
        instance.set_config_path(upstream.active_config_path)
        return ReloadConfigResult(
            config_path=str(instance.get_config_path()),
            updated=True,
            warnings=warnings,
        )

    if config_yaml is not None:
        validation = service_validate(config_yaml=config_yaml)
        target_path = instance.get_config_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        save_config(validation.config, target_path)
        return ReloadConfigResult(
            config_path=str(target_path),
            updated=True,
            warnings=validation.warnings,
        )

    if config_path is not None:
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
        return ReloadConfigResult(
            config_path=str(instance.get_config_path()),
            updated=True,
            warnings=warnings,
        )

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
    return ReloadConfigResult(
        config_path=str(instance.get_config_path()),
        updated=False,
        warnings=warnings,
    )
