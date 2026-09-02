"""Architecture pins for evidence verification at daemon/client boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (ROOT / "packages" / "cruxible-client" / "src", ROOT / "src")


def _verify_capture_calls() -> tuple[tuple[str, int, frozenset[str]], ...]:
    found: list[tuple[str, int, frozenset[str]]] = []
    for root in PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                if name != "verify_capture":
                    continue
                found.append(
                    (
                        path.relative_to(ROOT).as_posix(),
                        node.lineno,
                        frozenset(
                            keyword.arg for keyword in node.keywords if keyword.arg is not None
                        ),
                    )
                )
    return tuple(sorted(found))


def test_every_production_capture_verifier_injects_a_producer_receipt_resolver() -> None:
    calls = _verify_capture_calls()
    assert tuple(path for path, _line, _keywords in calls) == (
        "packages/cruxible-client/src/cruxible_client/contracts/claims.py",
        "src/cruxible_core/playbill/authoring/lowering.py",
        "src/cruxible_core/service/playbill_claim_attestations.py",
    )
    assert all("producer_receipt_resolver" in keywords for _path, _line, keywords in calls)


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
