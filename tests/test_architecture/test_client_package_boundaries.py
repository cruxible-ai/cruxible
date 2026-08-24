"""Dependency laws for the daemon/SDK package split."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT_ROOT = ROOT / "packages" / "cruxible-client" / "src" / "cruxible_client"
CORE_ROOT = ROOT / "src" / "cruxible_core"

# These modules run on the caller side despite sharing the daemon distribution.
# G7 may rebuild them over the public SDK. They are deliberately excluded from
# the daemon half of D2; the closed list prevents that exception from spreading.
CLIENT_ADAPTER_PREFIXES = ("cli/", "client/", "mcp/")
LEGACY_ERROR_BRIDGES = {"errors.py", "server/errors.py"}


def _absolute_imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            result.append(node.module)
    return tuple(result)


def _python_sources(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*.py") if path.is_file()))


def _is_contract_import(module: str) -> bool:
    return module == "cruxible_client.contracts" or module.startswith("cruxible_client.contracts.")


def test_d1_client_package_never_imports_daemon_package() -> None:
    violations = [
        f"{path.relative_to(ROOT)} -> {module}"
        for path in _python_sources(CLIENT_ROOT)
        for module in _absolute_imports(path)
        if module == "cruxible_core" or module.startswith("cruxible_core.")
    ]
    assert violations == []


def test_d2_daemon_domain_imports_only_client_contracts() -> None:
    violations: list[str] = []
    for path in _python_sources(CORE_ROOT):
        relative = path.relative_to(CORE_ROOT).as_posix()
        if relative.startswith(CLIENT_ADAPTER_PREFIXES) or relative in LEGACY_ERROR_BRIDGES:
            continue
        for module in _absolute_imports(path):
            if module == "cruxible_client":
                # ``from cruxible_client import contracts`` is the package form
                # used by generated HTTP request/response models.
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                root_import_is_contracts_only = all(
                    not isinstance(node, ast.ImportFrom)
                    or node.module != "cruxible_client"
                    or {alias.name for alias in node.names} <= {"contracts"}
                    for node in ast.walk(tree)
                )
                if root_import_is_contracts_only:
                    continue
            if module.startswith("cruxible_client") and not _is_contract_import(module):
                violations.append(f"{path.relative_to(ROOT)} -> {module}")
    assert violations == []


def test_d2_authoring_and_transport_exceptions_are_client_adapters_only() -> None:
    violations = [
        f"{path.relative_to(ROOT)} -> {module}"
        for path in _python_sources(CORE_ROOT)
        for module in _absolute_imports(path)
        if module.startswith(("cruxible_client.authoring", "cruxible_client.transport"))
        and not path.relative_to(CORE_ROOT).as_posix().startswith(CLIENT_ADAPTER_PREFIXES)
    ]
    assert violations == []
