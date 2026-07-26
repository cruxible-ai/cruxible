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


# The ONLY api.py functions allowed to replace a tool's static tier for a call.
# Config-declared write tiers (``write_tier``) make the direct-write facades'
# effective requirement payload-dependent: they pre-gate at the GOVERNED_WRITE
# floor (scope first, before any instance access), then check the payload's
# computed requirement via ``_direct_write_tier_gate``. Nothing else in the
# facade may adjust a tier — additions to this set need an architecture review.
#
# The feedback facades used to hold this power too (``_feedback_correction_tier_gate``,
# wi-feedback-write-tier-bypass). They no longer do: wi-feedback-approval-rail
# floors every ``correct`` at GRAPH_WRITE by ACTION, which the facade pre-gate
# could never bind more tightly than, and a facade-side denial landed
# UNRECEIPTED. The rail moved wholly into the service chokepoint
# (``_ADJUDICATION_ENFORCEMENT_SEAMS`` below) so refusals are receipted.
_TIER_OVERRIDE_FUNCTIONS = frozenset(
    {
        "add_entities",
        "add_relationships_with_provenance",
        "batch_direct_write",
        "_direct_write_tier_gate",
    }
)


def test_runtime_api_overrides_permission_tiers_only_in_direct_write_gate():
    path = _repo_root() / "src/cruxible_core/runtime/api.py"
    source = path.read_text()
    # The legacy ad-hoc override spelling stays banned outright.
    assert "required_mode=" not in source

    tree = ast.parse(source, filename=str(path))
    offenders: list[str] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "check_permission"
            ):
                continue
            # required_override adjusts a tool's tier; audit_success silences
            # the mutation_allowed record. BOTH are gate-only powers - a call
            # using either outside the sanctioned gates is an unaudited or
            # unreviewed permission path (second-review finding, 2026-07-11).
            has_gate_power = any(
                keyword.arg in ("required_override", "audit_success") for keyword in node.keywords
            )
            if has_gate_power and function.name not in _TIER_OVERRIDE_FUNCTIONS:
                offenders.append(f"{function.name}:{node.lineno}")
    assert offenders == []


def test_runtime_api_tier_branching_confined_to_the_gate():
    """Homegrown tier logic that skips check_permission entirely is invisible
    to the override test above - the old broad ban on the PermissionMode
    string caught that class by accident. Pin it deliberately: outside the
    sanctioned gate functions, api.py may not compare or branch on
    PermissionMode / get_current_mode at all."""
    path = _repo_root() / "src/cruxible_core/runtime/api.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if function.name in _TIER_OVERRIDE_FUNCTIONS:
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and node.id in ("PermissionMode", "get_current_mode"):
                offenders.append(f"{function.name}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in (
                "PermissionMode",
                "get_current_mode",
            ):
                offenders.append(f"{function.name}:{node.lineno}")
    assert offenders == []


# The service-layer files allowed to raise a tier for an ADJUDICATION act.
# Some governance requirements are properties of the PAYLOAD's action, not of
# the tool name, so the per-tool permission map cannot express them and the
# facade cannot enforce them: feedback ``approve``/``reject``/``correct``, and
# group ``resolve`` reached through the exported service function rather than
# the GRAPH_WRITE-gated tool. Both are enforced INSIDE the mutation-receipt
# scope of their service chokepoint so the refusal is receipted and rolls the
# open write transaction back. This is a deliberately short list - adding a
# file needs an architecture review, and the check must stay inside a
# ``mutation_receipt`` block (pinned by
# ``test_service_seam_tier_checks_sit_inside_a_mutation_receipt``).
_ADJUDICATION_ENFORCEMENT_SEAMS = frozenset(
    {
        "cruxible_core/service/feedback.py",
        "cruxible_core/service/group_transitions.py",
    }
)


# The service-layer files allowed to enforce a tool's STATIC tier for an
# adjudication act. Distinct from ``_ADJUDICATION_ENFORCEMENT_SEAMS`` above,
# which raises a tier via ``required_override``: these seams check the tool's
# own registered requirement, so they carry no gate power and the
# ``required_override``/``audit_success`` ban above cannot see them at all.
#
# The lifecycle verbs (``supersede``/``retract``/``retire``) sit here because
# their exported service functions are reachable by a direct library caller who
# never passes through the ``runtime/api.py`` facade — the facade gate alone
# would leave that channel untiered. The same receipted-refusal requirement
# applies and is pinned below: the check runs with the write transaction
# already open, so a denial is a receipted refusal that rolls back rather than
# a bare exception thrown before any receipt exists.
_STATIC_TIER_ADJUDICATION_SEAMS = frozenset(
    {
        "cruxible_core/service/artifact_lifecycle.py",
    }
)


def _service_files_calling_check_permission() -> set[str]:
    src_root = _repo_root() / "src"
    found: set[str] = set()
    for path in (src_root / "cruxible_core" / "service").rglob("*.py"):
        if "check_permission" in path.read_text():
            found.add(str(path.relative_to(src_root)))
    return found


def test_service_layer_permission_checks_stay_on_the_named_seams():
    """No service module enforces permissions except the reviewed seams.

    Enforcement scattered across the service layer is how a tier check ends up
    somewhere with no receipt around it. Set-equality in BOTH directions: a new
    service-layer ``check_permission`` fails here until it is named (and
    reviewed), and a seam that stops checking fails here too.
    """
    assert _service_files_calling_check_permission() == set(
        _ADJUDICATION_ENFORCEMENT_SEAMS | _STATIC_TIER_ADJUDICATION_SEAMS
    )


def test_static_tier_service_seams_check_inside_a_mutation_receipt():
    """A static-tier service seam earns its place the same way: by being receipted.

    Same contract as ``test_service_seam_tier_checks_are_reached_only_inside_a_
    mutation_receipt``, for seams that enforce a tool's registered tier instead
    of overriding it. Every ``check_permission`` call in the seam must sit
    lexically inside a ``with mutation_receipt(...)`` block, and there must be
    at least one.
    """
    src_root = _repo_root() / "src"
    offenders: list[str] = []
    for relative in sorted(_STATIC_TIER_ADJUDICATION_SEAMS):
        path = src_root / relative
        tree = ast.parse(path.read_text(), filename=str(path))

        receipted: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.With) and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "mutation_receipt"
                for item in node.items
            ):
                receipted.update(id(inner) for inner in ast.walk(node))

        checks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "check_permission"
        ]
        assert checks, f"{relative} is listed as a seam but carries no tier check"
        offenders.extend(
            f"{relative}:{node.lineno}" for node in checks if id(node) not in receipted
        )
    assert offenders == []


def test_gate_powers_never_leave_the_permission_module_and_api_gates():
    """Repo-wide: required_override / audit_success appear only in the
    permission module (definition), runtime/api.py (the facade gates), and the
    sanctioned service-layer adjudication seams - closes the
    import-check_permission-elsewhere hole the api.py-only AST walk leaves
    open."""
    src_root = _repo_root() / "src"
    allowed = {
        src_root / "cruxible_core" / "runtime" / "permissions.py",
        src_root / "cruxible_core" / "runtime" / "api.py",
    } | {src_root / relative for relative in _ADJUDICATION_ENFORCEMENT_SEAMS}
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text()
        if "required_override" in text or "audit_success" in text:
            offenders.append(str(path.relative_to(src_root)))
    assert offenders == []


def test_service_seam_tier_checks_are_reached_only_inside_a_mutation_receipt():
    """A service-seam tier check earns its exemption ONLY by being receipted.

    The whole reason the adjudication rails moved out of the facade is that a
    facade-side denial landed before any receipt existed - the changelog
    promises a receipted refusal, so the check must run with the write
    transaction already open. Pin exactly that: in each sanctioned seam, the
    helper carrying the ``required_override`` check is called ONLY from inside
    a ``with mutation_receipt(...)`` block (and is actually called at all)."""
    src_root = _repo_root() / "src"
    offenders: list[str] = []
    for relative in sorted(_ADJUDICATION_ENFORCEMENT_SEAMS):
        path = src_root / relative
        tree = ast.parse(path.read_text(), filename=str(path))

        gate_functions = {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "check_permission"
            and any(keyword.arg == "required_override" for keyword in node.keywords)
        }
        assert gate_functions, f"{relative} is listed as a seam but carries no tier check"

        receipted: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.With) and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "mutation_receipt"
                for item in node.items
            ):
                receipted.update(id(inner) for inner in ast.walk(node))

        called: set[str] = set()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in gate_functions
            ):
                continue
            called.add(node.func.id)
            if id(node) not in receipted:
                offenders.append(f"{relative}:{node.lineno} ({node.func.id})")
        assert called == gate_functions, f"{relative}: uncalled tier gate {gate_functions - called}"
    assert offenders == []


def test_feedback_channel_mutates_relationships_only():
    """The feedback channel's blast radius is relationship state only.

    The adjudication rail reasons about the ACTION, not about which types a
    payload touches, and every governance story told about feedback
    (write_tier's ``correct`` carve-out, the kill-switch scope, the
    pending-edge rail) assumes the channel cannot reach entity properties. Pin
    both halves: every feedback target is a relationship instance, and the
    channel's sole graph WRITE verb is ``update_relationship_state``. If
    feedback ever grows entity corrections, this fails loudly and the entity
    side of the governance story must be written before it is relaxed."""
    from cruxible_core.feedback.types import FeedbackBatchItem, FeedbackRecord
    from cruxible_core.graph.types import RelationshipInstance
    from cruxible_core.service.types import FeedbackItemInput, RelationshipTargetInput

    assert FeedbackRecord.model_fields["target"].annotation is RelationshipInstance
    assert FeedbackBatchItem.model_fields["target"].annotation is RelationshipInstance
    assert FeedbackItemInput.__dataclass_fields__["target"].type in (
        RelationshipTargetInput,
        "RelationshipTargetInput",
    )

    allowed_graph_verbs = {
        # Reads used to normalize/snapshot feedback context.
        "get_relationship",
        "relationship_count_between",
        "get_entity",
        # The single write verb of the channel: edge state only.
        "update_relationship_state",
    }
    for module in (
        "src/cruxible_core/feedback/applier.py",
        "src/cruxible_core/service/feedback.py",
    ):
        path = _repo_root() / module
        tree = ast.parse(path.read_text(), filename=str(path))
        used = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "graph"
        }
        unexpected = sorted(used - allowed_graph_verbs)
        assert used <= allowed_graph_verbs, f"{module}: unexpected graph verbs {unexpected}"


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
