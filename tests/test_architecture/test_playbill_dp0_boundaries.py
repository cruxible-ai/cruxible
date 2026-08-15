"""DP-0A guardrails for the Playbill-only served dependency path."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from cruxible_core.playbill.donors.manifest import DONOR_MANIFEST, donor_for

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
CORE = SRC / "cruxible_core"
GOLDENS = ROOT / "tests" / "goldens" / "playbill"

FACADE = CORE / "runtime" / "playbill_api.py"
HTTP_ROUTES = CORE / "server" / "routes" / "playbill.py"
MCP_HANDLERS = CORE / "mcp" / "handlers.py"

ORACLE_COMMITS = {
    "family_1": "e3fe35b360d098f14a5d59bf770ffee401224f0c",
    "procedure_graph_program": "986307d56649eb51747ca227228fbe19f73e3895",
}

RATIFIED_DONOR_REMOVAL_BATCHES = {
    "cruxible_core.procedure": "PC-E2",
    "cruxible_core.workflow": "PC-E2",
    "cruxible_core.config.schema": "PC-F",
    "cruxible_core.predicate": "PC-F",
    "cruxible_core.query": "PC-F",
    "cruxible_core.graph": "PC-F",
    "cruxible_core.receipt": "PC-E1",
    "cruxible_core.attestation": "PC-C",
    "cruxible_core.resolution_contracts": "PC-E1",
    "cruxible_core.source_artifacts.markdown": "PC-C",
    "cruxible_core.provider": "PC-E2",
    "cruxible_core.providers": "PC-E2",
    "cruxible_core.group": "PC-D",
    "cruxible_core.kits": "PC-D",
    "cruxible_core.runtime.instance": "PC-F",
    "cruxible_core.storage.sqlite": "PC-F",
    "cruxible_core.instance_protocol": "PC-F",
    "cruxible_core.governance.actors": "PC-A1",
}

SERVED_ROOTS = (
    "cruxible_core.runtime.playbill_api",
    "cruxible_core.runtime.playbill_manager",
    "cruxible_core.server.actor_identity",
    "cruxible_core.server.playbill_request_models",
    "cruxible_core.server.routes.playbill",
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
    "cruxible_core.config.schema",
    "cruxible_core.service.mutations",
    "cruxible_core.service.execution",
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

    donor_violations: list[str] = []
    for importer in sorted(closure):
        path = _module_path(importer)
        if path is None:
            continue
        for imported in _imports(path):
            donor = donor_for(imported)
            is_donor_package_initializer = donor is not None and donor.module_prefix.startswith(
                f"{importer}."
            )
            if donor is not None and importer != donor.adapter and not is_donor_package_initializer:
                donor_violations.append(f"{importer} -> {imported}")
    assert donor_violations == []


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
    expected = json.loads((GOLDENS / "served-surface-dp0a-v1.json").read_text(encoding="utf-8"))
    facade = _facade_operations()
    http = _playbill_facade_calls(HTTP_ROUTES)
    mcp = _playbill_facade_calls(MCP_HANDLERS)

    assert list(facade) == expected["facade_operations"]
    assert http == facade
    assert len(http) == expected["http_delegate_count"]
    assert set(mcp) <= set(facade)
    assert len(mcp) == expected["mcp_delegate_count"]
    assert "from cruxible_core.runtime import api\n" not in HTTP_ROUTES.read_text(encoding="utf-8")


def test_playbill_legacy_imports_are_adapter_only_and_manifested() -> None:
    donors_root = CORE / "playbill" / "donors"
    violations: list[str] = []
    for path in sorted((CORE / "playbill").rglob("*.py")):
        if path.is_relative_to(donors_root):
            continue
        for imported in _imports(path):
            donor = donor_for(imported)
            if donor is not None:
                violations.append(f"{path.relative_to(ROOT)} -> {imported}")
    assert violations == []

    adapters = {entry.adapter for entry in DONOR_MANIFEST if entry.adapter is not None}
    implemented = {
        f"cruxible_core.playbill.donors.{path.stem}"
        for path in donors_root.glob("*.py")
        if path.stem not in {"__init__", "manifest"}
    }
    assert implemented == adapters
    for adapter in implemented:
        path = _module_path(adapter)
        assert path is not None
        donor_imports = {item for item in _imports(path) if donor_for(item) is not None}
        assert len(donor_imports) == 1
        imported = next(iter(donor_imports))
        assert donor_for(imported).adapter == adapter  # type: ignore[union-attr]


def test_donor_manifest_matches_ratified_removal_batches() -> None:
    actual = {entry.module_prefix: entry.removal_batch for entry in DONOR_MANIFEST}
    assert actual == RATIFIED_DONOR_REMOVAL_BATCHES


def test_destructive_pass_oracles_are_exact_and_immutable() -> None:
    metadata = json.loads((GOLDENS / "oracles-v1.json").read_text(encoding="utf-8"))
    assert metadata["format"] == "playbill-oracles-v1"
    for name, commit in ORACLE_COMMITS.items():
        assert metadata[name]["commit"] == commit
        assert len(commit) == 40
        assert commit == commit.lower()
        assert all(character in "0123456789abcdef" for character in commit)
