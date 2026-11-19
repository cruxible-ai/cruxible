"""scripts/console_release.py: the pinned console commit, and the check that archives carry it."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cruxible_core.console.bundle import MANIFEST_NAME, MANIFEST_TAG

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMIT = "c" * 40


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "console_release", REPO_ROOT / "scripts" / "console_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(files: dict[str, bytes], **overrides: Any) -> dict[str, Any]:
    script = _script()
    return {
        "tag": MANIFEST_TAG,
        "console_commit": COMMIT,
        "pin_matches_core": True,
        "core_version": "9.9.9",
        "files": {name: script._sha256(body) for name, body in files.items()},
        **overrides,
    }


def _build(dist: Path, files: dict[str, bytes], manifest: dict[str, Any]) -> None:
    """A cruxible 9.9.9 wheel and sdist carrying ``files`` and ``manifest`` as a release would."""

    dist.mkdir(parents=True, exist_ok=True)
    body = json.dumps(manifest).encode()
    with zipfile.ZipFile(dist / "cruxible-9.9.9-py3-none-any.whl", "w") as wheel:
        wheel.writestr("cruxible-9.9.9.dist-info/METADATA", "Name: cruxible\nVersion: 9.9.9\n")
        for name, data in files.items():
            wheel.writestr(f"cruxible_core/console_dist/{name}", data)
        wheel.writestr(f"cruxible_core/console_dist/{MANIFEST_NAME}", body)
    with tarfile.open(dist / "cruxible-9.9.9.tar.gz", "w:gz") as sdist:
        entries = {"PKG-INFO": b"Name: cruxible\nVersion: 9.9.9\n"}
        entries |= {f"src/cruxible_core/console_dist/{n}": d for n, d in files.items()}
        entries[f"src/cruxible_core/console_dist/{MANIFEST_NAME}"] = body
        for name, data in entries.items():
            info = tarfile.TarInfo(f"cruxible-9.9.9/{name}")
            info.size = len(data)
            sdist.addfile(info, io.BytesIO(data))


FILES = {"index.html": b"<!doctype html>", "assets/app.js": b"console.log(1)"}


def test_the_tracked_pin_names_a_full_commit_of_the_console_repository() -> None:
    pin = _script().read_pin()
    assert pin["repository"] == "cruxible-ai/cruxible-app"
    assert len(pin["commit"]) == 40


def test_pin_refuses_a_short_commit_or_extra_keys(tmp_path: Path) -> None:
    script = _script()
    path = tmp_path / "pin.json"
    path.write_text(json.dumps({"repository": "o/r", "commit": "abc123"}))
    with pytest.raises(script.ReleaseRefused, match="40-character"):
        script.read_pin(path)
    path.write_text(json.dumps({"repository": "o/r", "commit": COMMIT, "branch": "x"}))
    with pytest.raises(script.ReleaseRefused, match="exactly"):
        script.read_pin(path)


def test_archives_carrying_the_pinned_console_pass(tmp_path: Path) -> None:
    script = _script()
    _build(tmp_path, FILES, _manifest(FILES))
    manifest = script.check_dist(tmp_path, commit=COMMIT)
    assert set(manifest["files"]) == set(FILES)
    assert script.main(["check", str(tmp_path), "--commit", COMMIT]) == 0


@pytest.mark.parametrize(
    ("manifest", "files", "match"),
    [
        (_manifest(FILES, console_commit="d" * 40), FILES, "the release pins"),
        (_manifest(FILES, pin_matches_core=False), FILES, "pin_matches_core"),
        (_manifest(FILES, core_version="9.9.8"), FILES, "the archive is 9.9.9"),
        (_manifest(FILES), {"index.html": FILES["index.html"]}, "assets/app.js is listed"),
        (_manifest(FILES), {**FILES, "assets/app.js": b"tampered"}, "manifest hash"),
    ],
)
def test_a_wrong_commit_core_or_file_is_refused(
    tmp_path: Path, manifest: dict[str, Any], files: dict[str, bytes], match: str
) -> None:
    script = _script()
    _build(tmp_path, files, manifest)
    with pytest.raises(script.ReleaseRefused, match=match):
        script.check_dist(tmp_path, commit=COMMIT)


def test_archives_without_a_console_are_refused(tmp_path: Path) -> None:
    script = _script()
    tmp_path.mkdir(exist_ok=True)
    with zipfile.ZipFile(tmp_path / "cruxible-9.9.9-py3-none-any.whl", "w") as wheel:
        wheel.writestr("cruxible-9.9.9.dist-info/METADATA", "Name: cruxible\nVersion: 9.9.9\n")
    with pytest.raises(script.ReleaseRefused, match="carries no console"):
        script.check_wheel(tmp_path / "cruxible-9.9.9-py3-none-any.whl", commit=COMMIT)
    assert script.main(["check", str(tmp_path / "empty")]) == 1
