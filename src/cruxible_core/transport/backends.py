"""File and OCI transport backends for published state bundles."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from cruxible_core.errors import ConfigError, TransportError
from cruxible_core.snapshot.types import PublishedStateManifest, StateSnapshot
from cruxible_core.transport.types import PulledReleaseBundle, ReleaseTransport


def _load_bundle(root_dir: Path) -> PulledReleaseBundle:
    manifest_path = root_dir / "manifest.json"
    snapshot_path = root_dir / "snapshot.json"
    if not manifest_path.exists():
        raise TransportError(f"Bundle missing manifest.json at {root_dir}")
    if not snapshot_path.exists():
        raise TransportError(f"Bundle missing snapshot.json at {root_dir}")
    manifest = PublishedStateManifest.model_validate_json(manifest_path.read_text())
    snapshot = StateSnapshot.model_validate_json(snapshot_path.read_text())
    return PulledReleaseBundle(root_dir=root_dir, manifest=manifest, snapshot=snapshot)


class FileReleaseTransport:
    """Simple file-backed release transport for tests and offline use."""

    def publish(self, ref: str, bundle_dir: Path) -> str:
        target = Path(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise TransportError(f"File transport target already exists: {target}")
        shutil.copytree(bundle_dir, target)
        return str(target)

    def pull(self, ref: str, dest_dir: Path) -> PulledReleaseBundle:
        source = Path(ref)
        if not source.exists():
            raise TransportError(f"File transport source not found: {source}")
        shutil.copytree(source, dest_dir, dirs_exist_ok=True)
        return _load_bundle(dest_dir)


class OciReleaseTransport:
    """OCI transport backed by the external oras CLI."""

    def _run(self, args: list[str], *, cwd: Path | None = None) -> None:
        try:
            subprocess.run(
                args,
                check=True,
                capture_output=True,
                text=True,
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError as exc:
            raise TransportError("oras binary not found in PATH") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else str(exc)
            raise TransportError(f"oras command failed: {stderr}") from exc

    def publish(self, ref: str, bundle_dir: Path) -> str:
        files = [
            "manifest.json:application/vnd.cruxible.manifest.v1+json",
            "snapshot.json:application/json",
            "config.yaml:text/yaml",
            "graph.json:application/json",
        ]
        if (bundle_dir / "cruxible.lock.yaml").exists():
            files.append("cruxible.lock.yaml:text/yaml")
        args = ["oras", "push", ref]
        args.extend(files)
        self._run(args=args, cwd=bundle_dir)
        return ref

    def pull(self, ref: str, dest_dir: Path) -> PulledReleaseBundle:
        dest_dir.mkdir(parents=True, exist_ok=True)
        self._run(["oras", "pull", ref, "-o", str(dest_dir)])
        return _load_bundle(dest_dir)


def resolve_transport(ref: str) -> tuple[ReleaseTransport, str]:
    """Resolve a transport implementation from a scheme-qualified ref."""
    from cruxible_core.transport.types import parse_transport_ref

    scheme, remainder = parse_transport_ref(ref)
    if scheme == "file":
        return FileReleaseTransport(), remainder
    if scheme == "oci":
        return OciReleaseTransport(), remainder
    raise ConfigError(f"Unsupported transport scheme '{scheme}'")
