"""Architecture boundary tests for the runtime refactor."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import get_args

from cruxible_client import CruxibleClient
from cruxible_client import contracts as client_contracts
from cruxible_core.cli.instance import CruxibleInstance as CliCruxibleInstance
from cruxible_core.client import CruxibleClient as CoreCompatClient
from cruxible_core.config.schema import StepKind
from cruxible_core.mcp import contracts as core_contracts
from cruxible_core.mcp import handlers
from cruxible_core.mcp import permissions as mcp_permissions
from cruxible_core.mcp.handlers import get_manager as handler_get_manager
from cruxible_core.runtime import api
from cruxible_core.runtime import permissions as runtime_permissions
from cruxible_core.runtime.instance import CruxibleInstance as RuntimeCruxibleInstance
from cruxible_core.runtime.instance_manager import get_manager as runtime_get_manager
from cruxible_core.workflow.step_handlers import DEFAULT_STEP_HANDLER_REGISTRY


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_mcp_handlers_get_manager_returns_canonical_runtime_singleton():
    assert handler_get_manager() is runtime_get_manager()


def test_cli_instance_re_exports_runtime_class_object():
    assert CliCruxibleInstance is RuntimeCruxibleInstance


def test_mcp_local_wrappers_delegate_to_runtime_api(monkeypatch):
    sentinel = client_contracts.EvaluateResult(
        entity_count=1,
        edge_count=2,
        findings=[],
        summary={},
        quality_summary={},
    )

    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr(api, "evaluate", lambda *args, **kwargs: sentinel)

    assert handlers.handle_evaluate("instance-id") is sentinel


def test_server_routes_do_not_import_mcp_handlers():
    routes_dir = _repo_root() / "src/cruxible_core/server/routes"
    for path in routes_dir.glob("*.py"):
        source = path.read_text()
        assert "from cruxible_core.mcp.handlers import" not in source, str(path)


def test_runtime_and_server_do_not_import_mcp_permissions():
    src_root = _repo_root() / "src/cruxible_core"
    checked_dirs = [src_root / "runtime", src_root / "server"]
    for directory in checked_dirs:
        for path in directory.rglob("*.py"):
            source = path.read_text()
            assert "cruxible_core.mcp.permissions" not in source, str(path)


def test_src_does_not_call_runtime_private_api_handlers():
    src_root = _repo_root() / "src/cruxible_core"
    for path in src_root.rglob("*.py"):
        source = path.read_text()
        assert "api._handle_" not in source, str(path)


def test_runtime_api_defines_no_private_handle_functions():
    path = _repo_root() / "src/cruxible_core/runtime/api.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_handle_")
    ]
    assert names == []


def test_runtime_api_does_not_own_config_materialization():
    path = _repo_root() / "src/cruxible_core/runtime/api.py"
    source = path.read_text()
    forbidden = [
        "cruxible_core.config.composer",
        "cruxible_core.config.loader",
        "cruxible_core.kits",
    ]
    for import_path in forbidden:
        assert import_path not in source, import_path


def test_runtime_api_does_not_construct_group_domain_models():
    path = _repo_root() / "src/cruxible_core/runtime/api.py"
    source = path.read_text()
    assert "cruxible_core.group.types" not in source


def test_runtime_api_does_not_construct_graph_or_feedback_domain_models():
    path = _repo_root() / "src/cruxible_core/runtime/api.py"
    source = path.read_text()
    forbidden = [
        "cruxible_core.feedback.types",
        "cruxible_core.graph.types",
    ]
    for import_path in forbidden:
        assert import_path not in source, import_path


def test_runtime_api_does_not_override_permission_tiers():
    path = _repo_root() / "src/cruxible_core/runtime/api.py"
    source = path.read_text()
    assert "required_mode=" not in source
    assert "PermissionMode" not in source


def test_runtime_api_scoped_permission_checks_include_instance_id():
    path = _repo_root() / "src/cruxible_core/runtime/api.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    missing: list[str] = []

    for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
        has_instance_arg = any(arg.arg == "instance_id" for arg in function.args.args)
        if not has_instance_arg:
            continue

        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "check_permission"
            ):
                continue
            has_instance_keyword = any(keyword.arg == "instance_id" for keyword in node.keywords)
            if not has_instance_keyword:
                missing.append(f"{function.name}:{node.lineno}")

    assert missing == []


def test_mcp_permission_exports_point_at_runtime_policy():
    assert mcp_permissions.PermissionMode is runtime_permissions.PermissionMode
    assert mcp_permissions.check_permission is runtime_permissions.check_permission


def test_service_modules_do_not_import_cli_instance():
    service_dir = _repo_root() / "src/cruxible_core/service"
    for path in service_dir.glob("*.py"):
        source = path.read_text()
        assert "from cruxible_core.cli.instance import" not in source, str(path)


def test_client_package_does_not_import_core_modules():
    client_dir = _repo_root() / "packages/cruxible-client/src/cruxible_client"
    for path in client_dir.rglob("*.py"):
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        imports_core = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports_core = any(
                    alias.name == "cruxible_core" or alias.name.startswith("cruxible_core.")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                imports_core = node.module == "cruxible_core" or (
                    node.module is not None and node.module.startswith("cruxible_core.")
                )
            if imports_core:
                break
        assert not imports_core, str(path)


def test_compatibility_re_exports_point_at_client_package():
    assert CoreCompatClient is CruxibleClient
    assert core_contracts.ValidateResult is client_contracts.ValidateResult


def test_core_and_client_package_versions_are_locked_together():
    root_pyproject = tomllib.loads((_repo_root() / "pyproject.toml").read_text())
    client_pyproject = tomllib.loads(
        (_repo_root() / "packages/cruxible-client/pyproject.toml").read_text()
    )

    core_version = root_pyproject["project"]["version"]
    client_version = client_pyproject["project"]["version"]
    dependencies = root_pyproject["project"]["dependencies"]

    assert core_version == client_version
    assert f"cruxible-client=={client_version}" in dependencies


def test_workflow_executor_uses_step_handler_registry():
    path = _repo_root() / "src/cruxible_core/workflow/executor.py"
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    execute_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_workflow"
    )
    direct_kind_checks = [
        node.lineno
        for node in ast.walk(execute_fn)
        if isinstance(node, ast.Compare) and _compares_compiled_step_kind(node)
    ]

    assert direct_kind_checks == []
    assert "DEFAULT_STEP_HANDLER_REGISTRY.execute" in source
    assert set(DEFAULT_STEP_HANDLER_REGISTRY.registered_kinds) == set(get_args(StepKind))


def test_governance_internals_do_not_import_surface_or_presentation_layers() -> None:
    src_root = _repo_root() / "src/cruxible_core"
    service_dir = src_root / "service"
    paths = {
        *(src_root / "group").rglob("*.py"),
        service_dir / "groups.py",
        *service_dir.glob("group_*.py"),
    }
    forbidden_prefixes = (
        "cruxible_core.cli",
        "cruxible_core.client",
        "cruxible_core.mcp",
        "cruxible_core.server",
        "cruxible_core.wiki",
        "cruxible_core.presentation",
        "cruxible_core.presentations",
    )
    violations: list[str] = []

    for path in sorted(paths):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules = [node.module]
            for module in imported_modules:
                if any(
                    module == prefix or module.startswith(f"{prefix}.")
                    for prefix in forbidden_prefixes
                ):
                    line_number = getattr(node, "lineno", 0)
                    violations.append(f"{path.relative_to(_repo_root())}:{line_number}:{module}")

    assert violations == []


def test_governance_does_not_reintroduce_relationship_identity_wrappers() -> None:
    src_root = _repo_root() / "src/cruxible_core"
    service_dir = src_root / "service"
    paths = {
        *(src_root / "group").rglob("*.py"),
        service_dir / "groups.py",
        *service_dir.glob("group_*.py"),
    }
    forbidden_class_names = {
        "RelationshipIdentity",
        "RelationshipKey",
        "RelationshipRef",
    }
    violations: list[str] = []

    for path in sorted(paths):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in forbidden_class_names:
                violations.append(f"{path.relative_to(_repo_root())}:{node.lineno}:{node.name}")

    assert violations == []


def _compares_compiled_step_kind(node: ast.Compare) -> bool:
    return any(
        _is_compiled_step_kind_ref(expression) for expression in [node.left, *node.comparators]
    )


def _is_compiled_step_kind_ref(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Attribute)
        and expression.attr == "kind"
        and isinstance(expression.value, ast.Name)
        and expression.value.id == "compiled_step"
    )
