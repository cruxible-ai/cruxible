"""A ledger publication may never run inside the daemon's event loop.

`publish_ledger_mirror` shells out to `git push`. Starlette runs a synchronous
route function in a threadpool and an `async def` one directly on the loop, so a
publishing route declared `async def` puts a blocking network call where every
other request in the process is waiting. One unreachable remote would then stall
the whole daemon rather than the one caller whose write it follows -- and the
mirror's own law is that a copy may never become a condition of the record.

`submit_approval` shipped exactly that way for the length of one batch: it was
`async def` before it began publishing, and it kept the keyword when it started.
The push now carries its own deadline, but a bounded stall on the loop is still
a stall of the whole process, so both bounds are load-bearing.

The closure below is deliberately over-approximate. It matches called functions
by bare name across the whole package, so an unrelated helper sharing a name
with a publishing one is counted as reaching it. A guardrail on availability
should fail loudly on a maybe rather than reason its way to silence.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "cruxible_core"
ROUTES = SOURCE / "server" / "routes" / "playbill.py"
PUBLICATION = "publish_ledger_mirror"


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        function = inner.func
        if isinstance(function, ast.Name):
            names.add(function.id)
        elif isinstance(function, ast.Attribute):
            names.add(function.attr)
    return names


def _call_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                graph.setdefault(node.name, set()).update(_called_names(node))
    return graph


def _publishing_names() -> set[str]:
    graph = _call_graph()
    reaching = {PUBLICATION}
    changed = True
    while changed:
        changed = False
        for name, called in graph.items():
            if name not in reaching and called & reaching:
                reaching.add(name)
                changed = True
    return reaching


def test_the_closure_finds_the_publication_and_its_known_callers() -> None:
    """Prove the closure can see anything at all before trusting its silence."""

    reaching = _publishing_names()

    assert PUBLICATION in reaching
    assert "set_ledger_mirror" in reaching
    assert "service_submit_playbill_approval" in reaching
    assert "service_activate_playbill_proposal" in reaching
    assert "service_withdraw_playbill_proposal" in reaching
    assert "playbill_init" in reaching


def test_no_async_playbill_route_can_reach_the_ledger_publication() -> None:
    reaching = _publishing_names()
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))

    offenders = sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and _called_names(node) & reaching
    )

    assert offenders == [], (
        "these Playbill routes are `async def` and can reach the ledger mirror "
        "publication, so a blocking `git push` would run on the event loop; "
        "declare them `def` like every other mutating route: " + ", ".join(offenders)
    )
