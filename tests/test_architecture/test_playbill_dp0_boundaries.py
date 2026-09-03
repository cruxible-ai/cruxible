"""DP-0B guardrails for the Playbill-only public and dependency surfaces."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
CORE = SRC / "cruxible_core"
GOLDENS = ROOT / "tests" / "goldens" / "playbill"
REVIEW_GUIDE = ROOT / "docs" / "dp0-review-guide.md"

FACADE = CORE / "runtime" / "playbill_api.py"
HTTP_ROUTES = CORE / "server" / "routes" / "playbill.py"
MCP_HANDLERS = CORE / "mcp" / "handlers.py"

ORACLE_COMMITS = {
    "family_1": "e3fe35b360d098f14a5d59bf770ffee401224f0c",
    "procedure_graph_program": "986307d56649eb51747ca227228fbe19f73e3895",
}

SERVED_ROOTS = (
    "cruxible_core.cli.main",
    "cruxible_core.cli.commands._common",
    "cruxible_core.cli.commands.context",
    "cruxible_core.cli.commands.credentials",
    "cruxible_core.cli.commands.playbill",
    "cruxible_core.cli.commands.server",
    "cruxible_core.mcp.handlers",
    "cruxible_core.mcp.server",
    "cruxible_core.mcp.tools",
    "cruxible_core.runtime.host_api",
    "cruxible_core.runtime.playbill_api",
    "cruxible_core.runtime.playbill_manager",
    "cruxible_core.server.app",
    "cruxible_core.server.auth",
    "cruxible_core.server.actor_identity",
    "cruxible_core.server.playbill_request_models",
    "cruxible_core.server.routes.hosted_instances",
    "cruxible_core.server.routes.instances",
    "cruxible_core.server.routes.playbill",
    "cruxible_core.server.routes.runtime_credentials",
    "cruxible_core.playbill.service.documents",
    "cruxible_core.playbill.service.explain",
    "cruxible_core.playbill.service.review",
    "cruxible_core.playbill.service.source_catalog",
)

FORBIDDEN_MODULE_PREFIXES = (
    "cruxible_core.runtime.api",
    "cruxible_core.runtime.instance",
    "cruxible_core.runtime.instance_manager",
    "cruxible_core.instance_protocol",
    "cruxible_core.graph",
    "cruxible_core.procedure",
    "cruxible_core.query",
    "cruxible_core.predicate",
    "cruxible_core.receipt_tree",
    "cruxible_core.workflow",
    "cruxible_core.provider",
    "cruxible_core.providers",
    "cruxible_core.client",
    "cruxible_core.governance",
    "cruxible_core.config.schema",
    "cruxible_core.service.mutations",
    "cruxible_core.service.execution",
    "cruxible_core.storage.sqlite",
    "cruxible_core.snapshot",
    "cruxible_core.telemetry",
    "cruxible_core.transport",
    "cruxible_core.working_set",
)


def _module_path(module: str) -> Path | None:
    relative = Path(*module.split("."))
    file_path = SRC / relative.with_suffix(".py")
    if file_path.is_file():
        return file_path
    package_path = SRC / relative / "__init__.py"
    return package_path if package_path.is_file() else None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            result.add(node.module)
            for alias in node.names:
                candidate = f"{node.module}.{alias.name}"
                if _module_path(candidate) is not None:
                    result.add(candidate)
    with_parents = set(result)
    for module in result:
        parts = module.split(".")
        with_parents.update(".".join(parts[:index]) for index in range(1, len(parts)))
    return with_parents


def _dependency_closure(roots: tuple[str, ...]) -> set[str]:
    pending = list(roots)
    visited: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited or not module.startswith("cruxible_core"):
            continue
        visited.add(module)
        path = _module_path(module)
        if path is None:
            continue
        pending.extend(_imports(path) - visited)
    return visited


def _playbill_facade_calls(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "playbill_api"
        and node.func.attr.startswith("playbill_")
    }
    return tuple(sorted(calls))


def _fenced_inventory(document: str, heading: str) -> set[str]:
    section = document.split(f"## {heading}\n", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    block = section.split("```text\n", maxsplit=1)[1].split("\n```", maxsplit=1)[0]
    return {line.strip() for line in block.splitlines() if line.strip()}


def _facade_operations() -> tuple[str, ...]:
    tree = ast.parse(FACADE.read_text(encoding="utf-8"), filename=str(FACADE))
    return tuple(
        sorted(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("playbill_")
        )
    )


def test_playbill_served_dependency_closure_excludes_legacy_core() -> None:
    closure = _dependency_closure(SERVED_ROOTS)
    violations = sorted(
        module
        for module in closure
        if any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in FORBIDDEN_MODULE_PREFIXES
        )
    )
    assert violations == []


def test_importing_playbill_http_surface_does_not_initialize_legacy_core() -> None:
    check = (
        "import sys; import cruxible_core.server.routes.playbill; "
        f"prefixes={FORBIDDEN_MODULE_PREFIXES!r}; "
        "bad=sorted(m for m in sys.modules if any(m==p or m.startswith(p+'.') "
        "for p in prefixes)); "
        "assert not bad, bad"
    )
    subprocess.run(
        [sys.executable, "-c", check],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_http_and_mcp_playbill_calls_delegate_to_the_dedicated_facade() -> None:
    expected = json.loads((GOLDENS / "served-surface-dp0b-v1.json").read_text(encoding="utf-8"))
    facade = _facade_operations()
    http = _playbill_facade_calls(HTTP_ROUTES)
    mcp = _playbill_facade_calls(MCP_HANDLERS)

    assert list(facade) == expected["facade_operations"]
    assert http == facade
    assert len(http) == expected["http_delegate_count"]
    assert set(mcp) <= set(facade)
    assert len(mcp) == expected["mcp_delegate_count"]
    assert "from cruxible_core.runtime import api\n" not in HTTP_ROUTES.read_text(encoding="utf-8")


def test_legacy_public_modules_and_mixed_runtime_facade_are_absent() -> None:
    deleted_cli = {
        "attestations.py",
        "config_views.py",
        "decision_records.py",
        "feedback.py",
        "gates.py",
        "groups.py",
        "instances.py",
        "kits.py",
        "lifecycle_verbs.py",
        "lists.py",
        "mutations.py",
        "outcome_contracts.py",
        "procedures.py",
        "read_stats.py",
        "reads.py",
        "source_artifacts.py",
        "state.py",
        "telemetry.py",
        "workflows.py",
        "working_set.py",
    }
    deleted_routes = {
        "attestations.py",
        "bindings.py",
        "decision_records.py",
        "feedback.py",
        "gates.py",
        "groups.py",
        "installs.py",
        "mutations.py",
        "outcome_contracts.py",
        "procedures.py",
        "queries.py",
        "snapshots.py",
        "source_artifacts.py",
        "state.py",
        "telemetry.py",
        "workflows.py",
    }
    assert not (CORE / "runtime" / "api.py").exists()
    assert all(not (CORE / "cli" / "commands" / name).exists() for name in deleted_cli)
    assert all(not (CORE / "server" / "routes" / name).exists() for name in deleted_routes)


def test_dp0c_deleted_products_are_absent() -> None:
    """DP-0C products leave no compatibility package or orphaned test surface."""
    for package in (
        "bindings",
        "blueprint",
        "canonical_views",
        "decision",
        "feedback",
        "installs",
        "kit_distribution",
        "snapshot",
        "telemetry",
        "transport",
        "ui_static",
    ):
        package_root = CORE / package
        assert not any(
            path.is_file() and "__pycache__" not in path.parts for path in package_root.rglob("*")
        )

    for service_module in (
        "analysis.py",
        "bindings.py",
        "config_mutations.py",
        "decisions.py",
        "feedback.py",
        "installs.py",
        "snapshots.py",
        "state.py",
        "state_diff.py",
        "telemetry.py",
        "views.py",
    ):
        assert not (CORE / "service" / service_module).exists()
    for family in (
        "test_bindings",
        "test_blueprint",
        "test_decision",
        "test_feedback",
        "test_installs",
        "test_snapshot",
        "test_telemetry",
        "test_transport",
        "test_ui_static",
    ):
        family_root = ROOT / "tests" / family
        assert not any(
            path.is_file() and "__pycache__" not in path.parts for path in family_root.rglob("*")
        )
    assert not (ROOT / "docs" / "blueprints.md").exists()
    assert not (ROOT / "docs" / "local-state-and-backups.md").exists()
    assert not (ROOT / "docs" / "publishing-states.md").exists()
    assert not (ROOT / "docs" / "state-resolution-and-maintenance.md").exists()
    assert not (CORE / "cli" / "formatting.py").exists()
    assert not (CORE / "working_set.py").exists()
    assert not (CORE / "runtime" / "instance_manager.py").exists()
    assert not (CORE / "mcp" / "kit_surface.py").exists()
    assert not (CORE / "server" / "telemetry.py").exists()
    assert not (ROOT / "tests" / "test_working_set_capture.py").exists()
    assert not any(
        path.is_file() and "__pycache__" not in path.parts for path in (ROOT / "kits").rglob("*")
    )
    for script in (
        "build_kit_bundles.py",
        "check_kit_lockfiles.py",
        "check_kit_release_assets.py",
        "generate_kit_docs.py",
    ):
        assert not (ROOT / "scripts" / script).exists()
    assert (
        ROOT / "tests" / "data" / "playbill_parity" / "modeling-parity-oracle-v1.json"
    ).is_file()


def test_public_registration_catalogs_are_playbill_only() -> None:
    from mcp.server.fastmcp import FastMCP

    from cruxible_client import CruxibleClient
    from cruxible_core.cli.main import CLI_COMMANDS
    from cruxible_core.mcp.tools import register_tools
    from cruxible_core.runtime.permissions import TOOL_PERMISSIONS

    assert set(CLI_COMMANDS) == {"context", "credential", "playbill", "server"}
    registered_tools = set(register_tools(FastMCP("dp0b-registration-inventory")))
    assert registered_tools == set(TOOL_PERMISSIONS)
    assert set(TOOL_PERMISSIONS) == {
        "cruxible_version",
        "cruxible_server_info",
        "cruxible_playbill_host_create",
        "cruxible_playbill_init",
        "cruxible_playbill_store_body",
        "cruxible_playbill_propose_document",
        "cruxible_playbill_inspect_proposal",
        "cruxible_playbill_inspect_refusal",
        "cruxible_playbill_review",
        "cruxible_playbill_prepare_approval",
        "cruxible_playbill_submit_approval",
        "cruxible_playbill_activate",
        "cruxible_playbill_authoring_abandon_insertion",
        "cruxible_playbill_authoring_bind",
        "cruxible_playbill_authoring_compile",
        "cruxible_playbill_authoring_confirm_insertion",
        "cruxible_playbill_authoring_prepare_publication",
        "cruxible_playbill_authoring_create",
        "cruxible_playbill_authoring_example",
        "cruxible_playbill_authoring_get",
        "cruxible_playbill_authoring_list_pending",
        "cruxible_playbill_authoring_preflight",
        "cruxible_playbill_authoring_resume",
        "cruxible_playbill_authoring_status",
        "cruxible_playbill_authoring_submit",
        "cruxible_playbill_list_documents",
        "cruxible_playbill_get_document",
        "cruxible_playbill_dereference",
        "cruxible_playbill_history",
        "cruxible_playbill_explain",
        "cruxible_playbill_source_context",
        "cruxible_playbill_check_source_bundle",
        "cruxible_playbill_propose_source_bundle",
        "cruxible_playbill_list_principals",
        "cruxible_playbill_propose_principal_change",
        "cruxible_playbill_propose_subject",
        "cruxible_playbill_list_subjects",
        "cruxible_playbill_get_subject",
        "cruxible_playbill_subject_history",
        "cruxible_playbill_propose_claim_type",
        "cruxible_playbill_list_claim_types",
        "cruxible_playbill_get_claim_type",
        "cruxible_playbill_claim_retire",
        "cruxible_playbill_claim_attest",
        "cruxible_playbill_claim_attest_new_capture",
        "cruxible_playbill_list_claims",
        "cruxible_playbill_get_claim",
        "cruxible_playbill_claim_history",
        "cruxible_playbill_explain_claim",
        "cruxible_playbill_propose_query_definition",
        "cruxible_playbill_list_query_definitions",
        "cruxible_playbill_policies_in_force",
        "cruxible_playbill_get_query_definition",
        "cruxible_playbill_run_query",
        "cruxible_playbill_procedure_readiness",
        "cruxible_playbill_procedure_bind",
        "cruxible_playbill_procedure_run",
        "cruxible_playbill_procedure_run_status",
        "cruxible_playbill_discover",
        "cruxible_playbill_expand",
        "cruxible_playbill_export_floor",
        "cruxible_playbill_resolve_coverage",
        "cruxible_playbill_workspace_coverage_resolve",
        "cruxible_playbill_workspace_coverage_status",
        "cruxible_playbill_workspace_floor_export",
        "cruxible_playbill_workspace_floor_status",
        "cruxible_playbill_workspace_source_check",
        "cruxible_playbill_workspace_source_compile",
        "cruxible_playbill_proposal_list",
        "cruxible_playbill_proposal_readmit",
        "cruxible_playbill_claim_type_migrate",
        "cruxible_playbill_seed_plan",
        "cruxible_playbill_search",
        "cruxible_playbill_since",
        "cruxible_playbill_audit",
        "cruxible_playbill_curation_list",
        "cruxible_playbill_curation_overrule",
        "cruxible_playbill_curation_accept_fixed",
        "cruxible_playbill_curation_suppress",
        "cruxible_playbill_whoami",
    }
    public_client_methods = {
        name
        for name, value in vars(CruxibleClient).items()
        if callable(value) and not name.startswith("_")
    }
    assert public_client_methods == {
        "close",
        "version",
        "server_info",
        "server_restart",
        "create_playbill_host",
        "show_playbill_host",
        "playbill_host_workspace_registration",
        "claim_runtime_bootstrap",
        "create_runtime_credential",
        "list_runtime_credentials",
        "revoke_runtime_credential",
        "rotate_runtime_credential",
        "init_playbill",
        "store_playbill_body",
        "propose_playbill_document",
        "propose_playbill_principal_change",
        "list_playbill_principals",
        "inspect_playbill_proposal",
        "inspect_playbill_refusal",
        "review_playbill_proposal",
        "prepare_playbill_approval",
        "submit_playbill_approval",
        "approve_playbill_proposal",
        "activate_playbill_proposal",
        "abandon_playbill_authoring_insertion",
        "compile_playbill_authoring",
        "compile_playbill_authoring_input",
        "confirm_playbill_authoring_insertion",
        "prepare_playbill_authoring_publication",
        "create_playbill_authoring_intent",
        "create_playbill_authoring_input",
        "get_playbill_authoring_intent",
        "list_pending_playbill_authoring_intents",
        "playbill_authoring_intent_status",
        "preflight_playbill_authoring_intent",
        "rebase_playbill_authoring_intent",
        "resume_playbill_authoring_intent",
        "submit_playbill_authoring_intent",
        "list_playbill_documents",
        "get_playbill_document",
        "dereference_playbill_document",
        "playbill_document_history",
        "explain_playbill_subject",
        "playbill_source_context",
        "check_playbill_source_bundle",
        "propose_playbill_source_bundle",
        "propose_playbill_subject",
        "list_playbill_subjects",
        "get_playbill_subject",
        "playbill_subject_history",
        "propose_playbill_claim_type",
        "propose_playbill_claim_type_input",
        "list_playbill_claim_types",
        "get_playbill_claim_type",
        "retire_playbill_claim",
        "append_playbill_claim_attestation",
        "recover_playbill_claim_attestations",
        "list_playbill_claims",
        "get_playbill_claim",
        "playbill_claim_history",
        "explain_playbill_claim",
        "propose_playbill_query_definition",
        "list_playbill_query_definitions",
        "list_playbill_policies_in_force",
        "get_playbill_query_definition",
        "run_playbill_query",
        "playbill_procedure_readiness",
        "bind_playbill_procedure",
        "run_playbill_procedure",
        "get_playbill_procedure_run",
        "discover_playbill",
        "expand_playbill",
        "export_playbill_floor",
        "resolve_playbill_coverage",
        "read_playbill_block_sync_backing",
        "list_playbill_proposals",
        "resolve_playbill_proposal_selector",
        "playbill_whoami",
        "readmit_playbill_proposal",
        "migrate_playbill_claim_type",
        "next_playbill",
        "audit_playbill",
        "list_playbill_curation",
        "overrule_playbill_curation",
        "accept_fixed_playbill_curation",
        "suppress_playbill_curation",
        "search_playbill",
        "since_playbill",
    }


def test_unserved_donor_operations_retain_permissions_without_public_registration() -> None:
    from cruxible_core.runtime.permissions import (
        DONOR_OPERATION_PERMISSIONS,
        PERMISSION_REQUIREMENTS,
        RUNTIME_OPERATION_PERMISSIONS,
        TOOL_PERMISSIONS,
        PermissionMode,
    )

    assert DONOR_OPERATION_PERMISSIONS == {
        "cruxible_feedback_adjudicate": PermissionMode.GRAPH_WRITE,
        "cruxible_resolve_group": PermissionMode.GRAPH_WRITE,
        "cruxible_supersede_claim": PermissionMode.GRAPH_WRITE,
        "cruxible_retract_claim": PermissionMode.GRAPH_WRITE,
        "cruxible_supersede_entity": PermissionMode.GRAPH_WRITE,
        "cruxible_retire_entity": PermissionMode.GRAPH_WRITE,
    }
    assert set(DONOR_OPERATION_PERMISSIONS).isdisjoint(TOOL_PERMISSIONS)
    assert set(DONOR_OPERATION_PERMISSIONS).isdisjoint(RUNTIME_OPERATION_PERMISSIONS)
    assert all(
        PERMISSION_REQUIREMENTS[name] == tier for name, tier in DONOR_OPERATION_PERMISSIONS.items()
    )


def test_pc_d_retired_donor_packages_and_modules_are_absent() -> None:
    for package in ("group", "kits"):
        root = CORE / package
        assert not any(
            path.is_file() and "__pycache__" not in path.parts for path in root.rglob("*")
        )

    retired_modules = (
        CORE / "procedure" / "migration.py",
        CORE / "procedure" / "reading_store.py",
        CORE / "procedure" / "store.py",
        CORE / "workflow" / "apply.py",
        CORE / "workflow" / "proposal_preview.py",
        CORE / "workflow" / "proposals.py",
        CORE / "service" / "execution.py",
        CORE / "service" / "group_read_models.py",
        CORE / "service" / "group_transitions.py",
        CORE / "service" / "groups.py",
        CORE / "service" / "lifecycle.py",
        CORE / "service" / "procedure_migrations.py",
        CORE / "service" / "procedures.py",
    )
    assert not any(path.exists() for path in retired_modules)

    # PC-D's residual law was that the retained mutation service could not
    # reach back into the group donor. PC-F deleted the mutation service
    # itself, which subsumes it.
    assert not (CORE / "service" / "mutations.py").exists()


def test_pc_e1_retired_storage_authorities_and_executor_are_absent() -> None:
    for package in ("receipt", "resolution_contracts"):
        root = CORE / package
        assert not any(
            path.is_file() and "__pycache__" not in path.parts for path in root.rglob("*")
        )

    retired_modules = (
        CORE / "service" / "resolution_contracts.py",
        CORE / "service" / "artifact_lifecycle.py",
        CORE / "service" / "gates.py",
        CORE / "service" / "mutation_receipts.py",
        CORE / "storage" / "resolution_evidence.py",
        CORE / "workflow" / "execution_context.py",
        CORE / "workflow" / "executor.py",
        CORE / "workflow" / "io.py",
        CORE / "workflow" / "step_handlers.py",
        CORE / "workflow" / "tracing.py",
    )
    assert not any(path.exists() for path in retired_modules)

    # PC-E1's residual laws named store classes, protocols, and accessors that
    # had to stay absent from three retained donor harnesses. PC-F deleted all
    # three harnesses, which subsumes every one of those substring checks.
    for retired_harness in (
        CORE / "storage" / "sqlite.py",
        CORE / "instance_protocol.py",
        CORE / "runtime" / "instance.py",
    ):
        assert not retired_harness.exists()


def test_pc_del1_retired_old_core_families_are_absent() -> None:
    """The old schema, graph, Procedure, query, receipt, and workflow island is gone."""
    for module in (
        CORE / "instance_protocol.py",
        CORE / "sqlite_ddl.py",
        CORE / "cli" / "instance.py",
        CORE / "runtime" / "instance.py",
        CORE / "server" / "auth_managed_entities.py",
        CORE / "storage" / "protocols.py",
        CORE / "storage" / "sqlite.py",
        CORE / "config" / "composition_ownership.py",
        CORE / "config" / "ownership.py",
        CORE / "config" / "provenance.py",
    ):
        assert not module.exists(), module

    for service_module in (
        "direct_write_policy.py",
        "evidence.py",
        "lifecycle_inputs.py",
        "mutation_guards.py",
        "mutation_proposals.py",
        "mutation_transactions.py",
        "mutations.py",
        "queries.py",
        "server.py",
        "types.py",
        "playbill_documents.py",
        "playbill_explain.py",
        "playbill_review.py",
        "playbill_source_catalog.py",
    ):
        assert not (CORE / "service" / service_module).exists(), service_module

    for package in (
        "client",
        "config",
        "governance",
        "graph",
        "procedure",
        "provider",
        "providers",
        "query",
        "receipt_tree",
        "workflow",
    ):
        root = CORE / package
        assert not any(
            path.is_file() and "__pycache__" not in path.parts for path in root.rglob("*")
        ), package
    assert not (CORE / "workflow_execution_types.py").exists()


def test_pc_f2_coverage_delivery_adds_no_authority() -> None:
    """Coverage points at accepted state; it may never reach a write path.

    Transitive closure is the wrong instrument here -- every read model in the
    substrate eventually imports the acceptance kernel through `claims` -- so
    the checkable statement is the one that actually constrains the batch: the
    coverage modules import from an explicit read-side allowlist and from
    nothing else, so there is no name in scope through which a resolver could
    propose, compile, accept, activate, settle, or touch the ledger.
    """

    package = CORE / "playbill" / "coverage"
    modules = sorted(path.stem for path in package.glob("*.py"))
    assert modules == [
        "__init__",
        "adapter",
        "claude_code",
        "contracts",
        "indexes",
        "manifest",
        "middleware",
        "render",
        "resolver",
        "workspace",
    ]

    permitted = {
        "cruxible_core",
        "cruxible_core.playbill",
        "cruxible_client.contracts.artifacts",
        "cruxible_client.contracts.canonical",
        "cruxible_client.contracts.captures",
        "cruxible_client.contracts.claims",
        "cruxible_client.contracts.claim_verdicts",
        "cruxible_core.playbill.coverage",
        "cruxible_core.playbill.coverage.adapter",
        "cruxible_core.playbill.coverage.contracts",
        "cruxible_core.playbill.coverage.indexes",
        "cruxible_core.playbill.coverage.manifest",
        "cruxible_core.playbill.coverage.middleware",
        "cruxible_core.playbill.coverage.render",
        "cruxible_core.playbill.coverage.resolver",
        "cruxible_core.playbill.coverage.workspace",
        "cruxible_client.contracts.discovery",
        "cruxible_client.contracts.errors",
        "cruxible_core.playbill.projection",
        "cruxible_core.playbill.query",
        "cruxible_client.contracts.query.grammar",
        "cruxible_core.playbill.query.semantic_discovery",
        "cruxible_client.contracts.semantic",
        "cruxible_client.contracts.source_references",
    }
    forbidden = (
        "cruxible_core.playbill.activation",
        "cruxible_core.playbill.compiler",
        "cruxible_core.playbill.git",
        "cruxible_core.playbill.instance",
        "cruxible_core.playbill.proposals",
        "cruxible_core.playbill.service",
        "cruxible_core.playbill.settlement",
        "cruxible_core.server",
        "cruxible_core.service",
        "cruxible_core.storage",
    )

    imported: set[str] = set()
    for path in package.glob("*.py"):
        imported.update(module for module in _imports(path) if module.startswith("cruxible_core"))
    assert sorted(imported - permitted) == []
    assert sorted(module for module in imported if module in forbidden) == []


def test_destructive_pass_oracles_are_exact_and_immutable() -> None:
    metadata = json.loads((GOLDENS / "oracles-v1.json").read_text(encoding="utf-8"))
    assert metadata["format"] == "playbill-oracles-v1"
    for name, commit in ORACLE_COMMITS.items():
        assert metadata[name]["commit"] == commit
        assert len(commit) == 40
        assert commit == commit.lower()
        assert all(character in "0123456789abcdef" for character in commit)


def test_dp0_review_guide_matches_surviving_inventories() -> None:
    from cruxible_core.cli.main import cli
    from cruxible_core.runtime.permissions import TOOL_PERMISSIONS
    from cruxible_core.server.app import create_app

    document = REVIEW_GUIDE.read_text(encoding="utf-8")

    cli_leaves: set[str] = set()

    def collect_leaves(command: object, path: tuple[str, ...]) -> None:
        children = getattr(command, "commands", None)
        if children:
            for name, child in children.items():
                collect_leaves(child, (*path, name))
            return
        cli_leaves.add(" ".join(path))

    collect_leaves(cli, ())
    assert _fenced_inventory(document, "Surviving public command inventory") == cli_leaves
    assert _fenced_inventory(document, "Surviving public MCP tool inventory") == set(
        TOOL_PERMISSIONS
    )

    documented_routes = {
        tuple(line.split(maxsplit=1))
        for line in _fenced_inventory(document, "Surviving public route inventory")
    }
    served_routes = {
        (method, route.path)
        for route in create_app().routes
        if getattr(route, "include_in_schema", False)
        for method in route.methods
    }
    assert documented_routes == served_routes

    tracked_goldens = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests" / "goldens").rglob("*")
        if path.is_file()
    }
    assert _fenced_inventory(document, "Exact frozen goldens retained") == tracked_goldens

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    retained_requirements = list(project["dependencies"])
    for extra in ("mcp",):
        retained_requirements.extend(project["optional-dependencies"][extra])
    for requirement in retained_requirements:
        name = Requirement(requirement).name
        assert re.search(rf"\| `{re.escape(name)}`(?: \([^|]+\))? \|", document, re.IGNORECASE)
