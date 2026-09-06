"""Dependency laws for the daemon/SDK package split."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT_ROOT = ROOT / "packages" / "cruxible-client" / "src" / "cruxible_client"
CORE_ROOT = ROOT / "src" / "cruxible_core"

# These modules run on the caller side despite sharing the daemon distribution.
# G7 may rebuild them over the public SDK. They are deliberately excluded from
# the daemon half of D2; the closed list prevents that exception from spreading.
CLIENT_ADAPTER_PREFIXES = ("cli/", "client/", "mcp/")
LEGACY_CLIENT_SIGNING_BRIDGES = {"playbill/signing.py"}
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
        if relative.startswith(CLIENT_ADAPTER_PREFIXES) or relative in (
            LEGACY_ERROR_BRIDGES | LEGACY_CLIENT_SIGNING_BRIDGES
        ):
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
        and path.relative_to(CORE_ROOT).as_posix() not in LEGACY_CLIENT_SIGNING_BRIDGES
    ]
    assert violations == []


def _import_targets(tree: ast.AST, relative: Path) -> tuple[str, ...]:
    targets = []
    package = ["cruxible_core", *relative.parts[:-1]]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = package[: len(package) - node.level + 1]
                module = ".".join(
                    [*prefix, *([] if node.module is None else node.module.split("."))]
                )
            else:
                module = node.module or ""
            targets.append(module)
            targets.extend(f"{module}.{alias.name}" for alias in node.names)
    return tuple(targets)


def test_daemon_domain_never_imports_client_signing_custody() -> None:
    # One legacy caller-side bridge preserves old imports. Neither the bridge
    # nor the client implementation may enter the daemon's dependency graph.
    forbidden = {"cruxible_core.playbill.signing", "cruxible_client.authoring.signing"}
    violations = []
    for path in _python_sources(CORE_ROOT):
        relative = path.relative_to(CORE_ROOT)
        if (
            relative.as_posix().startswith(CLIENT_ADAPTER_PREFIXES)
            or relative.as_posix() in LEGACY_CLIENT_SIGNING_BRIDGES
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _import_targets(tree, relative):
            if module in forbidden:
                violations.append(f"{relative} -> {module}")
    assert violations == []


@pytest.mark.parametrize(
    "statement,relative,target",
    [
        (
            "import cruxible_core.playbill.signing",
            "playbill/domain.py",
            "cruxible_core.playbill.signing",
        ),
        (
            "from cruxible_core.playbill import signing",
            "playbill/domain.py",
            "cruxible_core.playbill.signing",
        ),
        (
            "from .signing import LocalEd25519ApprovalSigner",
            "playbill/domain.py",
            "cruxible_core.playbill.signing",
        ),
        ("from . import signing", "playbill/__init__.py", "cruxible_core.playbill.signing"),
        ("from .. import signing", "playbill/service/domain.py", "cruxible_core.playbill.signing"),
        (
            "from cruxible_client.authoring import signing",
            "playbill/domain.py",
            "cruxible_client.authoring.signing",
        ),
    ],
)
def test_signing_guard_sees_absolute_package_and_relative_imports(statement, relative, target):
    assert target in _import_targets(ast.parse(statement), Path(relative))
