"""Every authoring refusal must name what to do next, not only what went wrong.

A refusal that states a fault and stops leaves the caller to guess the repair,
and the guess is usually another refused round trip. The model already exists in
three places -- the dispositions repair hands back the exact claim ids, the bind
refusal names the verb to run instead, the rebase hint interpolates the command
-- so this guardrail holds the rest of the authoring family to it.

Scope note: this covers the diagnostics the authoring preflight constructs,
where "next step" is representable today. The proposal and claim families carry
their refusals as bare CompilerDiagnostics whose next-step slots
(`local_edits`, `operation_references`) are declared but unused; widening this
law to them needs those slots populated, which is wire work, not a message edit.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "src/cruxible_core/playbill/authoring/preflight.py"
LOWERING = REPO_ROOT / "src/cruxible_core/playbill/authoring/lowering.py"

# An actionable message names an operation the caller can perform. These are the
# verbs the existing good refusals use; a message carrying none of them states a
# fault without a remedy.
_ACTIONABLE = (
    "playbill ",
    "Run ",
    "Use ",
    "Re-",
    "Replace ",
    "Choose ",
    "Omit ",
    "Mint ",
    "Repair ",
    "Name ",
    "Edit ",
    "Remove ",
    "Reduce ",
    "Disposition ",
    "Supply ",
    "Add ",
)


def _literal(node: ast.AST) -> str | None:
    """Fold a string literal or an implicit concatenation into one string."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _diagnostic_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_diagnostic"
    ]


def test_every_repairless_authoring_diagnostic_names_an_alternative() -> None:
    """A diagnostic that carries no repair object must name the step in prose."""
    offenders: list[str] = []
    for call in _diagnostic_calls(PREFLIGHT):
        repairs = _keyword(call, "repairs")
        carries_repair = repairs is not None and not (
            isinstance(repairs, ast.Tuple) and not repairs.elts
        )
        if carries_repair:
            continue
        message = _literal(_keyword(call, "message") or ast.Constant(value=None))
        code = _literal(_keyword(call, "code") or ast.Constant(value=None)) or "<computed>"
        if message is None:
            # A computed message cannot be checked statically; the two that exist
            # are asserted by name below.
            continue
        if not any(marker in message for marker in _ACTIONABLE):
            offenders.append(f"{PREFLIGHT.name}:{call.lineno} {code}: {message!r}")

    assert not offenders, (
        "These authoring refusals carry no repair and name no alternative, so a "
        "caller learns the fault and not the remedy. Name the operation to run "
        "in the message, or attach a repair. Offenders:\n  - " + "\n  - ".join(offenders)
    )


def test_the_two_computed_reference_messages_name_their_alternative() -> None:
    """The successor-ambiguous / retired pair build their message in a branch."""
    source = PREFLIGHT.read_text(encoding="utf-8")
    for marker in (
        "More than one accepted artifact claims to succeed this reference.",
        "The typed reference has no live successor at the intent base.",
    ):
        assert marker in source
        tail = source.split(marker, 1)[1][:300]
        assert any(item in tail for item in _ACTIONABLE), (
            f"the refusal {marker!r} states a fault without naming the next step"
        )


def test_every_lowering_refusal_still_carries_a_repair() -> None:
    """`_refuse` takes repair kind and description as required arguments.

    This is the structural version of the same law, and the reason the lowering
    family needed no message edits: it cannot refuse without a repair.
    """
    tree = ast.parse(LOWERING.read_text(encoding="utf-8"))
    refuse_defs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_refuse"
    ]
    assert len(refuse_defs) == 1
    kwonly = {argument.arg for argument in refuse_defs[0].args.kwonlyargs}
    assert {"repair_kind", "repair_description"} <= kwonly

    defaults = refuse_defs[0].args.kw_defaults
    names = [argument.arg for argument in refuse_defs[0].args.kwonlyargs]
    required = {name for name, default in zip(names, defaults, strict=True) if default is None}
    assert {"repair_kind", "repair_description"} <= required, (
        "repair_kind and repair_description must stay required, or a lowering "
        "refusal could be raised with no repair at all"
    )
