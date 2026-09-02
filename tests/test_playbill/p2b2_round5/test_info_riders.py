"""Portable regressions for the round-5 operational riders."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from cruxible_core.playbill.provider_process_leases import ProviderProcessLeaseStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _load_portability_guard():  # type: ignore[no-untyped-def]
    path = REPOSITORY_ROOT / "tests/test_guardrails/test_test_tree_portability.py"
    spec = importlib.util.spec_from_file_location("_p2b2_portability_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_portability_guard_detects_a_system_temporary_literal(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_portability_guard()
    tests = short_root / "tests"
    tests.mkdir()
    planted = tests / "test_planted.py"
    planted.write_text('ROOT = "' + "/" + 'tmp/hidden"\n', encoding="utf-8")
    monkeypatch.setattr(module, "REPOSITORY_ROOT", short_root)

    with pytest.raises(AssertionError):
        module.test_committed_tests_do_not_depend_on_developer_paths_or_mutate_sys_path()


def test_all_process_lease_store_calls_name_the_control_root() -> None:
    parameter = inspect.signature(ProviderProcessLeaseStore).parameters["control_root"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_short_root_finalizers_retry_cleanup_after_a_wait() -> None:
    for round_name in ("p2b2_round3", "p2b2_round4", "p2b2_round5"):
        source = (
            REPOSITORY_ROOT
            / "tests"
            / "test_playbill"
            / round_name
            / "conftest.py"
        ).read_text(encoding="utf-8")
        assert source.count("shutil.rmtree(root, ignore_errors=True)") == 2
        assert "time.sleep(" in source
