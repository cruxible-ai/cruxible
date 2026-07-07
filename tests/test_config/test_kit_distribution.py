"""Tests for fetch-by-digest kit distribution: bundling, manifest, resolver.

Covers the deterministic bundle builder (``scripts/build_kit_bundles.py``), the
committed packaged manifest (drift-pinned against ``kits/`` exactly like the
lockfile CI check), and ``resolve_published_kit`` end to end against a local
HTTP fixture: happy path, both digest refusals, unsafe tar members, the size
cap, cache hits skipping the network, local source checkouts winning over
fetch, and ``init`` composing a fetched overlay over its fetched base with no
local kits anywhere.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import socket
import sys
import tarfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

import cruxible_core.kit_distribution as kit_distribution
from cruxible_core import __version__
from cruxible_core.config.composer import resolve_overlay_kit_base_layer
from cruxible_core.errors import ConfigError
from cruxible_core.kit_distribution import (
    PublishedKitEntry,
    PublishedKitManifest,
    resolve_published_kit,
)
from cruxible_core.kits import resolve_kit_ref
from cruxible_core.service import service_init
from cruxible_core.workflow.compiler import compute_path_sha256

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGED_MANIFEST_PATH = (
    _REPO_ROOT / "src" / "cruxible_core" / "kit_distribution" / "manifest.json"
)
_DEMO_KIT_ID = "demo-pub"


class _BundleServer:
    """Local HTTP file server recording every request path it serves."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                name = self.path.lstrip("/")
                owner.requests.append(name)
                data = owner.files.get(name)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def bundle_server() -> Iterator[_BundleServer]:
    server = _BundleServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def fetch_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle_server: _BundleServer,
) -> _BundleServer:
    """Simulate an installed distribution: no source checkout, empty cache."""
    monkeypatch.setattr("cruxible_core.kits._discover_local_kit_catalog", lambda: {})
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "kit-cache"))
    monkeypatch.setenv(kit_distribution.MANIFEST_URL_BASE_ENV, bundle_server.base_url)
    return bundle_server


def _load_bundle_script() -> ModuleType:
    path = _REPO_ROOT / "scripts" / "build_kit_bundles.py"
    spec = importlib.util.spec_from_file_location("build_kit_bundles", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_demo_kit(root: Path, kit_id: str = _DEMO_KIT_ID) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("cruxible-kit.yaml").write_text(
        "schema_version: cruxible.kit.v1\n"
        f"kit_id: {kit_id}\n"
        "version: 0.2.0\n"
        "role: standalone\n"
        "entry_config: config.yaml\n"
        "provider_paths: []\n"
        "copy_paths: []\n"
        "requires_extras: []\n"
    )
    root.joinpath("config.yaml").write_text(
        "version: '1.0'\nname: demo\nentity_types: {}\nrelationships: []\n"
    )
    root.joinpath("cruxible.lock.yaml").write_text(
        "version: '1'\nconfig_digest: test\nartifacts: {}\nproviders: {}\n"
    )


def _publish_demo_kit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _BundleServer,
    *,
    tarball: bytes | None = None,
    dir_digest: str | None = None,
) -> Path:
    """Build + serve the demo kit, monkeypatch the packaged manifest; return its source dir."""
    script = _load_bundle_script()
    source = tmp_path / "demo-source" / _DEMO_KIT_ID
    _write_demo_kit(source)
    built = script.build_kit_bundle(source, tmp_path / "assets", "0.2.0")
    served = tarball if tarball is not None else built.path.read_bytes()
    server.files[built.asset] = served
    manifest = PublishedKitManifest(
        version="0.2.0",
        base_url="https://release.invalid/",
        kits={
            _DEMO_KIT_ID: PublishedKitEntry(
                asset=built.asset,
                tarball_sha256=hashlib.sha256(served).hexdigest()
                if tarball is not None
                else built.tarball_sha256,
                dir_digest=dir_digest if dir_digest is not None else built.dir_digest,
            )
        },
    )
    monkeypatch.setattr(kit_distribution, "load_published_manifest", lambda: manifest)
    return source


def _published_cache_entries(tmp_path: Path) -> list[Path]:
    cache_root = tmp_path / "kit-cache" / "published"
    if not cache_root.exists():
        return []
    return [path for path in cache_root.iterdir() if path.is_dir()]


def _tarball(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    payload = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=payload, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return payload.getvalue()


def _file_member(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    return info, data


# --- deterministic bundling and the packaged manifest ---


def test_build_kit_bundle_is_deterministic(tmp_path: Path) -> None:
    script = _load_bundle_script()
    source = tmp_path / "kit" / _DEMO_KIT_ID
    _write_demo_kit(source)

    first = script.build_kit_bundle(source, tmp_path / "out1", "0.2.0")
    # Filesystem metadata must not leak into the archive.
    for child in source.rglob("*"):
        child.chmod(0o755)
    second = script.build_kit_bundle(source, tmp_path / "out2", "0.2.0")

    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.tarball_sha256 == second.tarball_sha256
    assert first.dir_digest == second.dir_digest
    assert first.asset == f"{_DEMO_KIT_ID}-0.2.0.tar.gz"


def test_bundle_extraction_round_trips_dir_digest(tmp_path: Path) -> None:
    # The manifest's dir_digest is compute_path_sha256 (the lock digest
    # discipline) over exactly what the tarball extracts to.
    script = _load_bundle_script()
    source = tmp_path / "kit" / _DEMO_KIT_ID
    _write_demo_kit(source)
    built = script.build_kit_bundle(source, tmp_path / "out", "0.2.0")

    extracted = tmp_path / "extracted"
    with tarfile.open(built.path, mode="r:gz") as tar:
        tar.extractall(extracted, filter="data")
    assert compute_path_sha256(extracted) == built.dir_digest
    assert compute_path_sha256(source) == built.dir_digest


def test_packaged_manifest_pins_repo_kits() -> None:
    # Drift guard, same discipline as the kit lockfile CI check: the committed
    # packaged manifest must match a fresh digest of every kits/<id> directory.
    regen_hint = "regenerate with: uv run python scripts/build_kit_bundles.py"
    assert _PACKAGED_MANIFEST_PATH.exists(), f"packaged manifest missing; {regen_hint}"
    script = _load_bundle_script()
    packaged = json.loads(_PACKAGED_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert packaged["version"] == __version__, f"manifest version drifted; {regen_hint}"
    assert packaged["base_url"] == script.GITHUB_BASE_URL_TEMPLATE.format(version=__version__)

    kit_dirs = script.iter_kit_dirs(_REPO_ROOT / "kits")
    assert sorted(packaged["kits"]) == [kit_dir.name for kit_dir in kit_dirs], (
        f"manifest kit set drifted from kits/; {regen_hint}"
    )
    for kit_dir in kit_dirs:
        entry = packaged["kits"][kit_dir.name]
        assert entry["asset"] == f"{kit_dir.name}-{__version__}.tar.gz"
        assert entry["tarball_sha256"], f"{kit_dir.name}: empty tarball_sha256; {regen_hint}"
        assert entry["dir_digest"] == script.compute_kit_dir_digest(kit_dir), (
            f"kits/{kit_dir.name} changed without a manifest regen; {regen_hint}"
        )


def test_packaged_manifest_tarball_pin_is_reproducible(tmp_path: Path) -> None:
    # Rebuilding a repo kit's bundle from scratch must reproduce the committed
    # tarball sha256 byte for byte (deterministic archive discipline).
    script = _load_bundle_script()
    packaged = json.loads(_PACKAGED_MANIFEST_PATH.read_text(encoding="utf-8"))
    built = script.build_kit_bundle(
        _REPO_ROOT / "kits" / "agent-operation", tmp_path / "out", __version__
    )
    assert built.tarball_sha256 == packaged["kits"]["agent-operation"]["tarball_sha256"]


# --- resolver: happy path, refusals, caching, precedence ---


def test_resolve_published_kit_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    source = _publish_demo_kit(tmp_path, monkeypatch, fetch_env)

    resolved = resolve_published_kit(_DEMO_KIT_ID)

    assert resolved.is_dir()
    assert resolved.parent == tmp_path / "kit-cache" / "published"
    assert compute_path_sha256(resolved) == compute_path_sha256(source)
    assert fetch_env.requests == [f"{_DEMO_KIT_ID}-0.2.0.tar.gz"]

    # The alias resolver reaches the same bundle and installs it like any
    # local kit dir.
    bundle = resolve_kit_ref(_DEMO_KIT_ID)
    assert bundle.manifest.kit_id == _DEMO_KIT_ID
    assert (bundle.root / "cruxible.lock.yaml").exists()


def test_cache_hit_skips_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env)

    first = resolve_published_kit(_DEMO_KIT_ID)
    assert len(fetch_env.requests) == 1
    second = resolve_published_kit(_DEMO_KIT_ID)

    assert second == first
    assert len(fetch_env.requests) == 1


def test_local_source_checkout_wins_over_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env)
    local = tmp_path / "local-checkout" / _DEMO_KIT_ID
    _write_demo_kit(local)
    local.joinpath("LOCAL_MARKER").write_text("local\n")
    monkeypatch.setattr(
        "cruxible_core.kits._discover_local_kit_catalog",
        lambda: {_DEMO_KIT_ID: f"file://{local}"},
    )

    bundle = resolve_kit_ref(_DEMO_KIT_ID)

    assert (bundle.root / "LOCAL_MARKER").exists()
    assert fetch_env.requests == []


def test_tarball_sha256_mismatch_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env)
    asset = f"{_DEMO_KIT_ID}-0.2.0.tar.gz"
    fetch_env.files[asset] = fetch_env.files[asset] + b"tampered"

    with pytest.raises(ConfigError, match="tarball digest mismatch") as exc_info:
        resolve_published_kit(_DEMO_KIT_ID)

    assert "expected sha256:" in str(exc_info.value)
    assert "got sha256:" in str(exc_info.value)
    assert "clone https://github.com/cruxible-ai/cruxible" in str(exc_info.value)
    assert _published_cache_entries(tmp_path) == []


def test_dir_digest_mismatch_refuses_and_leaves_no_cache_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    wrong = "sha256:" + "0" * 64
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env, dir_digest=wrong)

    with pytest.raises(ConfigError, match="directory digest mismatch") as exc_info:
        resolve_published_kit(_DEMO_KIT_ID)

    assert wrong in str(exc_info.value)
    assert _published_cache_entries(tmp_path) == []


@pytest.mark.parametrize(
    ("label", "members"),
    [
        ("absolute path", [_file_member("/etc/evil")]),
        ("parent traversal", [_file_member("../evil.txt")]),
        ("nested traversal", [_file_member("providers/../../evil.txt")]),
    ],
)
def test_unsafe_tar_member_paths_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
    label: str,
    members: list[tuple[tarfile.TarInfo, bytes | None]],
) -> None:
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env, tarball=_tarball(members))

    with pytest.raises(ConfigError, match="unsafe member path"):
        resolve_published_kit(_DEMO_KIT_ID)

    assert _published_cache_entries(tmp_path) == []


def test_symlink_tar_member_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    link = tarfile.TarInfo(name="escape")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env, tarball=_tarball([(link, None)]))

    with pytest.raises(ConfigError, match="non-file member"):
        resolve_published_kit(_DEMO_KIT_ID)

    assert _published_cache_entries(tmp_path) == []


def test_download_size_cap_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env)
    monkeypatch.setattr(kit_distribution, "_MAX_BUNDLE_BYTES", 16)

    with pytest.raises(ConfigError, match="cap"):
        resolve_published_kit(_DEMO_KIT_ID)

    assert _published_cache_entries(tmp_path) == []


def test_download_failure_names_kit_url_and_clone_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    monkeypatch.setenv(kit_distribution.MANIFEST_URL_BASE_ENV, f"http://127.0.0.1:{dead_port}/")

    with pytest.raises(ConfigError, match="Could not download") as exc_info:
        resolve_published_kit(_DEMO_KIT_ID)

    message = str(exc_info.value)
    assert _DEMO_KIT_ID in message
    assert f"http://127.0.0.1:{dead_port}/{_DEMO_KIT_ID}-0.2.0.tar.gz" in message
    assert "clone https://github.com/cruxible-ai/cruxible" in message
    assert _published_cache_entries(tmp_path) == []


def test_packaged_base_url_must_be_https(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    _publish_demo_kit(tmp_path, monkeypatch, fetch_env)
    monkeypatch.delenv(kit_distribution.MANIFEST_URL_BASE_ENV)
    manifest = kit_distribution.load_published_manifest()
    assert manifest is not None
    monkeypatch.setattr(
        kit_distribution,
        "load_published_manifest",
        lambda: manifest.model_copy(update={"base_url": "http://release.invalid/"}),
    )

    with pytest.raises(ConfigError, match="must be https"):
        resolve_published_kit(_DEMO_KIT_ID)


# --- composed resolution: fetched overlays over fetched bases ---


def _publish_repo_kits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _BundleServer,
    kit_ids: list[str],
) -> None:
    script = _load_bundle_script()
    entries: dict[str, PublishedKitEntry] = {}
    for kit_id in kit_ids:
        built = script.build_kit_bundle(
            _REPO_ROOT / "kits" / kit_id, tmp_path / "assets", __version__
        )
        server.files[built.asset] = built.path.read_bytes()
        entries[kit_id] = PublishedKitEntry(
            asset=built.asset,
            tarball_sha256=built.tarball_sha256,
            dir_digest=built.dir_digest,
        )
    manifest = PublishedKitManifest(
        version=__version__, base_url="https://release.invalid/", kits=entries
    )
    monkeypatch.setattr(kit_distribution, "load_published_manifest", lambda: manifest)


def test_init_composes_fetched_overlay_over_fetched_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    # The pip-install acceptance path: no source checkout, empty cache, both
    # the overlay and its target_state base fetched from release bundles.
    _publish_repo_kits(tmp_path, monkeypatch, fetch_env, ["agent-operation", "case-law-monitoring"])
    # agent-operation declares an auth-managed entity type.
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

    result = service_init(tmp_path / "instance", kits=["agent-operation", "case-law-monitoring"])

    composed = result.instance.load_config()
    assert "Actor" in composed.entity_types  # base layer
    assert "Opinion" in composed.entity_types  # overlay layer
    assert (tmp_path / "instance" / "kits" / "agent-operation").is_dir()
    assert (tmp_path / "instance" / "kits" / "case-law-monitoring").is_dir()
    # Each bundle was downloaded exactly once despite repeated resolution.
    assert sorted(fetch_env.requests) == [
        f"agent-operation-{__version__}.tar.gz",
        f"case-law-monitoring-{__version__}.tar.gz",
    ]


def test_overlay_base_layer_resolution_reaches_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_env: _BundleServer,
) -> None:
    # target_state base resolution goes through resolve_kit_ref: with no
    # sibling kit dir and no source checkout, the base is fetched.
    _publish_repo_kits(tmp_path, monkeypatch, fetch_env, ["agent-operation"])
    overlay_dir = tmp_path / "overlay-only" / "demo-overlay"
    overlay_dir.mkdir(parents=True)
    overlay_dir.joinpath("cruxible-kit.yaml").write_text(
        "schema_version: cruxible.kit.v1\n"
        "kit_id: demo-overlay\n"
        "version: 0.2.0\n"
        "role: overlay\n"
        "target_state: agent-operation\n"
        "entry_config: config.yaml\n"
    )
    overlay_dir.joinpath("config.yaml").write_text(
        "version: '1.0'\nname: demo-overlay\nentity_types: {}\nrelationships: []\n"
    )

    base_layer = resolve_overlay_kit_base_layer(config_path=overlay_dir / "config.yaml")

    assert base_layer is not None
    assert base_layer.config_path is not None
    assert (tmp_path / "kit-cache") in base_layer.config_path.parents
    assert "Actor" in base_layer.config.entity_types
    assert fetch_env.requests == [f"agent-operation-{__version__}.tar.gz"]
