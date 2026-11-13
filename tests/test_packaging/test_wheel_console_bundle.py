"""A release build carries the Git-ignored console bundle, and the installed code serves it.

``src/cruxible_core/console_dist/`` is ignored by Git (``scripts/build_console.py``
creates it), so hatch would drop it from both the sdist and the wheel; the
``artifacts`` entries in pyproject.toml put it back. ``uv build`` makes the
wheel from the sdist, so both must carry it. This builds a copy of the project
holding a tiny fake bundle the way the release job does, checks both archives,
and serves ``index.html`` and the manifest from the unpacked wheel.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from cruxible_core.console.bundle import MANIFEST_NAME, write_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.wheel

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "console_dist", ".mypy_cache")

_SERVE = r"""
import json, sys
from pathlib import Path
from starlette.testclient import TestClient
import cruxible_core
from cruxible_core.console.bundle import load_bundle
from cruxible_core.console.listener import ConsoleListener, Upstream

bundle = load_bundle()
app = ConsoleListener(
    bundle=bundle,
    upstream=Upstream(label="unused", socket_path="/nonexistent"),
    instance_id="inst_x",
    port=4321,
    bootstrap_token="t",
    core_identity={},
)
client = TestClient(app, base_url="http://127.0.0.1:4321")
print(json.dumps({
    "package": str(Path(cruxible_core.__file__).parent),
    "index": client.get("/").text,
    "spa": client.get("/i/inst_x/overview").text,
    "manifest": client.get("/__console/cruxible-console-manifest.json").json(),
}))
"""


def test_a_built_wheel_carries_and_serves_the_console_bundle(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not available; cannot build the wheel")
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE", ".gitignore"):
        shutil.copy2(REPO_ROOT / name, project / name)
    shutil.copytree(REPO_ROOT / "src", project / "src", ignore=_IGNORE)
    shutil.copytree(
        REPO_ROOT / "packages" / "cruxible-client",
        project / "packages" / "cruxible-client",
        ignore=_IGNORE,
    )
    dist = project / "src" / "cruxible_core" / "console_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>fake console</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    write_manifest(dist, console_commit="f" * 40, console_pin={}, pin_matches_core=True)

    out = tmp_path / "out"
    built = subprocess.run(
        [uv, "build", "--package", "cruxible", "--out-dir", str(out)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr

    [sdist] = out.glob("cruxible-*.tar.gz")
    with tarfile.open(sdist) as archive:
        sdist_names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    assert "src/cruxible_core/console_dist/index.html" in sdist_names
    assert f"src/cruxible_core/console_dist/{MANIFEST_NAME}" in sdist_names

    [wheel] = out.glob("cruxible-*.whl")
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        archive.extractall(unpacked)
    assert {
        "cruxible_core/console_dist/index.html",
        "cruxible_core/console_dist/assets/app.js",
        f"cruxible_core/console_dist/{MANIFEST_NAME}",
    } <= names

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(unpacked), str(REPO_ROOT / "packages" / "cruxible-client" / "src")]
        ),
    }
    served = subprocess.run(
        [sys.executable, "-c", _SERVE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert served.returncode == 0, served.stderr
    result = json.loads(served.stdout.strip().splitlines()[-1])
    assert result["package"] == str(unpacked / "cruxible_core")
    assert result["index"] == result["spa"] == "<!doctype html><title>fake console</title>"
    assert result["manifest"]["console_commit"] == "f" * 40
    assert set(result["manifest"]["files"]) == {"index.html", "assets/app.js"}
