"""Keep committed tests independent of a developer checkout."""

from __future__ import annotations

from pathlib import Path

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
