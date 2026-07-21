"""Tests for scripts/check_kit_release_assets.py without live network calls."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script() -> ModuleType:
    path = _REPO_ROOT / "scripts" / "check_kit_release_assets.py"
    spec = importlib.util.spec_from_file_location("check_kit_release_assets", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_manifest(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": "0.2.8",
                "base_url": "https://example.invalid/releases/download/v0.2.8/",
                "kits": {
                    "alpha": {"asset": "alpha-0.2.8.tar.gz"},
                    "beta": {"asset": "beta-0.2.8.tar.gz"},
                },
            }
        ),
        encoding="utf-8",
    )


def test_remote_tag_exists_uses_exact_tag_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    recorded: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="tag", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert script.remote_tag_exists("upstream", "v0.2.8", 7.0) is True
    assert recorded["command"] == [
        "git",
        "ls-remote",
        "--exit-code",
        "--tags",
        "upstream",
        "refs/tags/v0.2.8",
    ]
    assert recorded["kwargs"]["timeout"] == 7.0


def test_remote_tag_exists_returns_false_only_for_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2, stdout="", stderr=""),
    )

    assert script.remote_tag_exists("origin", "v0.2.8", 30.0) is False


def test_missing_tag_skips_without_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    monkeypatch.setattr(script, "remote_tag_exists", lambda *_args: False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    def unexpected_head(*_args: Any) -> int:
        raise AssertionError("missing-tag path must not make HTTP requests")

    monkeypatch.setattr(script, "_head_status", unexpected_head)

    assert script.main(["--manifest-path", str(manifest_path)]) == 0
    out = capsys.readouterr().out
    assert "NOTICE: v0.2.8 does not exist on remote 'origin'" in out
    assert "::notice title=Kit release assets::v0.2.8 does not exist" in out
    assert "skipping release asset URL checks" in out


def test_existing_tag_heads_every_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    monkeypatch.setattr(script, "remote_tag_exists", lambda *_args: True)
    requests: list[tuple[str, str, float]] = []

    def fake_head(url: str, timeout: float) -> int:
        requests.append((url, "HEAD", timeout))
        return 200

    monkeypatch.setattr(script, "_head_status", fake_head)

    assert script.main(["--manifest-path", str(manifest_path), "--timeout", "5"]) == 0
    assert requests == [
        (
            "https://example.invalid/releases/download/v0.2.8/alpha-0.2.8.tar.gz",
            "HEAD",
            5.0,
        ),
        (
            "https://example.invalid/releases/download/v0.2.8/beta-0.2.8.tar.gz",
            "HEAD",
            5.0,
        ),
    ]
    assert "all kit release asset URLs are available for v0.2.8" in capsys.readouterr().out


def test_non_200_and_request_errors_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    monkeypatch.setattr(script, "remote_tag_exists", lambda *_args: True)

    def fake_head(url: str, _timeout: float) -> int:
        if "alpha" in url:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(script, "_head_status", fake_head)

    assert script.main(["--manifest-path", str(manifest_path)]) == 1
    err = capsys.readouterr().err
    assert "alpha: HEAD" in err
    assert "returned HTTP 404, expected 200" in err
    assert "beta: HEAD" in err
    assert "failed: <urlopen error offline>" in err


def test_remote_lookup_failure_is_not_treated_as_missing_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=128, stdout="", stderr="fatal: could not read from remote"
        ),
    )

    assert script.main(["--manifest-path", str(manifest_path)]) == 1
    err = capsys.readouterr().err
    assert "git ls-remote failed" in err
    assert "exit 128" in err
    assert "skipping" not in err


def test_head_request_uses_head_method(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    recorded: dict[str, Any] = {}

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> Response:
        recorded["method"] = request.get_method()
        recorded["timeout"] = timeout
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert script._head_status("https://example.invalid/asset.tar.gz", 9.0) == 200
    assert recorded == {"method": "HEAD", "timeout": 9.0}
