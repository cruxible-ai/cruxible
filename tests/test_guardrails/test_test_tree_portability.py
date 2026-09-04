"""Keep committed tests independent of a developer checkout."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from cruxible_client.authoring.context import resolve_playbill_context

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_DEVELOPER_PATHS = {
    "tests/test_playbill/test_family1_dogfood.py",
}


def test_committed_tests_do_not_depend_on_developer_paths_or_mutate_sys_path() -> None:
    developer_prefixes = ("/" + "Users/", "/" + "home/")
    system_temporary_prefix = "/" + "tmp"
    path_mutation = "sys.path." + "insert"
    cwd_relative_test_paths = ('Path("' + "tests/", "Path('" + "tests/")
    violations: list[str] = []
    for path in sorted((REPOSITORY_ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if relative in ALLOWED_DEVELOPER_PATHS:
            assert "CRUXIBLE_RUN_PLAYBILL_DOGFOOD" in text
            continue
        if (
            any(prefix in text for prefix in developer_prefixes)
            or system_temporary_prefix in text
            or path_mutation in text
            or any(marker in text for marker in cwd_relative_test_paths)
        ):
            violations.append(relative)
    assert violations == []


def test_state_root_can_only_be_removed_by_the_named_root_fixture() -> None:
    violations: list[str] = []
    for path in sorted((REPOSITORY_ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not node.args:
                continue
            first = node.args[0]
            removes_state_root = node.func.attr == "delenv" or (
                node.func.attr == "pop"
                and isinstance(node.func.value, ast.Attribute)
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "os"
                and node.func.value.attr == "environ"
            )
            if (
                removes_state_root
                and (isinstance(first, ast.Constant) and first.value == "CRUXIBLE_STATE_ROOT")
                and relative != "tests/conftest.py"
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []


def test_suite_uses_one_session_owned_state_root() -> None:
    state_root = Path(os.environ["CRUXIBLE_STATE_ROOT"])

    assert state_root.is_dir()
    assert state_root.name.startswith("server-state")


@pytest.mark.state_root_fallback
def test_state_root_fallback_marker_still_uses_an_isolated_home() -> None:
    assert "CRUXIBLE_STATE_ROOT" not in os.environ
    assert Path.home().name.startswith("state-root-fallback-home")


def test_no_test_reads_a_workspace_binding_it_did_not_create() -> None:
    """Workspace discovery must not find the developer's own governed checkout.

    A checkout that is itself a Playbill workspace carries
    `.playbill/coverage.json`, and discovery walks up from the current directory
    to find exactly that -- so a suite run inside one silently retargets at a
    live instance and fails on a machine where nothing is wrong with the code.
    The repository root is the probe: it is a candidate root for every test that
    does not chdir, and it is never a directory a test created.
    """

    resolved = resolve_playbill_context(cwd=REPOSITORY_ROOT, remembered={})

    assert resolved.workspace_binding_path is None
    assert resolved.workspace_source == "local"
    assert resolved.workspace_attached is False
    assert resolved.instance_source == "local"


def test_a_workspace_binding_a_test_creates_is_still_read(tmp_path: Path) -> None:
    """The isolation hides the ambient binding, not the mechanism under test."""

    binding_dir = tmp_path / ".playbill"
    binding_dir.mkdir()
    (binding_dir / "coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v1",
                "server_url": "https://workspace.example.test",
                "instance_id": "inst_from_this_test",
            }
        ),
        encoding="utf-8",
    )

    resolved = resolve_playbill_context(cwd=tmp_path, remembered={}, home=tmp_path.parent)

    assert resolved.workspace_binding_path == binding_dir / "coverage.json"
    assert resolved.workspace_source == "workspace"
    assert resolved.instance_id == "inst_from_this_test"
    assert resolved.instance_source == "workspace"
