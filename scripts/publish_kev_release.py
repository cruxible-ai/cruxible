"""Build and publish the KEV reference world model from fresh public data.

Usage:
    uv run python scripts/publish_kev_release.py --release-id 2026-03-27
    uv run python scripts/publish_kev_release.py \
      --transport-ref file:///tmp/kev-releases \
      --release-id 2026-03-27

This script is intentionally stateless. It fetches the KEV source data into a
temporary workspace, rewrites the demo config with the current artifact digest,
runs the canonical workflow, and publishes a single release bundle to both an
immutable release ref and the moving ``latest`` ref.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import httpx
import yaml

from cruxible_core.errors import ConfigError
from cruxible_core.kits import copy_kit_runtime_files, write_materialized_kit_metadata
from cruxible_core.service.execution import service_apply_workflow, service_lock, service_run
from cruxible_core.service.lifecycle import service_init
from cruxible_core.service.world import build_release_bundle
from cruxible_core.snapshot.types import PublishedWorldManifest
from cruxible_core.transport.backends import resolve_transport
from cruxible_core.transport.types import parse_transport_ref
from cruxible_core.workflow.compiler import compute_path_sha256

CISA_KEV_URL = (
    "https://www.cisa.gov/sites/default/files/csv/known_exploited_vulnerabilities.csv"
)
EPSS_KEV_URL = "https://raw.githubusercontent.com/jgamblin/KEV_EPSS/main/epss_kev_nvd.csv"
DEFAULT_TRANSPORT_REF = "oci://ghcr.io/cruxible-ai/models/kev-reference"
DEFAULT_WORLD_ID = "kev-reference"
DEFAULT_WORKFLOW_NAME = "build_public_kev_reference"
DEFAULT_COMPATIBILITY = "data_only"

MIN_KEV_ROWS = 1000
MIN_EPSS_ROWS = 1000
MIN_NVD_ENTRIES = 500


@dataclass(frozen=True)
class PublishRefs:
    immutable_ref: str
    latest_ref: str


@dataclass(frozen=True)
class PublishKevReleaseResult:
    manifest: PublishedWorldManifest
    immutable_ref: str
    latest_ref: str


def publish_kev_release(
    *,
    transport_ref: str,
    release_id: str,
    world_id: str = DEFAULT_WORLD_ID,
    workflow_name: str = DEFAULT_WORKFLOW_NAME,
    compatibility: str = DEFAULT_COMPATIBILITY,
    nvd_api_key: str | None = None,
) -> PublishKevReleaseResult:
    """Build and publish the KEV reference world release bundle."""
    refs = build_publish_refs(transport_ref=transport_ref, release_id=release_id)
    repo_root = _repo_root()

    with tempfile.TemporaryDirectory(prefix="kev_publish_") as temp_dir:
        workspace = Path(temp_dir)
        kit_root = repo_root / "kits" / "kev-reference"
        copy_kit_runtime_files(kit_root, workspace)

        data_dir = workspace / "data"
        if data_dir.exists():
            shutil.rmtree(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        populate_data_dir(data_dir, api_key=nvd_api_key)

        config_src = kit_root / "config.yaml"
        config_path = workspace / "config.yaml"
        artifact_sha256 = compute_path_sha256(data_dir)
        write_temp_kev_config(
            source_path=config_src,
            output_path=config_path,
            artifact_sha256=artifact_sha256,
        )
        write_materialized_kit_metadata(workspace)

        instance = service_init(workspace, config_path="config.yaml").instance
        service_lock(instance)
        preview = service_run(instance, workflow_name, {})
        if preview.apply_digest is None:
            raise ConfigError(
                f"Canonical workflow '{workflow_name}' did not produce an apply digest"
            )
        applied = service_apply_workflow(
            instance,
            workflow_name,
            {},
            expected_apply_digest=preview.apply_digest,
            expected_head_snapshot_id=preview.head_snapshot_id,
        )

        if applied.committed_snapshot_id is None:
            raise ConfigError(
                f"Canonical workflow '{workflow_name}' did not produce a committed snapshot"
            )

        bundle_dir = build_release_bundle(
            instance=instance,
            snapshot_id=applied.committed_snapshot_id,
            world_id=world_id,
            release_id=release_id,
            compatibility=compatibility,
            parent_release_id=None,
        )

        publish_release_bundle(bundle_dir, refs)

        manifest = PublishedWorldManifest.model_validate_json(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        return PublishKevReleaseResult(
            manifest=manifest,
            immutable_ref=refs.immutable_ref,
            latest_ref=refs.latest_ref,
        )


def build_publish_refs(*, transport_ref: str, release_id: str) -> PublishRefs:
    """Build immutable and latest publish refs from a base transport ref."""
    _validate_release_id(release_id)
    scheme, remainder = parse_transport_ref(transport_ref)
    if scheme == "oci":
        leaf = remainder.rsplit("/", 1)[-1]
        if ":" in leaf or "@" in leaf:
            raise ConfigError("OCI transport ref must not already include a tag or digest")
        return PublishRefs(
            immutable_ref=f"oci://{remainder}:{release_id}",
            latest_ref=f"oci://{remainder}:latest",
        )
    if scheme == "file":
        base_dir = Path(remainder)
        return PublishRefs(
            immutable_ref=f"file://{base_dir / release_id}",
            latest_ref=f"file://{base_dir / 'latest'}",
        )
    raise ConfigError(f"Unsupported transport scheme '{scheme}'")


def publish_release_bundle(bundle_dir: Path, refs: PublishRefs) -> None:
    """Publish one already-built bundle to immutable and latest refs."""
    immutable_transport, immutable_target = resolve_transport(refs.immutable_ref)
    immutable_transport.publish(immutable_target, bundle_dir)

    latest_transport, latest_target = resolve_transport(refs.latest_ref)
    _prepare_latest_target(refs.latest_ref)
    latest_transport.publish(latest_target, bundle_dir)


def populate_data_dir(data_dir: Path, *, api_key: str | None) -> None:
    """Fetch the three KEV source files into the temp artifact directory."""
    kev_text = download_text(CISA_KEV_URL)
    kev_row_count = kev_text.count("\n") - 1
    if kev_row_count < MIN_KEV_ROWS:
        raise ConfigError(
            f"KEV CSV has only {kev_row_count} rows (expected >={MIN_KEV_ROWS}). "
            "Source may be returning a maintenance page or truncated data."
        )
    (data_dir / "known_exploited_vulnerabilities.csv").write_text(
        kev_text, encoding="utf-8"
    )

    epss_text = download_text(EPSS_KEV_URL)
    epss_row_count = epss_text.count("\n") - 1
    if epss_row_count < MIN_EPSS_ROWS:
        raise ConfigError(
            f"EPSS CSV has only {epss_row_count} rows (expected >={MIN_EPSS_ROWS}). "
            "Source may be returning a maintenance page or truncated data."
        )
    (data_dir / "epss_kev_nvd.csv").write_text(epss_text, encoding="utf-8")

    nvd_rows = load_nvd_fetcher()(api_key)
    if len(nvd_rows) < MIN_NVD_ENTRIES:
        raise ConfigError(
            f"NVD fetch returned only {len(nvd_rows)} entries (expected >={MIN_NVD_ENTRIES}). "
            "NVD API may be returning partial data."
        )
    (data_dir / "nvd_kev_cves.json").write_text(
        json.dumps(nvd_rows, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def write_temp_kev_config(
    *,
    source_path: Path,
    output_path: Path,
    artifact_sha256: str,
) -> None:
    """Write a temp KEV config with the current data bundle digest."""
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected YAML mapping in {source_path}")
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, dict) or "public_kev_bundle" not in artifacts:
        raise ConfigError("KEV config is missing artifacts.public_kev_bundle")
    public_bundle = artifacts["public_kev_bundle"]
    if not isinstance(public_bundle, dict):
        raise ConfigError("artifacts.public_kev_bundle must be a mapping")
    public_bundle["sha256"] = artifact_sha256
    output_path.write_text(
        yaml.safe_dump(raw, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def download_text(url: str) -> str:
    """Fetch a text payload over HTTPS."""
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def load_nvd_fetcher() -> Callable[[str | None], list[dict[str, Any]]]:
    """Load the KEV kit NVD fetch helper from its file path."""
    repo_root = _repo_root()
    module = load_module_from_path(
        name="cruxible_kev_fetch_nvd",
        path=repo_root / "kits" / "kev-triage" / "scripts" / "fetch_nvd_kev.py",
    )
    fetcher = getattr(module, "fetch_all_kev_cves", None)
    if not callable(fetcher):
        raise ConfigError("fetch_nvd_kev.py does not define fetch_all_kev_cves()")
    return fetcher


def load_module_from_path(*, name: str, path: Path) -> ModuleType:
    """Import a module from an arbitrary file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_latest_target(ref: str) -> None:
    """Make file-backed latest refs replaceable while keeping immutable refs strict."""
    scheme, remainder = parse_transport_ref(ref)
    if scheme != "file":
        return
    latest_path = Path(remainder)
    if latest_path.is_dir():
        shutil.rmtree(latest_path)
    elif latest_path.exists():
        latest_path.unlink()


def _validate_release_id(value: str) -> None:
    PublishedWorldManifest(
        world_id="kev-reference",
        release_id=value,
        snapshot_id="snap_validation",
        compatibility=DEFAULT_COMPATIBILITY,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport-ref",
        default=DEFAULT_TRANSPORT_REF,
        help="Base transport ref, without an OCI tag; file:// refs publish into <base>/<tag>.",
    )
    parser.add_argument(
        "--release-id",
        required=True,
        help="Immutable release identifier, e.g. 2026-03-27",
    )
    parser.add_argument(
        "--world-id",
        default=DEFAULT_WORLD_ID,
        help=f"Published world_id (default: {DEFAULT_WORLD_ID})",
    )
    parser.add_argument(
        "--workflow-name",
        default=DEFAULT_WORKFLOW_NAME,
        help=f"Canonical workflow to run (default: {DEFAULT_WORKFLOW_NAME})",
    )
    parser.add_argument(
        "--compatibility",
        choices=("data_only", "additive_schema", "breaking"),
        default=DEFAULT_COMPATIBILITY,
        help=f"Release compatibility classification (default: {DEFAULT_COMPATIBILITY})",
    )
    parser.add_argument(
        "--nvd-api-key",
        default=None,
        help="Optional NVD API key. Defaults to NVD_API_KEY from the environment when omitted.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    nvd_api_key = args.nvd_api_key or os.environ.get("NVD_API_KEY")

    result = publish_kev_release(
        transport_ref=args.transport_ref,
        release_id=args.release_id,
        world_id=args.world_id,
        workflow_name=args.workflow_name,
        compatibility=args.compatibility,
        nvd_api_key=nvd_api_key,
    )
    print(f"Published {result.manifest.world_id}:{result.manifest.release_id}")
    print(f"Immutable ref: {result.immutable_ref}")
    print(f"Latest ref:    {result.latest_ref}")
    print(f"Snapshot:      {result.manifest.snapshot_id}")


if __name__ == "__main__":
    main()
