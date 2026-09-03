"""Keep committed tests independent of a developer checkout."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_DEVELOPER_PATHS = {
    "tests/test_playbill/test_family1_dogfood.py",
}


def test_committed_tests_do_not_depend_on_developer_paths_or_mutate_sys_path() -> None:
    developer_prefixes = ("/" + "Users/", "/" + "home/")
    system_temporary_prefix = "/" + "tmp"
    path_mutation = "sys.path." + "insert"
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
