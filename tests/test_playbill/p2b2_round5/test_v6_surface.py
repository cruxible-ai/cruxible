"""Round-5: docs accuracy, the portability guardrail's own detection, and costs."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cruxible_core
import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_core.runtime.provider_runtime import (
    ProviderRuntimeOperationalConfigV1,
    ProviderRuntimeOperator,
)

REPOSITORY_ROOT = Path(cruxible_core.__file__).resolve().parents[2]
DOCS = REPOSITORY_ROOT / "docs" / "cli-reference.md"


def _documented_defaults() -> dict[str, str]:
    text = DOCS.read_text("utf-8")
    section = text[text.index("### Provider runtime operational configuration") :]
    section = section[: section.index("\n## ")]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        match = re.match(r"\|\s*`([a-z_]+)`\s*\|\s*`([^`]+)`\s*\|", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def test_every_documented_knob_default_matches_the_model() -> None:
    documented = _documented_defaults()
    assert len(documented) >= 9, documented
    for name, field in ProviderRuntimeOperationalConfigV1.model_fields.items():
        if name == "tag":
            continue
        assert name in documented, name
        if isinstance(field.default, tuple):
            # Every tuple knob is documented in the JSON config shape the CLI reads,
            # so the guardrail reads the shape rather than naming each knob: the
            # exemption named `deployments` alone and went red the moment a second
            # tuple knob shipped.
            assert documented[name] == "[]", (name, documented[name])
            continue
        assert documented[name] == str(field.default), (name, documented[name], field.default)


def test_three_spot_checked_defaults_are_the_shipped_module_constants() -> None:
    documented = _documented_defaults()
    assert documented["descendant_tracker_poll_interval_seconds"] == str(
        lease_module.DEFAULT_PROVIDER_DESCENDANT_TRACKER_POLL_INTERVAL_SECONDS
    )
    assert documented["recovery_aggregate_timeout_seconds"] == str(
        lease_module.DEFAULT_PROVIDER_RECOVERY_AGGREGATE_TIMEOUT_SECONDS
    )
    assert documented["lease_acquisition_timeout_seconds"] == str(
        lease_module.DEFAULT_PROVIDER_LEASE_ACQUISITION_TIMEOUT_SECONDS
    )


def test_the_two_status_lines_the_docs_promise_exist_in_the_cli() -> None:
    cli = (REPOSITORY_ROOT / "src" / "cruxible_core" / "cli").rglob("*.py")
    text = "".join(path.read_text("utf-8") for path in cli)
    assert "Provider lane:" in text
    assert "Provider lane reason:" in text


def test_the_docs_explain_restart_free_construction_repair() -> None:
    """V-3: the construction re-initialization path is explicit."""

    text = DOCS.read_text("utf-8")
    section = text[text.index("### Provider runtime operational configuration") :]
    section = section[: section.index("\n## ")]
    assert "restart" in section.lower()
    assert "exactly the construction stages" in section
    assert "re-arm" in section or "lazy re-arm" in section


# ---------------------------------------------------- guardrail self-detection


def _load_guardrail():  # type: ignore[no-untyped-def]
    path = REPOSITORY_ROOT / "tests" / "test_guardrails" / "test_test_tree_portability.py"
    spec = importlib.util.spec_from_file_location("_portability_guardrail", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "planted",
    [
        'ROOT = "/' + 'Users/someone/Git/checkout"\n',
        'ROOT = "/' + 'home/someone/checkout"\n',
        "import sys\nsys.path." + "insert(0, '/x')\n",
        'ROOT = "/' + 'tmp/hidden"\n',
    ],
)
def test_the_portability_guardrail_detects_a_planted_violation(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    module = _load_guardrail()
    fake_tests = short_root / "tests" / "test_thing"
    fake_tests.mkdir(parents=True)
    (fake_tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPOSITORY_ROOT", short_root)
    module.test_committed_tests_do_not_depend_on_developer_paths_or_mutate_sys_path()
    bad = fake_tests / "test_bad.py"
    bad.write_text(planted, encoding="utf-8")
    with pytest.raises(AssertionError):
        module.test_committed_tests_do_not_depend_on_developer_paths_or_mutate_sys_path()
    bad.unlink()
    module.test_committed_tests_do_not_depend_on_developer_paths_or_mutate_sys_path()


def test_the_guardrail_allow_list_is_exactly_the_gated_dogfood_test() -> None:
    module = _load_guardrail()
    assert module.ALLOWED_DEVELOPER_PATHS == {"tests/test_playbill/test_family1_dogfood.py"}
    gated = REPOSITORY_ROOT / "tests" / "test_playbill" / "test_family1_dogfood.py"
    assert "CRUXIBLE_RUN_PLAYBILL_DOGFOOD" in gated.read_text("utf-8")


def test_the_scratch_prefix_is_gitignored_and_the_tree_is_clean() -> None:
    ignore = (REPOSITORY_ROOT / ".gitignore").read_text("utf-8").splitlines()
    assert "/.b2-*" in ignore
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout == "", completed.stdout


def test_no_adopted_regression_names_a_developer_path() -> None:
    for directory in ("p2b2_round3", "p2b2_round4", "p2b2_round5"):
        for path in (REPOSITORY_ROOT / "tests" / "test_playbill" / directory).rglob("*.py"):
            text = path.read_text("utf-8")
            assert "/" + "Users/" not in text, path
            assert "sys.path." + "insert" not in text, path
            if "REPOSITORY_ROOT" in text:
                assert "Path(__file__)" in text, path


# ---------------------------------------------------------------- state layout


def test_the_daemon_scratch_parent_sits_inside_a_private_state_root(short_root: Path) -> None:
    operator = ProviderRuntimeOperator(short_root)
    assert operator.process_leases is not None
    daemon = short_root / "daemon"
    assert daemon.is_dir()
    root_mode = stat.S_IMODE(short_root.stat().st_mode)
    daemon_mode = stat.S_IMODE(daemon.stat().st_mode)
    # The state root must be private even if the intermediate is not.
    assert root_mode == 0o700, oct(root_mode)
    assert daemon_mode & 0o077 == 0 or root_mode == 0o700, (oct(root_mode), oct(daemon_mode))


# ------------------------------------------------------------- re-arm cost


def test_only_the_first_degraded_invocation_pays_the_aggregate_budget(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-4: later calls inside the backoff refuse from cached state."""

    from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused

    store_root = short_root / "daemon" / "provider-process-leases"
    (short_root / "daemon").mkdir(parents=True, exist_ok=True)
    (short_root / "daemon" / "provider-runtime.json").write_text(
        '{"tag":"cruxible-provider-runtime-operational-config-v1",'
        '"lease_recovery_timeout_seconds":0.2,'
        '"recovery_aggregate_timeout_seconds":0.35,'
        '"rearm_backoff_seconds":5.0}',
        encoding="utf-8",
    )
    operator = ProviderRuntimeOperator(short_root)
    assert operator.process_leases is not None
    assert operator.process_leases.recovery_aggregate_timeout_seconds == 0.35
    survivors = [
        subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(4)
    ]
    real_killpg = os.killpg
    try:
        from cruxible_client.contracts.canonical import canonical_bytes

        boot = lease_module._current_boot_id()
        for index, process in enumerate(survivors):
            invocation = "sha256:" + f"{index:064x}"
            record_path, _control = operator.process_leases.paths(invocation)
            record_path.write_bytes(
                canonical_bytes(
                    {
                        "invocation_id": invocation,
                        "pid": process.pid,
                        "process_group_id": process.pid,
                        "session_id": os.getsid(process.pid),
                        "boot_id": boot,
                        "process_start_time": lease_module._process_start_time(process.pid),
                    }
                )
            )
        monkeypatch.setattr(lease_module.os, "killpg", lambda *args: None)
        operator.mark_unavailable(
            "provider_process_group_survived_recovery", "stuck", retryable=True
        )
        costs: list[float] = []
        for _ in range(3):
            started = time.monotonic()
            with pytest.raises(ProviderLocalRuntimeRefused):
                operator._begin_invocation()
            costs.append(time.monotonic() - started)
        assert costs[0] >= 0.3, costs
        assert max(costs[1:]) < 0.1, costs
        assert store_root.exists()
    finally:
        for process in survivors:
            with contextlib.suppress(OSError):
                real_killpg(os.getpgid(process.pid), 9)
            with contextlib.suppress(Exception):
                process.kill()
                process.wait(timeout=2)
