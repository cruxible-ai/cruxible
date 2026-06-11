"""Tests for published state transport backends."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_core.errors import TransportError
from cruxible_core.snapshot.types import PublishedStateManifest, StateSnapshot
from cruxible_core.transport.backends import OciReleaseTransport


def test_oci_publish_uses_bundle_dir_as_working_directory(tmp_path: Path, monkeypatch) -> None:
    bundle_dir = _write_bundle(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(args, *, check, capture_output, text, cwd=None):  # noqa: ANN001
        captured["args"] = args
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = OciReleaseTransport().publish("registry.example/cruxible/case-law:v1", bundle_dir)

    assert result == "registry.example/cruxible/case-law:v1"
    assert captured["cwd"] == str(bundle_dir)
    assert "manifest.json:application/vnd.cruxible.manifest.v1+json" in captured["args"]


def test_oci_publish_raises_clear_error_when_oras_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle_dir = _write_bundle(tmp_path)

    def missing_run(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise FileNotFoundError("oras")

    monkeypatch.setattr(subprocess, "run", missing_run)

    with pytest.raises(TransportError, match="oras binary not found"):
        OciReleaseTransport().publish("registry.example/cruxible/case-law:v1", bundle_dir)


def _write_bundle(tmp_path: Path) -> Path:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest = PublishedStateManifest(
        state_id="case-law",
        release_id="v1.0.0",
        snapshot_id="snap_1",
        compatibility="data_only",
        owned_entity_types=["Case"],
        owned_relationship_types=["cites"],
    )
    snapshot = StateSnapshot(
        snapshot_id="snap_1",
        created_at=datetime.now(timezone.utc),
        label="v1.0.0",
        config_digest="sha256:cfg",
        lock_digest=None,
        graph_digest="sha256:graph",
        parent_snapshot_id=None,
        origin_snapshot_id=None,
    )
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")))
    (bundle_dir / "snapshot.json").write_text(json.dumps(snapshot.model_dump(mode="json")))
    (bundle_dir / "config.yaml").write_text('version: "1.0"\nname: case-law\nentity_types: {}\n')
    (bundle_dir / "graph.json").write_text(json.dumps({"directed": True, "multigraph": True}))
    return bundle_dir
