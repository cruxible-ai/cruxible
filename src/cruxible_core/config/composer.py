"""Config composition helpers for current layered config shapes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cruxible_core.config.loader import load_config, save_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError

_SAFE_LIST_KEYS = {"constraints", "quality_checks", "tests", "decision_policies"}
_KEYED_MAP_KEYS = {
    "entity_types",
    "named_queries",
    "enums",
    "contracts",
    "artifacts",
    "providers",
    "workflows",
    "feedback_profiles",
    "outcome_profiles",
}

_INLINE_RELATIVE_EXTENDS_ERROR = (
    "Inline config_yaml with a relative extends path cannot be composed — use an "
    "absolute path or validate from a file"
)


@dataclass(frozen=True)
class ResolvedConfigLayer:
    """One config contribution with its resolved source path or directory."""

    config: CoreConfig
    config_path: Path | None = None
    config_dir: Path | None = None

    @property
    def source_dir(self) -> Path | None:
        """Return the directory used for relative artifact rebasing."""
        if self.config_dir is not None:
            return self.config_dir
        if self.config_path is not None:
            return self.config_path.parent
        return None


def resolve_config_layers(
    config: CoreConfig,
    *,
    config_path: Path | None = None,
    config_dir: Path | None = None,
) -> list[ResolvedConfigLayer]:
    """Lower today's supported config shapes into an ordered layer sequence."""
    if config_path is not None and config_dir is not None:
        raise ConfigError("Provide config_path or config_dir, not both")
    resolved_path = config_path.resolve() if config_path is not None else None
    resolved_dir = config_dir.resolve() if config_dir is not None else None
    source_dir = (
        resolved_dir
        if resolved_dir is not None
        else (resolved_path.parent if resolved_path is not None else None)
    )
    if config.extends is None:
        return [
            ResolvedConfigLayer(
                config=config,
                config_path=resolved_path,
                config_dir=resolved_dir,
            )
        ]

    base_path = Path(config.extends)
    if not base_path.is_absolute():
        if source_dir is None:
            raise ConfigError(_INLINE_RELATIVE_EXTENDS_ERROR)
        base_path = source_dir / base_path
    if not base_path.exists():
        raise ConfigError(f"Base config for extends not found: {base_path}")

    resolved_base_path = base_path.resolve()
    return [
        ResolvedConfigLayer(config=load_config(resolved_base_path), config_path=resolved_base_path),
        ResolvedConfigLayer(
            config=config,
            config_path=resolved_path,
            config_dir=resolved_dir,
        ),
    ]


def compose_config_sequence(
    layers: Sequence[ResolvedConfigLayer],
    *,
    runtime: bool = False,
    validate: bool = True,
) -> CoreConfig:
    """Compose an ordered config sequence using current append-only semantics.

    Composition creates a new effective config, so semantic validation runs by
    default before the composed model is returned. ``validate=False`` is reserved
    for tests/tools that need to inspect raw merge mechanics.
    """
    if not layers:
        raise ConfigError("Config composition requires at least one layer")

    composed_data: dict[str, Any] | None = None
    removed_provider_names: set[str] = set()
    last_index = len(layers) - 1

    for index, layer in enumerate(layers):
        layer_data = layer.config.model_dump(mode="python", by_alias=True, exclude_none=True)
        if runtime and index != last_index:
            layer_data, _removed_workflow_names, removed = _strip_canonical_runtime_config(
                layer_data
            )
            removed_provider_names.update(removed)
        source_dir = layer.source_dir
        if source_dir is not None:
            layer_data = _rebase_artifact_uris(layer_data, source_dir.resolve())
        if composed_data is None:
            composed_data = layer_data
        else:
            composed_data = _compose_mapping(composed_data, layer_data)

    assert composed_data is not None
    if runtime:
        composed_data = _strip_removed_runtime_providers(
            composed_data,
            removed_provider_names=removed_provider_names,
        )
    composed_data.pop("extends", None)
    composed = CoreConfig.model_validate(composed_data)
    if validate:
        from cruxible_core.config.validator import validate_config

        validate_config(composed)
    return composed


def compose_configs(
    base: CoreConfig,
    overlay: CoreConfig,
    *,
    base_config_path: Path | None = None,
    overlay_config_path: Path | None = None,
    validate: bool = True,
) -> CoreConfig:
    """Compose a base config and overlay using strict append-only semantics."""
    return compose_config_sequence(
        [
            ResolvedConfigLayer(config=base, config_path=base_config_path),
            ResolvedConfigLayer(config=overlay, config_path=overlay_config_path),
        ],
        validate=validate,
    )


def compose_runtime_configs(
    base: CoreConfig,
    overlay: CoreConfig,
    *,
    base_config_path: Path | None = None,
    overlay_config_path: Path | None = None,
    validate: bool = True,
) -> CoreConfig:
    """Compose a release-backed overlay runtime config.

    Upstream canonical workflows are removed from the base config before
    composition so downstream overlays do not attempt to rebuild upstream
    reference state or verify build-only artifacts.
    """
    return compose_config_sequence(
        [
            ResolvedConfigLayer(config=base, config_path=base_config_path),
            ResolvedConfigLayer(config=overlay, config_path=overlay_config_path),
        ],
        runtime=True,
        validate=validate,
    )


def write_composed_config(
    *,
    base_path: Path,
    overlay_path: Path,
    output_path: Path,
) -> CoreConfig:
    """Compose base+overlay configs and materialize the merged output on disk."""
    base = load_config(base_path)
    overlay = load_config(overlay_path)
    composed = compose_configs(
        base,
        overlay,
        base_config_path=base_path,
        overlay_config_path=overlay_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(composed, output_path)
    return composed


def write_runtime_composed_config(
    *,
    base_path: Path,
    overlay_path: Path,
    output_path: Path,
) -> CoreConfig:
    """Compose base+overlay configs for a release-backed overlay runtime."""
    base = load_config(base_path)
    overlay = load_config(overlay_path)
    composed = compose_runtime_configs(
        base,
        overlay,
        base_config_path=base_path,
        overlay_config_path=overlay_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(composed, output_path)
    return composed


def compose_config_files(
    *,
    base_path: Path,
    overlay_path: Path | None = None,
) -> CoreConfig:
    """Compose config files without writing the merged result to disk."""
    base = load_config(base_path)
    if overlay_path is None:
        return compose_config_sequence(resolve_config_layers(base, config_path=base_path))
    overlay = load_config(overlay_path)
    return compose_configs(
        base,
        overlay,
        base_config_path=base_path,
        overlay_config_path=overlay_path,
    )


def compose_runtime_config_files(
    *,
    base_path: Path,
    overlay_path: Path,
) -> CoreConfig:
    """Compose two config files for release-backed overlay runtime use."""
    base = load_config(base_path)
    overlay = load_config(overlay_path)
    return compose_runtime_configs(
        base,
        overlay,
        base_config_path=base_path,
        overlay_config_path=overlay_path,
    )


def _compose_mapping(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, overlay_value in overlay.items():
        if key == "extends":
            result[key] = overlay_value
            continue
        if key in {"name", "description"}:
            result[key] = overlay_value
            continue
        if key in _SAFE_LIST_KEYS:
            result[key] = list(base.get(key, [])) + list(overlay_value)
            continue
        if key == "relationships":
            result[key] = _compose_relationships(base.get(key, []), overlay_value)
            continue
        if key in _KEYED_MAP_KEYS:
            base_map = dict(base.get(key, {}))
            for child_key, child_value in overlay_value.items():
                if child_key in base_map:
                    raise ConfigError(
                        f"Overlay cannot redefine upstream '{key}' entry '{child_key}'"
                    )
                base_map[child_key] = child_value
            result[key] = base_map
            continue
        if key in result and result[key] != overlay_value:
            raise ConfigError(f"Overlay cannot override upstream field '{key}'")
        result[key] = overlay_value
    return result


def _compose_relationships(
    base_rels: list[dict[str, Any]],
    overlay_rels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = {rel["name"] for rel in base_rels}
    merged = list(base_rels)
    for rel in overlay_rels:
        if rel["name"] in names:
            raise ConfigError(f"Overlay cannot redefine upstream relationship '{rel['name']}'")
        merged.append(rel)
    return merged


def _strip_canonical_runtime_config(
    data: dict[str, Any],
) -> tuple[dict[str, Any], set[str], set[str]]:
    workflows = data.get("workflows")
    if not isinstance(workflows, dict):
        return data, set(), set()

    filtered_workflows: dict[str, Any] = {}
    removed_workflow_names: set[str] = set()
    removed_provider_names: set[str] = set()
    for workflow_name, workflow_value in workflows.items():
        if isinstance(workflow_value, dict) and workflow_value.get("type") == "canonical":
            removed_workflow_names.add(workflow_name)
            removed_provider_names.update(_workflow_provider_names(workflow_value))
            continue
        filtered_workflows[workflow_name] = workflow_value

    if not removed_workflow_names:
        return data, set(), set()

    stripped = dict(data)
    stripped["workflows"] = filtered_workflows

    tests = data.get("tests")
    if isinstance(tests, list):
        stripped["tests"] = [
            test_value
            for test_value in tests
            if not (
                isinstance(test_value, dict)
                and test_value.get("workflow") in removed_workflow_names
            )
        ]

    return stripped, removed_workflow_names, removed_provider_names


def _workflow_provider_names(workflow_value: dict[str, Any]) -> set[str]:
    steps = workflow_value.get("steps")
    if not isinstance(steps, list):
        return set()

    provider_names: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        provider_name = step.get("provider")
        if isinstance(provider_name, str) and provider_name:
            provider_names.add(provider_name)
    return provider_names


def _strip_removed_runtime_providers(
    data: dict[str, Any],
    *,
    removed_provider_names: set[str],
) -> dict[str, Any]:
    if not removed_provider_names:
        return data

    providers = data.get("providers")
    workflows = data.get("workflows")
    if not isinstance(providers, dict) or not isinstance(workflows, dict):
        return data

    used_provider_names: set[str] = set()
    for workflow_value in workflows.values():
        if isinstance(workflow_value, dict):
            used_provider_names.update(_workflow_provider_names(workflow_value))

    provider_names_to_remove = removed_provider_names - used_provider_names
    if not provider_names_to_remove:
        return data

    stripped = dict(data)
    stripped["providers"] = {
        provider_name: provider_value
        for provider_name, provider_value in providers.items()
        if provider_name not in provider_names_to_remove
    }
    return stripped


def _rebase_artifact_uris(data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict):
        return data

    rebased_artifacts: dict[str, Any] = {}
    changed = False
    for artifact_name, artifact_value in artifacts.items():
        if not isinstance(artifact_value, dict):
            rebased_artifacts[artifact_name] = artifact_value
            continue
        uri = artifact_value.get("uri")
        if not isinstance(uri, str):
            rebased_artifacts[artifact_name] = artifact_value
            continue
        rebased_uri = _rebase_artifact_uri(uri, config_dir)
        if rebased_uri == uri:
            rebased_artifacts[artifact_name] = artifact_value
            continue
        changed = True
        rebased_artifact = dict(artifact_value)
        rebased_artifact["uri"] = rebased_uri
        rebased_artifacts[artifact_name] = rebased_artifact

    if not changed:
        return data

    rebased = dict(data)
    rebased["artifacts"] = rebased_artifacts
    return rebased


def _rebase_artifact_uri(uri: str, config_dir: Path) -> str:
    parsed = urlparse(uri)
    if parsed.scheme not in {"", "file"}:
        return uri

    if parsed.scheme == "file":
        path = Path(parsed.path)
        if path.is_absolute():
            return uri
        return (config_dir / path).resolve().as_uri()

    path = Path(uri)
    if path.is_absolute():
        return uri
    return str((config_dir / path).resolve())
