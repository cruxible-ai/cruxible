"""Instance config source pointer: schema, digests, and compose-at-load.

An instance never stores an editable config. It stores ``config-source.yaml``
in the instance directory — an ordered list of layers (pinned kit refs plus at
most one instance-delta fragment) — and the runtime composes the layers at
load. Any flattened form is a derived, digest-verified artifact, never an
input: there is no daemon-side config file to edit
(dd-config-by-reference-one-source).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from cruxible_core.config.composer import ResolvedConfigLayer, compose_config_sequence
from cruxible_core.config.loader import load_config, load_config_from_string
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError

if TYPE_CHECKING:
    from cruxible_core.kits import KitBundle

CONFIG_SOURCE_FILE_NAME = "config-source.yaml"
CONFIG_SOURCE_VERSION: Literal["1"] = "1"

_INSTANCE_KITS_DIR = "kits"


class KitSourceLayer(BaseModel):
    """One pinned kit layer, resolved exactly like ``init --kit`` resolves refs."""

    kind: Literal["kit"] = "kit"
    ref: str

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_ref(self) -> KitSourceLayer:
        if not self.ref.strip():
            raise ValueError("kit layer ref must not be empty")
        return self


class FragmentSourceLayer(BaseModel):
    """The optional instance-delta overlay layer (at most one, last)."""

    kind: Literal["fragment"] = "fragment"
    path: str

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_path(self) -> FragmentSourceLayer:
        if not self.path.strip():
            raise ValueError("fragment layer path must not be empty")
        return self


ConfigSourceLayer = Annotated[
    KitSourceLayer | FragmentSourceLayer,
    Field(discriminator="kind"),
]


class ConfigSourcePointer(BaseModel):
    """Typed contents of an instance's ``config-source.yaml``."""

    version: Literal["1"] = CONFIG_SOURCE_VERSION
    layers: list[ConfigSourceLayer]

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_layers(self) -> ConfigSourcePointer:
        if not self.layers:
            raise ValueError("config source must declare at least one layer")
        if not isinstance(self.layers[0], KitSourceLayer):
            raise ValueError("the first config source layer must be a kit layer")
        fragment_indexes = [
            index
            for index, layer in enumerate(self.layers)
            if isinstance(layer, FragmentSourceLayer)
        ]
        if len(fragment_indexes) > 1:
            raise ValueError("config source allows at most one fragment layer")
        if fragment_indexes and fragment_indexes[0] != len(self.layers) - 1:
            raise ValueError("the fragment layer must be the last config source layer")
        return self

    @property
    def kit_layers(self) -> list[KitSourceLayer]:
        return [layer for layer in self.layers if isinstance(layer, KitSourceLayer)]

    @property
    def fragment_layer(self) -> FragmentSourceLayer | None:
        last = self.layers[-1]
        return last if isinstance(last, FragmentSourceLayer) else None


@dataclass(frozen=True)
class ResolvedSourceLayer:
    """One resolved pointer layer with its content digest, for receipts."""

    kind: Literal["kit", "fragment"]
    ref: str
    digest: str
    kit_id: str | None = None


@dataclass(frozen=True)
class ComposedConfigSource:
    """A source pointer resolved and composed into the serving config."""

    pointer: ConfigSourcePointer
    pointer_digest: str
    config: CoreConfig
    composed_digest: str
    layers: tuple[ResolvedSourceLayer, ...]


def load_config_source(path: str | Path) -> ConfigSourcePointer:
    """Load and validate a ``config-source.yaml`` pointer file."""
    pointer_path = Path(path)
    if not pointer_path.exists():
        raise ConfigError(f"Config source pointer not found: {pointer_path}")
    try:
        raw = yaml.safe_load(pointer_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Failed to read config source pointer: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in config source pointer: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config source pointer at {pointer_path} must be a YAML mapping")
    try:
        return ConfigSourcePointer.model_validate(raw)
    except ValidationError as exc:
        errors = [
            f"{' → '.join(str(part) for part in err['loc'])}: {err['msg']}"
            if err.get("loc")
            else err["msg"]
            for err in exc.errors()
        ]
        raise ConfigError(
            f"Invalid config source pointer at {pointer_path}",
            errors=errors,
        ) from exc


def save_config_source(pointer: ConfigSourcePointer, path: str | Path) -> None:
    """Serialize a config source pointer to YAML and write it atomically."""
    target = Path(path)
    data = pointer.model_dump(mode="python", exclude_none=True)
    yaml_str = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = tempfile.NamedTemporaryFile(
            mode="w",
            dir=target.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        )
        tmp_path = Path(fd.name)
        try:
            with fd:
                fd.write(yaml_str)
                fd.flush()
            tmp_path.replace(target)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ConfigError(f"Failed to write config source pointer: {exc}") from exc


def compute_config_source_digest(pointer: ConfigSourcePointer) -> str:
    """Compute a stable digest over the pointer contents (recorded in receipts)."""
    dumped = json.dumps(
        pointer.model_dump(mode="python", exclude_none=True),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(dumped.encode()).hexdigest()}"


def validate_kit_layer_sequence(kit_refs: Sequence[str], bundles: Sequence[KitBundle]) -> None:
    """Validate a composed kit sequence: standalone base, overlays on earlier kits."""
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
    for _kit_ref, bundle in zip(kit_refs[1:], bundles[1:]):
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


def compose_config_source(
    pointer: ConfigSourcePointer,
    *,
    instance_root: str | Path,
) -> ComposedConfigSource:
    """Resolve a source pointer and compose its layers into the serving config.

    Kit refs resolve through the existing init-time kit resolution
    (``resolve_kit_ref``); no new resolution machinery. When the instance root
    holds a materialized copy of a kit whose content matches the resolved
    bundle digest, the materialized copy is used as that layer's source dir so
    composed artifact URIs stay instance-local (and byte-compatible with the
    pre-pointer flattened composition); on content drift the freshly resolved
    bundle is authoritative — that is how ``config refresh`` picks up updates.

    The optional fragment layer must satisfy the same allowed-roots
    containment used for artifact paths; a fragment escaping containment is a
    load ERROR, never a warning.
    """
    # Imported lazily: kits and the lock digest live above the config package
    # in the import graph, mirroring the loader/composer lazy-import pattern.
    from cruxible_core.kits import (
        KIT_MANIFEST_FILE,
        compute_bundle_digest,
        is_kit_provider_ref,
        namespace_kit_provider_ref,
        resolve_kit_ref,
    )
    from cruxible_core.workflow.compiler import compute_lock_config_digest

    root = Path(instance_root).resolve()
    kit_layers = pointer.kit_layers
    bundles = [resolve_kit_ref(layer.ref) for layer in kit_layers]
    validate_kit_layer_sequence([layer.ref for layer in kit_layers], bundles)

    resolved_layers: list[ResolvedConfigLayer] = []
    layer_records: list[ResolvedSourceLayer] = []
    for layer, bundle in zip(kit_layers, bundles):
        kit_id = bundle.manifest.kit_id
        layer_root = bundle.root
        materialized = root / _INSTANCE_KITS_DIR / kit_id
        if (materialized / KIT_MANIFEST_FILE).exists() and compute_bundle_digest(
            materialized
        ) == bundle.digest:
            layer_root = materialized
        entry_config = layer_root / bundle.manifest.entry_config
        if not entry_config.is_file():
            raise ConfigError(
                f"Kit '{kit_id}' is missing entry_config: {bundle.manifest.entry_config}"
            )
        layer_config = load_config(entry_config)
        for provider in layer_config.providers.values():
            if is_kit_provider_ref(provider.ref):
                provider.ref = namespace_kit_provider_ref(provider.ref, kit_id)
        resolved_layers.append(ResolvedConfigLayer(config=layer_config, config_path=entry_config))
        layer_records.append(
            ResolvedSourceLayer(kind="kit", ref=layer.ref, digest=bundle.digest, kit_id=kit_id)
        )

    fragment = pointer.fragment_layer
    if fragment is not None:
        fragment_path = _resolve_fragment_path(fragment.path, instance_root=root)
        try:
            fragment_bytes = fragment_path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"Failed to read config fragment: {exc}") from exc
        fragment_config = load_config_from_string(
            fragment_bytes.decode("utf-8"),
            partial_layer=True,
        )
        resolved_layers.append(
            ResolvedConfigLayer(config=fragment_config, config_path=fragment_path)
        )
        layer_records.append(
            ResolvedSourceLayer(
                kind="fragment",
                ref=fragment.path,
                digest=f"sha256:{hashlib.sha256(fragment_bytes).hexdigest()}",
            )
        )

    config = compose_config_sequence(resolved_layers)
    return ComposedConfigSource(
        pointer=pointer,
        pointer_digest=compute_config_source_digest(pointer),
        config=config,
        composed_digest=compute_lock_config_digest(config),
        layers=tuple(layer_records),
    )


def _resolve_fragment_path(path: str, *, instance_root: Path) -> Path:
    """Resolve a fragment path under the allowed-roots containment for artifacts."""
    # Imported lazily to reuse the ONE artifact-path containment implementation
    # without a top-level config -> service dependency.
    from cruxible_core.service.source_artifacts import resolve_contained_source_path

    try:
        resolved = resolve_contained_source_path(
            path,
            allowed_source_roots=[instance_root],
        )
    except ConfigError as exc:
        raise ConfigError(f"Config source fragment path escapes the allowed roots: {path}") from exc
    if not resolved.is_file():
        raise ConfigError(f"Config source fragment not found: {resolved}")
    return resolved
