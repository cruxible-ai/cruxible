"""The terminal lifecycle state gates every governed-write door, by inventory.

Decommission shipped enforced at four call sites: proposal submit, activation
and the inert body store. Approvals, curation rulings, attestations,
predictions, settlements, Procedure binds and Procedure/Line runs all still
landed durable writes into an instance that had stopped accepting them. The set
below is the declared write plane; adding a governed-write door without gating
it, or dropping a gate, has to move this inventory deliberately.

An authoring draft is on it too: creating an intent persists a record in the
exhaust, so a decommissioned instance refuses it rather than accumulating
drafts that can never be submitted. Creating one was the only coordinator door
gated at first, which left every draft that ALREADY existed fully mutable on a
dead instance -- its payload replaceable, its preflight recomputable, its
publication preparable, confirmable and abandonable. So the inventory now names
every coordinator method that persists, and names each one directly rather than
resting on the one it delegates to: a door that keeps its gate only because of
who it calls loses it silently the day it stops calling them.

Reads, replay, crash roll-forward and consumption exhaust are deliberately NOT
here: a decommissioned instance keeps serving what it already accepted, so the
observation and recovery planes stay open. That is why `get`, `resume`,
`status` and `list_pending` are absent even though the protocol roll-forward
they run can persist a transition: rolling an expectation forward to `expired`
is the instance describing what already happened to it, not a new intent.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
GATE_NAMES = frozenset({"require_writable", "_require_writable"})

# module path -> the qualified names that refuse a decommissioned instance.
DECLARED_WRITE_GATES: dict[str, frozenset[str]] = {
    "cruxible_core/playbill/instance.py": frozenset(
        {
            "PlaybillInstance.decommission",
            "PlaybillInstance.store_document_body",
            "PlaybillInstance.prepare_generation",
        }
    ),
    "cruxible_core/playbill/authoring/coordinator.py": frozenset(
        {
            "AuthoringIntentCoordinator.create",
            "AuthoringIntentCoordinator.create_input",
            "AuthoringIntentCoordinator.replace_payload",
            "AuthoringIntentCoordinator.preflight",
            "AuthoringIntentCoordinator.compile",
            "AuthoringIntentCoordinator.compile_input",
            "AuthoringIntentCoordinator.rebase",
            "AuthoringIntentCoordinator.submit",
            "AuthoringIntentCoordinator.prepare_publication",
            "AuthoringIntentCoordinator.confirm_insertion",
            "AuthoringIntentCoordinator.abandon_insertion",
        }
    ),
    "cruxible_core/playbill/proposals.py": frozenset({"ProposalService.submit"}),
    "cruxible_core/playbill/service/documents.py": frozenset({"service_submit_playbill_approval"}),
    "cruxible_core/service/playbill_claim_attestations.py": frozenset(
        {"service_append_claim_attestation"}
    ),
    "cruxible_core/service/playbill_curation.py": frozenset(
        {
            "service_overrule_playbill_curation",
            "service_suppress_playbill_curation",
            "service_accept_fixed_playbill_curation",
        }
    ),
    "cruxible_core/service/playbill_predictions.py": frozenset(
        {"service_predict_playbill", "service_settle_playbill_prediction"}
    ),
    "cruxible_core/service/playbill_procedure_runs.py": frozenset(
        {
            "service_bind_playbill_procedure",
            "service_run_playbill_procedure",
            "service_run_playbill_line",
        }
    ),
}


def _gated_functions(path: Path) -> set[str]:
    """Return the qualified names in one module that call the write gate."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    gated: set[str] = set()
    stack: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack.append(child.name)
                walk(child)
                stack.pop()
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in GATE_NAMES
                and stack
            ):
                gated.add(".".join(stack))
            walk(child)

    walk(tree)
    return gated


def test_the_declared_write_plane_is_exactly_the_gated_one() -> None:
    observed = {
        str(path.relative_to(SOURCE)): gated
        for path in sorted(SOURCE.rglob("*.py"))
        if (gated := _gated_functions(path))
    }

    assert observed == {module: set(names) for module, names in DECLARED_WRITE_GATES.items()}


def test_the_gate_is_the_first_statement_of_every_served_write_door() -> None:
    """A gate after the work is not a gate: the refusal must precede the write."""

    for module, names in DECLARED_WRITE_GATES.items():
        path = SOURCE / module
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(name.endswith(node.name) for name in names):
                continue
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # the docstring
            assert body, f"{module}:{node.name} has no body"
            first = body[0]
            assert isinstance(first, ast.Expr), f"{module}:{node.name} does not gate first"
            call = first.value
            assert isinstance(call, ast.Call), f"{module}:{node.name} does not gate first"
            assert isinstance(call.func, ast.Attribute), f"{module}:{node.name} does not gate first"
            assert call.func.attr in GATE_NAMES, f"{module}:{node.name} does not gate first"
