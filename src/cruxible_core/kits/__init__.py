"""Kit manifest, bundle cache, and provider path helpers."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Iterator
from urllib.parse import unquote

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from cruxible_core.errors import ConfigError

_logger = logging.getLogger(__name__)

KIT_MANIFEST_FILE = "cruxible-kit.yaml"
KIT_METADATA_FILE = "kit.json"
KIT_SCHEMA_VERSION = "cruxible.kit.v1"
LOCK_FILE_NAME = "cruxible.lock.yaml"
OCI_PIN_FILE = "oci-pins.json"
OCI_REPIN_ENV = "CRUXIBLE_OCI_REPIN"

_IGNORED_DIRS = {"__pycache__", ".cruxible", ".ruff_cache", ".pytest_cache"}
_IGNORED_FILES = {".DS_Store"}
_IGNORED_SUFFIXES = {".pyc"}
_SHIPPED_KIT_CATALOG: dict[str, str] = {
    "agent-operation": "oci://ghcr.io/cruxible-ai/kits/agent-operation:0.2.0",
    "case-law-monitoring": "oci://ghcr.io/cruxible-ai/kits/case-law-monitoring:0.2.0",
    "kev-reference": "oci://ghcr.io/cruxible-ai/kits/kev-reference:0.2.0",
    "kev-triage": "oci://ghcr.io/cruxible-ai/kits/kev-triage:0.2.0",
    "supply-chain-blast-radius": ("oci://ghcr.io/cruxible-ai/kits/supply-chain-blast-radius:0.2.0"),
}


class KitManifest(BaseModel):
    """Versioned kit bundle manifest."""

    schema_version: str = KIT_SCHEMA_VERSION
    kit_id: str
    version: str
    role: str
    target_state: str | None = None
    requires_base: str | None = None
    entry_config: str = "config.yaml"
    provider_paths: list[str] = Field(default_factory=list)
    copy_paths: list[str] = Field(default_factory=list)
    requires_extras: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_role(self) -> KitManifest:
        if self.schema_version != KIT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {KIT_SCHEMA_VERSION}")
        if self.role not in {"base", "standalone", "overlay"}:
            raise ValueError("role must be base, standalone, or overlay")
        if self.role == "overlay" and not self.target_state:
            raise ValueError("role: overlay requires target_state")
        if self.role in {"base", "standalone"} and self.target_state is not None:
            raise ValueError(f"role: {self.role} must not set target_state")
        if self.role == "base" and self.requires_base is not None:
            raise ValueError("role: base must not set requires_base")
        if self.requires_base is not None and not self.requires_base.strip():
            raise ValueError("requires_base must name a base kit")
        _validate_relative_path(self.entry_config, field_name="entry_config")
        for field_name, values in (
            ("provider_paths", self.provider_paths),
            ("copy_paths", self.copy_paths),
        ):
            for value in values:
                _validate_relative_path(value, field_name=field_name)
        return self


class KitBundle(BaseModel):
    """Resolved kit bundle root and manifest."""

    root: Path
    manifest: KitManifest
    digest: str


def get_kit_catalog() -> dict[str, str]:
    """Return built-in kit aliases mapped to file refs."""
    catalog = dict(_SHIPPED_KIT_CATALOG)
    catalog.update(_discover_local_kit_catalog())
    return catalog


def _discover_local_kit_catalog() -> dict[str, str]:
    """Return source-checkout kit aliases mapped to file refs."""
    repo_root = Path(__file__).resolve().parents[3]
    kits_dir = repo_root / "kits"
    catalog: dict[str, str] = {}
    if not kits_dir.exists():
        return catalog
    for manifest_path in sorted(kits_dir.glob("*/" + KIT_MANIFEST_FILE)):
        try:
            manifest = load_kit_manifest(manifest_path.parent)
        except ConfigError:
            continue
        catalog[manifest.kit_id] = f"file://{manifest_path.parent}"
    return catalog


def load_kit_manifest(root: Path) -> KitManifest:
    """Load and validate a kit manifest from a bundle root."""
    path = root / KIT_MANIFEST_FILE
    if not path.exists():
        raise ConfigError(f"Kit bundle is missing {KIT_MANIFEST_FILE}: {root}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read kit manifest at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Kit manifest at {path} must contain a YAML mapping")
    try:
        return KitManifest.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid kit manifest at {path}: {exc}") from exc


def resolve_kit_ref(kit: str) -> KitBundle:
    """Resolve a kit alias or transport ref into the local content-addressed cache."""
    normalized = kit.strip()
    if not normalized:
        raise ConfigError("Kit ref must not be empty")
    if "://" not in normalized:
        catalog = get_kit_catalog()
        resolved = catalog.get(normalized)
        if resolved is None or not resolved.startswith("file://"):
            # No local source checkout provides this alias; installed
            # distributions resolve it from digest-pinned release bundles.
            from cruxible_core.kit_distribution import published_kit_ids, resolve_published_kit

            if normalized in published_kit_ids():
                return _install_kit_cache(resolve_published_kit(normalized))
            if resolved is None:
                known = ", ".join(sorted(set(catalog) | published_kit_ids()))
                raise ConfigError(f"Unknown kit '{kit}'. Known kits: {known or '(none)'}")
        normalized = resolved

    if normalized.startswith("file://"):
        source = Path(unquote(normalized.removeprefix("file://"))).expanduser().resolve()
        if not source.exists():
            raise ConfigError(f"Kit file ref does not exist: {source}")
        return _install_kit_cache(source)
    if normalized.startswith("oci://"):
        pulled = _pull_oci_kit(normalized.removeprefix("oci://"))
        bundle = _install_kit_cache(pulled)
        _pin_oci_kit_digest(normalized, bundle.digest)
        return bundle
    raise ConfigError("Kit refs must be aliases, file:// refs, or oci:// refs")


def resolve_verified_kit_bundle(
    *,
    kit: str,
    expected_role: str,
    target_state: str | None = None,
) -> KitBundle:
    """Resolve a kit ref and verify it fully, before anything is written anywhere.

    Everything that can refuse a kit -- the ``oci://`` pin, the cache integrity
    check, the role and target-state checks, the presence of ``entry_config``,
    and the bundled lock's digest -- runs against the staged cache copy. A
    caller can therefore verify first and install second, so a refusal leaves
    the target root exactly as it found it instead of half-populated with a kit
    that turned out to be untrustworthy.
    """
    bundle = resolve_kit_ref(kit)
    manifest = bundle.manifest
    if manifest.role != expected_role:
        if expected_role == "standalone" and manifest.role == "overlay":
            raise ConfigError(
                f"Kit '{manifest.kit_id}' is an overlay kit. Use `cruxible state "
                "create-overlay --kit` instead of `cruxible init --kit`."
            )
        raise ConfigError(
            f"Kit '{manifest.kit_id}' has role '{manifest.role}', expected '{expected_role}'"
        )
    if (
        manifest.role == "overlay"
        and target_state is not None
        and manifest.target_state != target_state
    ):
        raise ConfigError(
            f"Kit '{manifest.kit_id}' targets state '{manifest.target_state}', not '{target_state}'"
        )
    if not (bundle.root / manifest.entry_config).exists():
        raise ConfigError(
            f"Kit '{manifest.kit_id}' is missing entry_config: {manifest.entry_config}"
        )
    _verify_bundled_lock(bundle.root)
    return bundle


def install_verified_kit_bundle(
    bundle: KitBundle,
    *,
    root: Path,
    upstream_config_path: str | None = None,
) -> Path:
    """Copy an already-verified kit bundle into an instance root.

    Writes only. Every refusal belongs to ``resolve_verified_kit_bundle``, which
    must have run first; this function assumes the bundle it is handed has
    already been checked.
    """
    manifest = bundle.manifest
    _copy_bundle_files(bundle.root, root)
    config_path = root / manifest.entry_config
    if manifest.role == "overlay" and upstream_config_path is not None:
        _rewrite_extends(config_path, upstream_config_path)
    write_materialized_kit_metadata(root, bundle_digest=bundle.digest)
    return config_path


def materialize_kit(
    *,
    kit: str,
    root: Path,
    expected_role: str,
    target_state: str | None = None,
    upstream_config_path: str | None = None,
) -> Path:
    """Verify a resolved kit bundle, then copy it into an instance root."""
    bundle = resolve_verified_kit_bundle(
        kit=kit,
        expected_role=expected_role,
        target_state=target_state,
    )
    return install_verified_kit_bundle(
        bundle,
        root=root,
        upstream_config_path=upstream_config_path,
    )


def copy_kit_runtime_files(
    source_root: Path,
    target_root: Path,
    *,
    include_entry_config: bool = True,
) -> None:
    """Copy kit-local provider and artifact paths next to an uploaded config."""
    manifest = load_kit_manifest(source_root)
    target_root.mkdir(parents=True, exist_ok=True)
    runtime_paths = [
        KIT_MANIFEST_FILE,
        LOCK_FILE_NAME,
        *manifest.provider_paths,
        *manifest.copy_paths,
    ]
    if include_entry_config:
        runtime_paths.insert(2, manifest.entry_config)
    for rel_path in runtime_paths:
        source = source_root / rel_path
        if not source.exists():
            continue
        _copy_path(source, target_root / rel_path)


def write_materialized_kit_metadata(root: Path, *, bundle_digest: str | None = None) -> None:
    """Write materialized kit metadata for an already-copied kit root."""
    manifest = load_kit_manifest(root)
    _write_materialized_metadata(
        root,
        KitBundle(
            root=root,
            manifest=manifest,
            digest=bundle_digest or compute_bundle_digest(root),
        ),
    )


def namespace_kit_provider_ref(ref: str, kit_id: str) -> str:
    """Rewrite a kit:// provider ref to its kit-scoped form for composed instances."""
    rel_path, attr = _parse_kit_provider_ref(ref)
    first_segment = rel_path.split("/", 1)[0]
    if first_segment == kit_id:
        return ref
    return f"kit://{kit_id}/{rel_path}::{attr}"


def _locate_kit_root_for_ref(rel_path: str, config_base_path: Path) -> tuple[Path, str]:
    """Resolve a kit:// ref path to its kit root and kit-relative provider path.

    Two layouts are supported: the kit-scoped form ``kit://<kit_id>/<path>``
    rooted at ``<instance_root>/kits/<kit_id>/`` (composed instances), and the
    un-namespaced form ``kit://<path>`` resolved by walking up to the nearest
    flat kit root (existing flat-layout instances). If a ref could resolve under
    both layouts the ambiguity is refused rather than guessed.
    """
    base = config_base_path.resolve()
    candidates = [base, *base.parents]
    first_segment, separator, remainder = rel_path.partition("/")

    namespaced_root: Path | None = None
    if separator and remainder:
        for candidate in candidates:
            scoped_root = candidate / "kits" / first_segment
            if (scoped_root / KIT_MANIFEST_FILE).exists():
                namespaced_root = scoped_root
                break

    flat_root: Path | None = None
    for candidate in candidates:
        if (candidate / KIT_MANIFEST_FILE).exists():
            flat_root = candidate
            break

    if namespaced_root is not None and flat_root is not None:
        raise ConfigError(
            f"kit:// provider ref path '{rel_path}' is ambiguous: it matches both the "
            f"kit-scoped root {namespaced_root} and the flat kit root {flat_root}"
        )
    if namespaced_root is not None:
        manifest = load_kit_manifest(namespaced_root)
        if manifest.kit_id != first_segment:
            raise ConfigError(
                f"Materialized kit at {namespaced_root} declares kit_id "
                f"'{manifest.kit_id}', not '{first_segment}'"
            )
        _validate_dev_tree_metadata(namespaced_root)
        return namespaced_root, remainder
    if flat_root is not None:
        _validate_dev_tree_metadata(flat_root)
        return flat_root, rel_path
    raise ConfigError("kit:// provider refs require a materialized kit root with cruxible-kit.yaml")


def resolve_kit_provider_ref(ref: str, config_base_path: Path) -> tuple[Path, str, Path]:
    """Resolve a kit:// provider ref to file path, attribute, and kit root."""
    rel_path, attr = _parse_kit_provider_ref(ref)
    kit_root, kit_rel_path = _locate_kit_root_for_ref(rel_path, config_base_path)
    manifest = load_kit_manifest(kit_root)
    path = _safe_join(kit_root, kit_rel_path)
    if path.suffix != ".py":
        raise ConfigError(f"kit:// provider ref '{ref}' must point to a .py file")
    _ensure_under_declared_provider_path(path, kit_root, manifest)
    if not path.exists():
        raise ConfigError(f"kit:// provider ref '{ref}' does not exist at {path}")
    return path, attr, kit_root


def load_kit_provider_module(path: Path, kit_root: Path) -> ModuleType:
    """Load a kit provider module under a digest-scoped synthetic package."""
    digest = compute_kit_runtime_digest(kit_root).removeprefix("sha256:")
    package_root = f"_cruxible_kit_{digest}"
    relative = path.relative_to(kit_root).with_suffix("")
    module_name = ".".join([package_root, *relative.parts])
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    _ensure_synthetic_packages(package_root, kit_root, relative.parent.parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"Could not load kit provider module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        raise ConfigError(
            f"Kit provider import failed for {path}: missing dependency '{exc.name}'. "
            "Install the dependency in the Cruxible Python environment, expose it "
            "through a Cruxible extra, or move the dependency behind a command/http provider."
        ) from exc
    except Exception as exc:
        raise ConfigError(f"Kit provider import failed for {path}: {exc}") from exc
    return module


def compute_bundle_digest(root: Path) -> str:
    """Hash all non-junk regular files in a bundle in sorted POSIX-relative order."""
    digest = hashlib.sha256()
    for path in _iter_bundle_files(root):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def compute_kit_runtime_digest(root: Path, manifest: KitManifest | None = None) -> str:
    """Hash only kit-owned runtime files in a materialized kit root."""
    root = root.resolve()
    resolved_manifest = manifest or load_kit_manifest(root)
    digest = hashlib.sha256()
    for path in _iter_kit_runtime_files(root, resolved_manifest):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def compute_kit_provider_sha256(ref: str, config_base_path: Path) -> str:
    """Hash all Python files under declared provider paths for a kit provider ref."""
    _path, _attr, kit_root = resolve_kit_provider_ref(ref, config_base_path)
    manifest = load_kit_manifest(kit_root)
    digest = hashlib.sha256()
    for provider_path in manifest.provider_paths:
        root = _safe_join(kit_root, provider_path)
        for path in sorted(root.rglob("*.py")):
            _reject_symlink(path)
            if not path.is_file():
                continue
            rel = path.relative_to(kit_root).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def is_kit_provider_ref(ref: str) -> bool:
    return ref.startswith("kit://")


def config_yaml_has_kit_provider_refs(config_yaml: str) -> bool:
    """Return whether uploaded config YAML declares any kit:// provider refs."""
    try:
        raw = yaml.safe_load(config_yaml)
    except yaml.YAMLError:
        return False
    if not isinstance(raw, dict):
        return False
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return False
    for provider in providers.values():
        if isinstance(provider, dict) and isinstance(provider.get("ref"), str):
            if is_kit_provider_ref(provider["ref"]):
                return True
    return False


def _install_kit_cache(source: Path) -> KitBundle:
    """Install a kit bundle into the content-addressed cache and re-verify it.

    The cache key is the bundle's content digest, so a cache hit *asserts* the
    contents. That assertion is never taken on faith: the directory is rehashed
    before it is reused, and the staged copy is rehashed before it is installed.
    A digest-keyed directory whose contents no longer hash to its key has been
    corrupted or poisoned -- anything materialized from it would carry a digest
    that describes bytes it no longer holds -- so it is refused, not repaired
    silently.
    """
    source = source.resolve()
    manifest = load_kit_manifest(source)
    digest = compute_bundle_digest(source)
    cache_dir = _kit_cache_dir()
    digest_key = digest.removeprefix("sha256:")
    target = cache_dir / digest_key
    lock_path = cache_dir / f"{digest_key}.lock"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with _file_lock(lock_path):
        if target.exists():
            _verify_cached_kit_dir(target, expected=digest)
        else:
            # Stage, hash, then install atomically -- the kit_distribution
            # pattern. Hashing the staged copy catches a truncated or altered
            # write before it is ever visible under the digest key.
            temp_target = Path(tempfile.mkdtemp(prefix=f"{digest_key}.", dir=cache_dir))
            try:
                _copy_bundle_files(source, temp_target)
                staged = compute_bundle_digest(temp_target)
                if staged != digest:
                    raise ConfigError(
                        f"Kit bundle from {source} hashed to {digest} at its source but "
                        f"{staged} after being staged into the cache. The copy did not "
                        "reproduce the bundle, so it is not installed. Retry; if it "
                        "recurs, the cache directory or the source tree is unstable."
                    )
                os.replace(temp_target, target)
            except Exception:
                shutil.rmtree(temp_target, ignore_errors=True)
                raise
    return KitBundle(root=target, manifest=manifest, digest=digest)


def _verify_cached_kit_dir(target: Path, *, expected: str) -> None:
    actual = compute_bundle_digest(target)
    if actual == expected:
        return
    raise ConfigError(
        f"Cached kit bundle at {target} is poisoned: the cache entry is keyed by "
        f"content digest {expected}, but its contents now hash to {actual}. Something "
        "edited the cache after installation, so the cached copy is refused rather "
        f"than reused. Delete {target} and re-run the command to re-fetch the kit from "
        f"its ref (or clear the whole cache directory {target.parent}); if the cache "
        "lives on shared or writable-by-others storage, move it by setting "
        "CRUXIBLE_KIT_CACHE_DIR."
    )


def _pull_oci_kit(ref: str) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="cruxible_kit_oci_"))
    try:
        subprocess.run(
            ["oras", "pull", ref, "-o", str(dest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise ConfigError("oras binary not found in PATH for oci:// kit refs") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigError(f"Timed out pulling oci:// kit ref '{ref}' after {exc.timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        raise ConfigError(f"Failed to pull oci:// kit ref '{ref}': {detail}") from exc
    return dest


def _oci_pin_path() -> Path:
    return _kit_cache_dir() / OCI_PIN_FILE


def _load_oci_pins() -> dict[str, str]:
    path = _oci_pin_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except ValueError as exc:
        raise ConfigError(
            f"OCI kit pin file at {path} is unreadable: {exc}. Delete it to re-pin "
            "every oci:// kit ref on its next resolution."
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            f"OCI kit pin file at {path} must contain a JSON object mapping refs to "
            "digests. Delete it to re-pin every oci:// kit ref on its next resolution."
        )
    return {str(key): str(value) for key, value in raw.items()}


def _pin_oci_kit_digest(ref: str, digest: str) -> None:
    """Pin an oci:// kit tag to the content it first resolved to, or verify it.

    An ``oci://`` tag is mutable at the registry: the same ref can be re-pushed
    to point at different bytes. The first resolution records the content digest
    of what the tag delivered; every later resolution must match it. A repointed
    tag is refused -- silently accepting new content under an unchanged ref is
    exactly the substitution a digest is supposed to catch.

    The re-pin escape hatch carries the digest the operator expects, not a bare
    ``1``. That makes it self-expiring: once the pin is moved, an environment
    still exporting the old value no longer authorizes anything, and a *second*
    tag repoint is refused rather than waved through by a variable someone left
    in a shell profile or a CI job definition. Overwriting an existing pin is
    also logged with both digests, so a re-pin is never a silent event.
    """
    pin_path = _oci_pin_path()
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = pin_path.with_suffix(".json.lock")
    with _file_lock(lock_path):
        pins = _load_oci_pins()
        recorded = pins.get(ref)
        if recorded == digest:
            return
        if recorded is not None:
            repin_authorization = os.environ.get(OCI_REPIN_ENV, "").strip()
            if repin_authorization != digest:
                detail = (
                    f" ({OCI_REPIN_ENV} is set to {repin_authorization!r}, which "
                    "authorizes a different digest than the one the registry served)"
                    if repin_authorization
                    else ""
                )
                raise ConfigError(
                    f"Kit ref '{ref}' is pinned to content digest {recorded}, but the "
                    f"registry now serves {digest} for that same tag. The mutable tag "
                    "was repointed at different content, so the pull is refused rather "
                    f"than silently accepted{detail}. If the new content is expected, "
                    "re-pin explicitly by naming the digest you are accepting -- "
                    f"{OCI_REPIN_ENV}={digest} -- and re-run the command, or pull the "
                    f"immutable digest ref directly; the pin file is {pin_path}."
                )
            _logger.warning(
                "Re-pinning oci kit ref %s: %s -> %s (authorized by %s). The previously "
                "pinned content is no longer what this ref resolves to.",
                ref,
                recorded,
                digest,
                OCI_REPIN_ENV,
            )
        pins[ref] = digest
        pin_path.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n")


def _kit_cache_dir() -> Path:
    configured = os.environ.get("CRUXIBLE_KIT_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "cruxible" / "kits"


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _copy_bundle_files(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for path in _iter_bundle_files(source):
        rel = path.relative_to(source)
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _copy_path(source: Path, target: Path) -> None:
    _reject_symlink(source)
    if source.is_dir():
        for path in _iter_bundle_files(source):
            rel = path.relative_to(source)
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _iter_bundle_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel_parts = path.relative_to(root).parts
        if any(part in _IGNORED_DIRS for part in rel_parts):
            continue
        if path.name in _IGNORED_FILES or path.suffix in _IGNORED_SUFFIXES:
            continue
        if path.is_symlink():
            raise ConfigError(f"Kit bundles must not contain symlinks: {path}")
        if path.is_file():
            files.append(path)
    return files


def _verify_bundled_lock(root: Path) -> None:
    """Verify the lock a kit bundle ships, not merely that it is present.

    A kit lock pins the kit's providers and artifacts by digest, and every
    downstream consumer treats it as the authority on what the kit executes.
    The lock records its own content digest, so a lock whose body no longer
    hashes to that digest was rewritten after it was generated -- pinning
    something other than what the kit author locked. That is refused here,
    before the kit is registered as materialized.
    """
    lock_path = root / LOCK_FILE_NAME
    if not lock_path.exists():
        raise ConfigError(
            f"Kit bundle is missing {LOCK_FILE_NAME}; run `cruxible lock` before publishing"
        )
    # Imported lazily: workflow.compiler imports this module for kit:// provider
    # digests, so a module-level import here would close the cycle.
    from cruxible_core.workflow.compiler import compute_lock_digest, load_lock

    try:
        lock = load_lock(lock_path)
    except ConfigError as exc:
        raise ConfigError(
            f"Kit bundle lock at {lock_path} is unreadable: {exc}. Re-run "
            "`cruxible lock --kit-dir <kit>` upstream and re-publish the kit."
        ) from exc
    expected = compute_lock_digest(lock)
    if lock.lock_digest is None:
        cache_hint = (
            f" This bundle is cached; deleting {lock_path.parent} re-fetches it from source."
            if _kit_cache_dir() in lock_path.parents
            else ""
        )
        raise ConfigError(
            f"Kit bundle lock at {lock_path} records no lock_digest, so its pinned "
            f"providers and artifacts cannot be verified (its contents hash to "
            f"{expected}). Re-run `cruxible lock --kit-dir <kit>` upstream and "
            f"re-publish the kit.{cache_hint}"
        )
    if lock.lock_digest != expected:
        raise ConfigError(
            f"Kit bundle lock at {lock_path} failed digest verification: the lock "
            f"records lock_digest={lock.lock_digest}, but its contents hash to "
            f"{expected}. The lock was edited after it was generated, so the kit is "
            "refused rather than materialized against unverified pins. Re-run "
            "`cruxible lock --kit-dir <kit>` upstream, re-publish the kit, and "
            "materialize again -- never hand-edit a lock file."
        )


def _write_materialized_metadata(root: Path, bundle: KitBundle) -> None:
    metadata_dir = root / ".cruxible"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "kit_id": bundle.manifest.kit_id,
        "version": bundle.manifest.version,
        "bundle_digest": bundle.digest,
        "runtime_digest": compute_kit_runtime_digest(root, bundle.manifest),
    }
    (metadata_dir / KIT_METADATA_FILE).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def _validate_dev_tree_metadata(root: Path) -> None:
    metadata_path = root / ".cruxible" / KIT_METADATA_FILE
    if not metadata_path.exists():
        if os.environ.get("CRUXIBLE_KIT_DEV_RESOLVE") == "1":
            return
        raise ConfigError(
            "kit:// provider refs found a dev-tree kit root without .cruxible/kit.json. "
            "Materialize the kit first, or set CRUXIBLE_KIT_DEV_RESOLVE=1 for local "
            "non-agent development."
        )
    try:
        metadata = json.loads(metadata_path.read_text())
    except ValueError as exc:
        raise ConfigError(f"Invalid kit metadata at {metadata_path}: {exc}") from exc
    manifest = load_kit_manifest(root)
    if metadata.get("kit_id") != manifest.kit_id or metadata.get("version") != manifest.version:
        raise ConfigError("Materialized kit metadata does not match cruxible-kit.yaml")
    recorded_digest = metadata.get("runtime_digest") or metadata.get("digest")
    if recorded_digest:
        current_digest = compute_kit_runtime_digest(root, manifest)
        if recorded_digest != current_digest:
            if os.environ.get("CRUXIBLE_KIT_DEV_RESOLVE") != "1":
                raise ConfigError(
                    "Materialized kit contents changed since installation. "
                    "Re-materialize the kit or set CRUXIBLE_KIT_DEV_RESOLVE=1 "
                    "for local development."
                )


def _iter_kit_runtime_files(root: Path, manifest: KitManifest) -> list[Path]:
    root = root.resolve()
    files: dict[str, Path] = {}
    runtime_paths = [
        KIT_MANIFEST_FILE,
        LOCK_FILE_NAME,
        manifest.entry_config,
        *manifest.provider_paths,
        *manifest.copy_paths,
    ]
    for rel_path in runtime_paths:
        path = _safe_join(root, rel_path)
        if not path.exists():
            continue
        if path.is_dir():
            for child in _iter_bundle_files(path):
                files[child.relative_to(root).as_posix()] = child
        elif path.is_file():
            files[path.relative_to(root).as_posix()] = path
    return [files[key] for key in sorted(files)]


def _parse_kit_provider_ref(ref: str) -> tuple[str, str]:
    if not ref.startswith("kit://"):
        raise ConfigError(f"Provider ref '{ref}' is not a kit:// ref")
    target = ref.removeprefix("kit://")
    path_part, sep, attr = target.partition("::")
    if not sep or not path_part or not attr:
        raise ConfigError(f"Invalid kit provider ref '{ref}'. Use kit://relative/path.py::callable")
    try:
        _validate_relative_path(path_part, field_name="provider ref")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return path_part, attr


def _validate_relative_path(value: str, *, field_name: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{field_name} must be a relative path without '..'")


def _safe_join(root: Path, rel_path: str) -> Path:
    try:
        _validate_relative_path(rel_path, field_name="kit path")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    root_resolved = root.resolve()
    candidate = root_resolved / Path(rel_path)
    _reject_symlink(candidate)
    path = candidate.resolve()
    if root_resolved not in [path, *path.parents]:
        raise ConfigError(f"Kit path escapes bundle root: {rel_path}")
    _reject_symlink(path)
    return path


def _reject_symlink(path: Path) -> None:
    for candidate in [path, *path.parents]:
        if candidate.is_symlink():
            raise ConfigError(f"Kit path must not traverse symlinks: {candidate}")


def _ensure_under_declared_provider_path(path: Path, kit_root: Path, manifest: KitManifest) -> None:
    for provider_path in manifest.provider_paths:
        root = _safe_join(kit_root, provider_path)
        if root in [path, *path.parents]:
            return
    raise ConfigError(
        f"kit:// provider path {path.relative_to(kit_root).as_posix()} is not under "
        f"declared provider_paths for kit '{manifest.kit_id}'"
    )


def _ensure_synthetic_packages(package_root: str, kit_root: Path, parts: tuple[str, ...]) -> None:
    root_module = sys.modules.get(package_root)
    if root_module is None:
        root_module = ModuleType(package_root)
        root_module.__path__ = [str(kit_root)]
        sys.modules[package_root] = root_module
    current_path = kit_root
    module_name = package_root
    for part in parts:
        current_path = current_path / part
        module_name = f"{module_name}.{part}"
        if module_name in sys.modules:
            continue
        module = ModuleType(module_name)
        module.__path__ = [str(current_path)]
        sys.modules[module_name] = module


def _rewrite_extends(config_path: Path, upstream_config_path: str) -> None:
    raw = config_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    updated: list[str] = []
    replaced = 0
    for line in lines:
        if line.startswith("extends:"):
            updated.append(f"extends: {upstream_config_path}")
            replaced += 1
        else:
            updated.append(line)
    if replaced > 1:
        raise ConfigError(
            f"Overlay kit config '{config_path}' has multiple top-level extends: entries"
        )
    if replaced == 0:
        updated.insert(0, f"extends: {upstream_config_path}")
    config_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
