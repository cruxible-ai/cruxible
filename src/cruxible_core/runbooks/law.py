"""What the proposal evaluator reads to judge a Runbook member and a Trigger aimed at one.

Kept apart from ``proposals.py`` so the Runbook's own reads (one candidate
lookup per step) stay in one place; the law itself is the client contract's
``evaluate_runbook_law``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.line_specs import LineSpec
from cruxible_client.contracts.runbooks import (
    AcceptedRunbook,
    RunbookFormatError,
    RunbookLineState,
    evaluate_runbook_law,
    parse_runbook,
    runbook_digest,
    runbook_path,
)


@dataclass(frozen=True)
class RunbookMemberVerdict:
    verdict: str
    diagnostics: tuple[CompilerDiagnostic, ...]
    artifact_digest: str | None
    predecessor_digest: str | None
    required_tier: PermissionTier | None
    retired: bool


def line_state(accepted: Any) -> RunbookLineState | None:
    """The Runbook law's reading of one accepted Line, or None when it is not one."""

    line = getattr(accepted, "line", None)
    if not isinstance(line, LineSpec):
        return None
    return RunbookLineState(
        live=line.lifecycle.state == "live", takes_event=line.trigger_input is not None
    )


def judge_runbook_member(
    content: bytes,
    *,
    path: str,
    parent_content: bytes | None,
    lines: Mapping[str, Any],
) -> RunbookMemberVerdict:
    """Parse, then judge one Runbook against the candidate's Lines: one lookup per step."""

    runbook = parse_runbook(content, path=path)
    predecessor: AcceptedRunbook | None = None
    if parent_content is not None:
        previous = parse_runbook(parent_content, path=path)
        predecessor = AcceptedRunbook(
            path=path, runbook=previous, artifact_digest=runbook_digest(previous).tagged
        )
    states: dict[str, RunbookLineState] = {}
    if runbook.lifecycle.state == "live":
        for identity in runbook.lines:
            state = line_state(lines.get(identity.qualified))
            if state is not None:
                states[identity.qualified] = state
    law = evaluate_runbook_law(runbook, path=path, predecessor=predecessor, lines=states)
    return RunbookMemberVerdict(
        verdict=law.verdict,
        diagnostics=law.diagnostics,
        artifact_digest=law.artifact_digest,
        predecessor_digest=None if predecessor is None else predecessor.artifact_digest,
        required_tier=law.required_tier,
        retired=runbook.lifecycle.state == "retired",
    )


def candidate_runbook_live(tree: Mapping[str, bytes], name: str) -> bool:
    """Whether the candidate holds a live Runbook of this name: one path lookup."""

    try:
        path = runbook_path(name)
    except RunbookFormatError:
        return False
    content = tree.get(path)
    if content is None:
        return False
    try:
        return parse_runbook(content, path=path).lifecycle.state == "live"
    except RunbookFormatError:
        return False


__all__ = ["RunbookMemberVerdict", "candidate_runbook_live", "judge_runbook_member", "line_state"]
