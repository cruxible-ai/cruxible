"""Round-4: static/contract re-establishment and new static defects."""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
import typing
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

FENCE_FILES = (
    "src/cruxible_core/playbill/provider_process_leases.py",
    "src/cruxible_core/playbill/provider_local_runtime.py",
    "src/cruxible_core/runtime/provider_runtime.py",
    "src/cruxible_core/runtime/playbill_manager.py",
)


# ---------- re-establishment ----------


def test_l9_no_tmp_remains_on_the_fence_path() -> None:
    for name in FENCE_FILES:
        text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
        assert "/tmp" not in text, name


def test_fence_code_literal_is_exactly_five_members() -> None:
    from cruxible_core.playbill.provider_process_leases import ProviderProcessFenceCodeV1

    assert set(typing.get_args(ProviderProcessFenceCodeV1)) == {
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
        "provider_process_lease_echo_failed",
        "provider_process_lease_echo_mismatch",
        "provider_process_group_survived_recovery",
    }


def test_lane_code_literal_is_the_closed_ruled_set() -> None:
    from cruxible_client.contracts import ProviderLaneUnavailableCodeV1
    from cruxible_core.playbill.provider_process_leases import ProviderProcessFenceCodeV1

    members = set(typing.get_args(ProviderLaneUnavailableCodeV1))
    assert members == set(typing.get_args(ProviderProcessFenceCodeV1)) | {
        "provider_runtime_recovery_failed"
    }


def test_recovery_failure_code_is_the_fence_literal() -> None:
    from cruxible_core.playbill.provider_process_leases import (
        ProviderProcessFenceCodeV1,
        ProviderProcessRecoveryFailureV1,
    )

    hints = typing.get_type_hints(ProviderProcessRecoveryFailureV1)
    assert hints["code"] is ProviderProcessFenceCodeV1


def test_server_info_provider_lane_is_present_and_required() -> None:
    from cruxible_client.contracts import ProviderLaneStatusV1, ServerInfoResult

    field = ServerInfoResult.model_fields["provider_lane"]
    assert field.is_required()
    assert field.annotation is ProviderLaneStatusV1
    with pytest.raises(ValueError):
        ProviderLaneStatusV1(state="available", code="provider_runtime_recovery_failed", detail="x")
    with pytest.raises(ValueError):
        ProviderLaneStatusV1(state="unavailable", code=None, detail=None)


def test_node_refusal_details_is_semantic_only_no_schema_change() -> None:
    """T-6 must be propagation, not a field addition."""
    import subprocess

    from cruxible_client.contracts.procedures.results import ProcedureNodeRefusalV1

    assert "details" in ProcedureNodeRefusalV1.model_fields
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "diff",
            "3cbeff5737024182ac0bd77ec0fdf64e7a71fb8d..3de5c80f662994c8ea2c053f5fa334421ad340a6",
            "--",
            "packages/cruxible-client/src/cruxible_client/contracts/procedures/results.py",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "details" not in diff or "+    details" not in diff


def test_l8_fence_scope_is_required_and_fixed() -> None:
    from cruxible_client.contracts.provider_execution import ProviderInvocationReceiptV1

    field = ProviderInvocationReceiptV1.model_fields["fence_scope"]
    assert field.is_required()
    assert typing.get_args(field.annotation) == ("process_group+descendant_sweep",)
    assert field.description == (
        "Process-group kill plus deterministic same-session sweep and best-effort "
        "cross-session sweep within the configured poll interval."
    )


def test_c10_caps_carry_no_numeric_literal() -> None:
    from cruxible_core.playbill.provider_local_runtime import translate_provider_budget

    source = inspect.getsource(translate_provider_budget)
    assert not re.search(r"\b\d{3,}\b", source), source


def test_l12_f5_no_bare_timeout_literal_on_the_fence_path() -> None:
    text = (REPOSITORY_ROOT / "src/cruxible_core/playbill/provider_process_leases.py").read_text()
    assert re.search(r"timeout_seconds: float = 5", text) is None
    runtime = (REPOSITORY_ROOT / "src/cruxible_core/playbill/provider_local_runtime.py").read_text()
    assert "timeout=5" not in runtime


def test_hand_edit_repair_fabricates_no_command() -> None:
    from cruxible_core.service.playbill_next import _repair_command

    assert _repair_command("hand_edit", arguments={}) is None


# ---------- NEW static defects ----------


def test_committed_regressions_do_not_hardcode_a_developer_worktree() -> None:
    """Adopted round-3 regressions run only on the reviewer's machine."""

    offenders: list[str] = []
    for path in sorted((REPOSITORY_ROOT / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "/" + "Users/" not in text and "/" + "home/" not in text:
            continue
        if "skipif" in text or "CRUXIBLE_RUN_PLAYBILL_DOGFOOD" in text:
            continue  # opt-in dogfood precedent
        offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert offenders == [], offenders


def test_adopted_regressions_write_into_the_repository_working_tree() -> None:
    conftest = (REPOSITORY_ROOT / "tests/test_playbill/p2b2_round3/conftest.py").read_text(
        encoding="utf-8"
    )
    assert "dir=REPOSITORY_ROOT" in conftest
    ignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/.b2-*" in ignore


def test_publish_translates_every_write_oserror() -> None:
    """T-2 ruled a publish failure must never unwind untyped."""

    from cruxible_core.playbill.provider_process_leases import ProviderProcessLeaseStore

    source = inspect.getsource(ProviderProcessLeaseStore.publish)
    assert "tempfile.mkstemp(" in source
    head, _sep, tail = source.partition("record_path, _control_path = self.paths")
    # Everything from mkstemp onward has a typed OSError translation; the
    # BaseException branch remains only to clean up non-OS failures unchanged.
    assert "except BaseException:" in tail
    assert "except OSError as exc:" in tail
    assert "ProviderLocalRuntimeRefused" in tail, tail
    assert tail.index("try:") < tail.index("tempfile.mkstemp")


def test_run_child_catches_the_typed_publish_refusal() -> None:
    from cruxible_core.playbill import provider_local_runtime

    source = inspect.getsource(provider_local_runtime._run_child)
    tree = ast.parse(source.strip())
    caught: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            calls_publish = any(
                isinstance(inner, ast.Attribute) and inner.attr == "publish"
                for statement in node.body
                for inner in ast.walk(statement)
            )
            if not calls_publish:
                continue
            for handler in node.handlers:
                if isinstance(handler.type, ast.Name):
                    caught.append(handler.type.id)
    assert "ProviderLocalRuntimeRefused" in caught
    assert "OSError" not in caught and "BaseException" not in caught


def test_operator_construction_guards_every_filesystem_stage() -> None:
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

    source = "\n".join(
        (
            textwrap.dedent(inspect.getsource(ProviderRuntimeOperator.__init__)),
            textwrap.dedent(
                inspect.getsource(ProviderRuntimeOperator._initialize_filesystem_components)
            ),
        )
    )
    tree = ast.parse(source.strip())
    guarded_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    if hasattr(inner, "lineno"):
                        guarded_lines.add(inner.lineno)
    unguarded = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mkdir"
        and node.lineno not in guarded_lines
    ]
    assert unguarded == []
    # FileProviderSecretStore construction is also outside every guard
    secret_store_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FileProviderSecretStore"
    ]
    assert secret_store_lines and set(secret_store_lines) <= guarded_lines


def test_lazy_rearm_routes_the_recovery_result_through_the_manager_fold() -> None:
    """A lazy re-arm cannot clear before the manager acknowledges its result."""

    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

    source = "\n".join(
        (
            inspect.getsource(ProviderRuntimeOperator._lazy_rearm_locked),
            inspect.getsource(ProviderRuntimeOperator._fold_recovery_locked),
        )
    )
    assert "completion_invocation_ids" in source
    assert "self._recovery_fold(result)" in source
    manager = (REPOSITORY_ROOT / "src/cruxible_core/runtime/playbill_manager.py").read_text()
    assert "def _fold_provider_recovery(" in manager
    assert "operator.acknowledge_recovery((invocation_id,))" in manager
