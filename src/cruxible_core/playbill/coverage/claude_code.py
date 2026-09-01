"""The Claude Code PostToolUse envelope: one translation table, one assumption.

This is the *secondary* delivery path. §11.7's Claude Code disposition asked for
transparent coverage on Read, Grep, Edit, and Write via `updatedToolOutput`;
what the harness actually supports is narrower, and this module is where that
gap is written down in code rather than discovered at run time. The full-fidelity
path is :mod:`.middleware`.

The envelope, verbatim
----------------------
Read out of the shipped Claude Code binary at version **2.1.234**, because the
published documentation disagreed with itself on this surface. Stdin for one
PostToolUse event::

    {..., "hook_event_name": "PostToolUse", "tool_name": ..., "tool_input": {...},
     "tool_response": <the tool's structured output>, "tool_use_id": ...,
     "duration_ms": ...}

It is `tool_response`, not `tool_result`. Stdout::

    {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                            "updatedToolOutput": <same shape as tool_response>}}

`hookEventName` is required, and the union member also accepts
`additionalContext` -- which this module never emits, per §11.7, because Claude
Code renders it inside a `<system-reminder>` and that is the §11.4 instruction
channel.

Why only Grep is annotated
--------------------------
`updatedToolOutput` is not appended text. It is validated against the tool's own
output schema and then rendered by the tool's own mapper, which builds the
model-visible string from typed fields. That leaves exactly one usable slot
among the four tools:

* **Grep** in `content` mode carries a free-text `content` field that its mapper
  emits verbatim. Coverage cards append cleanly, and every other field of the
  response is passed through byte-identical.
* **Read** renders `tool_response.file.content` through a line numberer.
  Appending there would present coverage cards as numbered file lines that do
  not exist in the file -- fabricated source content, and a trap for the next
  edit that targets those line numbers.
* **Edit** and **Write** synthesize their result from typed fields
  (`filePath`, `type`, `userModified`) with no free-text slot at all.

So Read, Edit, and Write are consumed for **observation only**: their paths are
resolved, which refreshes the local freshness manifest and its epoch, so the
next Grep answers against a current snapshot. Their output is returned
unmodified and no other channel is used.

Versioning
----------
:data:`ENVELOPE_VERSION` pins the version this table was read from. Nothing here
adapts to an unknown envelope: a payload that is not a recognizable PostToolUse
event yields no event and no output change, which is the same fail-open answer
as an unreachable daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_core.playbill.coverage.middleware import (
    HarnessGrepHitV1,
    HarnessLineRangeV1,
    HarnessToolEventV1,
    HarnessToolKindV1,
)

ENVELOPE_VERSION = "2.1.234"
HOOK_EVENT_NAME = "PostToolUse"

# The one translation table: vendor tool name -> the middleware's tool kind.
TOOL_KINDS: dict[str, HarnessToolKindV1] = {
    "Read": "read",
    "Grep": "grep",
    "Edit": "edit",
    "Write": "write",
}

# The subset whose output shape can carry an annotation without fabricating
# anything. See the module docstring; this is a fact about Claude Code 2.1.234,
# not a Playbill policy.
ANNOTATABLE_TOOLS: frozenset[str] = frozenset({"Grep"})


class PostToolUseResponseError(ValueError):
    """A recognized PostToolUse event carried no structured tool response."""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _relative_path(raw: Any, *, workspace_root: Path) -> str | None:
    """Make one absolute tool path workspace-relative, or decline.

    A path outside the workspace is not unbindable-with-an-error; it is simply
    not part of this working set, and unbound is silent.
    """

    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    try:
        # Both sides resolved, because a workspace reached through a symlink
        # (a temporary directory on macOS, a worktree behind a link) would
        # otherwise fail to match its own absolute tool paths.
        base = workspace_root.expanduser().resolve()
        absolute = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        return absolute.relative_to(base).as_posix()
    except (ValueError, OSError):
        return None


def _grep_hits(response: dict[str, Any], *, workspace_root: Path) -> tuple[HarnessGrepHitV1, ...]:
    """Read `path:line:...` locations out of a content-mode search result.

    Locations only. The matched text is never parsed for meaning, and the
    surrounding output is never rewritten.
    """

    if response.get("mode") != "content":
        return ()
    content = response.get("content")
    if not isinstance(content, str):
        return ()
    hits: list[HarnessGrepHitV1] = []
    for raw in content.splitlines():
        head, separator, remainder = raw.partition(":")
        number, _, _ = remainder.partition(":")
        if not separator or not number.isdigit():
            continue
        path = _relative_path(head, workspace_root=workspace_root)
        if path is not None:
            hits.append(HarnessGrepHitV1(path=path, line=int(number)))
    return tuple(hits)


def _read_ranges(
    tool_input: dict[str, Any],
    response: dict[str, Any],
    *,
    path: str,
) -> tuple[HarnessLineRangeV1, ...]:
    """The window a Read reported, when it reported one.

    `offset`/`limit` are the request; the response's `startLine`/`numLines` are
    what was actually returned, and the response wins where both exist because
    it describes the bytes the agent is looking at.
    """

    file_block = _mapping(response.get("file"))
    start = file_block.get("startLine")
    count = file_block.get("numLines")
    if not isinstance(start, int) or not isinstance(count, int):
        start = tool_input.get("offset")
        count = tool_input.get("limit")
    if not isinstance(start, int) or not isinstance(count, int) or start < 1 or count < 1:
        return ()
    return (HarnessLineRangeV1(path=path, start_line=start, end_line=start + count - 1),)


def read_post_tool_use_event(
    payload: Any,
    *,
    workspace_root: Path,
) -> HarnessToolEventV1 | None:
    """Translate one PostToolUse payload, or decline to recognize it."""

    if not isinstance(payload, dict) or payload.get("hook_event_name") != HOOK_EVENT_NAME:
        return None
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or tool_name not in TOOL_KINDS:
        return None
    if tool_name in ANNOTATABLE_TOOLS and not isinstance(payload.get("tool_response"), dict):
        raise PostToolUseResponseError("recognized PostToolUse tool_response must be an object")

    tool_input = _mapping(payload.get("tool_input"))
    response = _mapping(payload.get("tool_response"))
    kind = TOOL_KINDS[tool_name]

    if kind == "grep":
        hits = _grep_hits(response, workspace_root=workspace_root)
        content = response.get("content")
        return HarnessToolEventV1(
            kind="grep",
            tool_name=tool_name,
            grep_hits=hits,
            original_output=content if isinstance(content, str) else "",
        )

    path = _relative_path(tool_input.get("file_path"), workspace_root=workspace_root)
    if path is None:
        return None
    if kind == "read":
        ranges = _read_ranges(tool_input, response, path=path)
        # A windowed read asks about its window; a whole-file read asks about
        # the whole source. Naming both would ask twice about the same bytes.
        return HarnessToolEventV1(
            kind="read",
            tool_name=tool_name,
            paths=() if ranges else (path,),
            ranges=ranges,
        )
    return HarnessToolEventV1(kind=kind, tool_name=tool_name, paths=(path,))


def annotated_tool_output(
    payload: Any,
    appended_coverage_text: str,
) -> dict[str, Any] | None:
    """Build the `updatedToolOutput` object, or return ``None`` to change nothing.

    The returned object is the original `tool_response` with exactly one field
    extended: Grep's free-text `content`, with the coverage lines appended after
    it. Every other field is carried through unchanged, including `numLines` and
    `totalLines`, which describe the *search* and would be misreported if the
    annotation were counted into them.
    """

    if not appended_coverage_text or not isinstance(payload, dict):
        return None
    if payload.get("tool_name") not in ANNOTATABLE_TOOLS:
        return None
    response = payload.get("tool_response")
    if not isinstance(response, dict) or response.get("mode") != "content":
        return None
    content = response.get("content")
    if not isinstance(content, str) or not content:
        return None
    return {**response, "content": content + "\n" + appended_coverage_text}


def post_tool_use_response(updated_tool_output: dict[str, Any] | None) -> dict[str, Any]:
    """The stdout object, in the shape Claude Code 2.1.234 parses.

    An empty object is a valid, complete answer meaning "change nothing," and it
    is what every non-annotatable tool and every failed-open delivery emits.
    `additionalContext` is never populated: §11.7 forbids it for ordinary
    coverage cards, and Claude Code would render it as a system reminder.
    """

    if updated_tool_output is None:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT_NAME,
            "updatedToolOutput": updated_tool_output,
        }
    }


__all__ = [
    "ANNOTATABLE_TOOLS",
    "ENVELOPE_VERSION",
    "HOOK_EVENT_NAME",
    "TOOL_KINDS",
    "annotated_tool_output",
    "post_tool_use_response",
    "read_post_tool_use_event",
]
