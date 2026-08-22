"""Shared CLI/MCP lowering from workspace selections to coverage observations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from cruxible_core.errors import DataValidationError
from cruxible_core.playbill.coverage.adapter import (
    WorkingPathBindingsV1,
    WorkingPathBindingV1,
    WorkingSourceObservationV1,
    observe_working_source,
    parse_grep_batch,
    read_working_path,
    selection_for_lines,
)
from cruxible_core.playbill.coverage.contracts import LogicalSourceIdentityV1


class WorkspaceCoverageError(DataValidationError):
    """A workspace coverage selection is malformed or empty."""


def bindings_from_mapping(declared: Mapping[str, str]) -> WorkingPathBindingsV1:
    bindings: list[WorkingPathBindingV1] = []
    for path, value in sorted(declared.items()):
        plane, separator, identity = value.partition(":")
        if not separator or plane not in {"ledger", "external"}:
            raise WorkspaceCoverageError(
                f"binding for {path} must name the ledger or external plane"
            )
        bindings.append(
            WorkingPathBindingV1(
                path=path,
                source=LogicalSourceIdentityV1(
                    plane=cast(Literal["ledger", "external"], plane),
                    identity=identity,
                ),
            )
        )
    if not bindings:
        raise WorkspaceCoverageError("coverage needs at least one declared binding")
    return WorkingPathBindingsV1(bindings=tuple(bindings))


def _line_range(value: str) -> tuple[str, int, int]:
    path, separator, span = value.rpartition(":")
    start_text, dash, end_text = span.partition("-")
    if not separator or not path or not start_text.isdigit():
        raise WorkspaceCoverageError("a range must be PATH:START-END")
    end_text = end_text if dash else start_text
    if not end_text.isdigit():
        raise WorkspaceCoverageError("a range must be PATH:START-END")
    return path, int(start_text), int(end_text)


def observe_workspace(
    bindings: WorkingPathBindingsV1,
    *,
    root: Path,
    files: tuple[str, ...] = (),
    ranges: tuple[str, ...] = (),
    grep_text: str | None = None,
    whole_working_set: bool = False,
) -> tuple[WorkingSourceObservationV1, ...]:
    """Observe each selected source once, with whole-source edits taking precedence."""

    whole = set(bindings.paths) if whole_working_set else set(files)
    windows: dict[str, set[tuple[int, int]]] = {}
    for value in ranges:
        path, start_line, end_line = _line_range(value)
        windows.setdefault(path, set()).add((start_line, end_line))
    if grep_text is not None:
        for path, line in parse_grep_batch(grep_text):
            windows.setdefault(path, set()).add((line, line))

    observations: list[WorkingSourceObservationV1] = []
    for path in sorted(whole | set(windows)):
        content = read_working_path(path, root=root)
        selections = (
            ()
            if path in whole
            else tuple(
                selection_for_lines(content, start_line=start, end_line=end)
                for start, end in sorted(windows[path])
            )
        )
        observations.append(
            observe_working_source(bindings.source_for(path), content, selections=selections)
        )
    if not observations:
        raise WorkspaceCoverageError(
            "name at least one file, range, grep result, or whole working set"
        )
    return tuple(observations)


__all__ = [
    "WorkspaceCoverageError",
    "bindings_from_mapping",
    "observe_workspace",
]
