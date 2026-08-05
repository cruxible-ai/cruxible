"""Tests for scripts/check_version_lockstep.py, including the live repo tree."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script() -> ModuleType:
    path = _REPO_ROOT / "scripts" / "check_version_lockstep.py"
    spec = importlib.util.spec_from_file_location("check_version_lockstep", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_tree(
    tmp_path: Path,
    *,
    core_version: str = "1.2.3",
    client_version: str = "1.2.3",
    pin: str | None = None,
    manifest_version: str | None = None,
    base_url: str | None = None,
) -> dict[str, Path]:
    pin = f"cruxible-client=={client_version}" if pin is None else pin
    manifest_version = core_version if manifest_version is None else manifest_version
    if base_url is None:
        base_url = f"https://example.invalid/releases/download/v{core_version}/"

    core = tmp_path / "pyproject.toml"
    core.write_text(
        "[project]\n"
        'name = "cruxible"\n'
        f'version = "{core_version}"\n'
        f'dependencies = ["{pin}", "pydantic>=2.12"]\n',
        encoding="utf-8",
    )
    client = tmp_path / "client-pyproject.toml"
    client.write_text(
        f'[project]\nname = "cruxible-client"\nversion = "{client_version}"\n',
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"version": manifest_version, "base_url": base_url, "kits": {}}),
        encoding="utf-8",
    )
    return {"core": core, "client": client, "manifest": manifest}


def _argv(paths: dict[str, Path], *extra: str) -> list[str]:
    return [
        "--core-pyproject",
        str(paths["core"]),
        "--client-pyproject",
        str(paths["client"]),
        "--manifest-path",
        str(paths["manifest"]),
        *extra,
    ]


def test_repo_tree_is_in_lockstep() -> None:
    """The committed tree must always be releasable; this is the pre-push gate."""
    script = _load_script()
    assert script.check_version_lockstep() == []


def test_aligned_tree_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()
    paths = _write_tree(tmp_path)

    assert script.main(_argv(paths, "--tag", "v1.2.3")) == 0
    assert "version lockstep ok" in capsys.readouterr().out


def test_client_version_behind_core_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact v0.3.1 break: core bumped, cruxible-client left at 0.3.0."""
    script = _load_script()
    paths = _write_tree(
        tmp_path,
        core_version="0.3.1",
        client_version="0.3.0",
        pin="cruxible-client==0.3.1",
    )

    assert script.main(_argv(paths, "--tag", "v0.3.1")) == 1
    err = capsys.readouterr().err
    assert "Version mismatch: cruxible=0.3.1 cruxible-client=0.3.0" in err
    # The pin disagreeing with the built client is reported in the same run,
    # not left for the next attempt.
    assert "Missing exact dependency pin: cruxible-client==0.3.0" in err
    assert "found: cruxible-client==0.3.1" in err


def test_dependency_pin_not_exact_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()
    paths = _write_tree(tmp_path, pin="cruxible-client>=1.2.3")

    assert script.main(_argv(paths)) == 1
    err = capsys.readouterr().err
    assert "Missing exact dependency pin: cruxible-client==1.2.3" in err
    assert "found: cruxible-client>=1.2.3" in err


def test_manifest_version_and_base_url_must_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    paths = _write_tree(
        tmp_path,
        manifest_version="1.2.2",
        base_url="https://example.invalid/releases/download/v1.2.2/",
    )

    assert script.main(_argv(paths)) == 1
    err = capsys.readouterr().err
    assert "Kit manifest version '1.2.2' does not match 1.2.3" in err
    assert "does not point at v1.2.3" in err


def test_tag_mismatch_fails_only_when_a_tag_is_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    paths = _write_tree(tmp_path)

    # No tag locally: an otherwise-aligned tree passes.
    assert script.main(_argv(paths)) == 0
    capsys.readouterr()

    assert script.main(_argv(paths, "--tag", "v1.2.4")) == 1
    assert "Tag v1.2.4 does not match package version 1.2.3" in capsys.readouterr().err


def test_unreadable_inputs_error_rather_than_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    paths = _write_tree(tmp_path)
    paths["manifest"].unlink()

    assert script.main(_argv(paths)) == 1
    assert "could not read manifest" in capsys.readouterr().err


def test_ci_parity_runs_the_lockstep_check() -> None:
    """Item 2's whole point: the gate script must actually invoke it."""
    parity = (_REPO_ROOT / "scripts" / "ci_parity.sh").read_text(encoding="utf-8")
    assert "scripts/check_version_lockstep.py" in parity


def test_publish_workflow_runs_the_same_script_with_the_tag() -> None:
    workflow = (_REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert 'python scripts/check_version_lockstep.py --tag "$GITHUB_REF_NAME"' in workflow
