"""Regenerate the checked-in Playbill served-surface inventory."""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FACADE = REPO_ROOT / "src/cruxible_core/runtime/playbill_api.py"
HTTP_ROUTES = REPO_ROOT / "src/cruxible_core/server/routes/playbill.py"
MCP_HANDLERS = REPO_ROOT / "src/cruxible_core/mcp/handlers.py"
SNAPSHOT = REPO_ROOT / "tests/goldens/playbill/served-surface-dp0b-v1.json"


def _facade_operations(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(
        sorted(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("playbill_")
        )
    )


def _playbill_facade_calls(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(
        sorted(
            {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "playbill_api"
                and node.func.attr.startswith("playbill_")
            }
        )
    )


def main() -> None:
    facade = _facade_operations(FACADE)
    http = _playbill_facade_calls(HTTP_ROUTES)
    mcp = _playbill_facade_calls(MCP_HANDLERS)
    if http != facade:
        raise SystemExit("HTTP delegates must exactly match the Playbill facade inventory")
    if not set(mcp) <= set(facade):
        raise SystemExit("MCP delegates must be a subset of the Playbill facade inventory")
    payload = {
        "format": "playbill-served-surface-dp0b-v1",
        "facade_operations": list(facade),
        "http_delegate_count": len(http),
        "mcp_delegate_count": len(mcp),
    }
    SNAPSHOT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {SNAPSHOT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
