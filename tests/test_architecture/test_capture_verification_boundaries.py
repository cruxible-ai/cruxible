"""Architecture pins for evidence verification at daemon/client boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (ROOT / "packages" / "cruxible-client" / "src", ROOT / "src")
CAPTURE_MODULE = "cruxible_client.contracts.captures"


def _verify_capture_call_nodes(tree: ast.AST) -> tuple[ast.Call, ...]:
    function_names: set[str] = set()
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == CAPTURE_MODULE:
            function_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "verify_capture"
            )
        elif isinstance(node, ast.Import):
            module_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == CAPTURE_MODULE
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "cruxible_client.contracts":
            module_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "captures"
            )
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct = isinstance(node.func, ast.Name) and node.func.id in function_names
        qualified = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "verify_capture"
            and ast.unparse(node.func.value) in module_names
        )
        if direct or qualified:
            found.append(node)
    return tuple(found)


def _verify_capture_calls() -> tuple[tuple[str, int, frozenset[str], bool], ...]:
    found: list[tuple[str, int, frozenset[str], bool]] = []
    for root in PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for node in _verify_capture_call_nodes(tree):
                resolver_values = tuple(
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "producer_receipt_resolver"
                )
                found.append(
                    (
                        path.relative_to(ROOT).as_posix(),
                        node.lineno,
                        frozenset(
                            keyword.arg for keyword in node.keywords if keyword.arg is not None
                        ),
                        any(
                            isinstance(value, ast.Constant) and value.value is None
                            for value in resolver_values
                        ),
                    )
                )
    return tuple(sorted(found))


def test_every_production_capture_verifier_injects_a_producer_receipt_resolver() -> None:
    calls = _verify_capture_calls()
    assert tuple(path for path, _line, _keywords, _literal_none in calls) == (
        "packages/cruxible-client/src/cruxible_client/contracts/claims.py",
        "src/cruxible_core/playbill/authoring/lowering.py",
        "src/cruxible_core/service/playbill_claim_attestations.py",
    )
    assert all(
        "producer_receipt_resolver" in keywords and not literal_none
        for _path, _line, keywords, literal_none in calls
    )


def test_capture_verifier_guard_resolves_aliases_and_rejects_literal_none() -> None:
    tree = ast.parse(
        "from cruxible_client.contracts.captures import verify_capture as check\n"
        "check('sha256:' + '0' * 64, producer_receipt_resolver=None)\n"
    )
    calls = _verify_capture_call_nodes(tree)
    assert len(calls) == 1
    resolver = next(
        keyword.value for keyword in calls[0].keywords if keyword.arg == "producer_receipt_resolver"
    )
    assert isinstance(resolver, ast.Constant) and resolver.value is None

    dotted_tree = ast.parse(
        "import cruxible_client.contracts.captures\n"
        "cruxible_client.contracts.captures.verify_capture('sha256:' + '0' * 64)\n"
    )
    assert len(_verify_capture_call_nodes(dotted_tree)) == 1


def test_client_claim_contract_does_not_import_daemon_receipt_machinery() -> None:
    path = ROOT / "packages/cruxible-client/src/cruxible_client/contracts/claims.py"
    tree = ast.parse(path.read_bytes(), filename=str(path))
    daemon_imports = tuple(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("cruxible_core")
    )
    assert daemon_imports == ()
