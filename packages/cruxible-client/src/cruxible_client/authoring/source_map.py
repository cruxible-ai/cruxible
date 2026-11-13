"""Best-effort AST source locations for structured server diagnostics."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.authoring.sdk_types import CallSite, SourceMapEntry


def _call_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def capture_keyword_sites(
    operation: str,
    *,
    stacklevel: int = 1,
) -> dict[str, CallSite]:
    """Locate keyword expressions in the smallest call spanning the caller line."""

    frame = inspect.currentframe()
    try:
        for _ in range(stacklevel + 1):
            if frame is None:
                return {}
            frame = frame.f_back
        if frame is None:
            return {}
        filename = frame.f_code.co_filename
        line = frame.f_lineno
        try:
            source = Path(filename).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=filename)
        except (OSError, UnicodeError, SyntaxError):
            return {}
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node) == operation
            and node.lineno <= line <= getattr(node, "end_lineno", node.lineno)
        ]
        if not calls:
            return {}
        call = min(
            calls,
            key=lambda item: (
                getattr(item, "end_lineno", item.lineno) - item.lineno,
                getattr(item, "end_col_offset", item.col_offset) - item.col_offset,
            ),
        )
        sites: dict[str, CallSite] = {}
        for keyword in call.keywords:
            if keyword.arg is None:
                continue
            value = keyword.value
            sites[keyword.arg] = CallSite(
                logical_file=filename,
                line=value.lineno,
                column=value.col_offset,
                expression=ast.get_source_segment(source, value),
            )
        return sites
    finally:
        del frame


@dataclass(frozen=True)
class DiagnosticSourceMap:
    entries: tuple[SourceMapEntry, ...]

    def locate(self, emitted_path: str) -> CallSite | None:
        exact = [entry.call_site for entry in self.entries if emitted_path in entry.emitted_paths]
        return exact[0] if len(exact) == 1 else None


def entries_for_keywords(
    *,
    builder: str,
    emitted: dict[str, tuple[str, ...]],
    sites: dict[str, CallSite],
) -> tuple[SourceMapEntry, ...]:
    return tuple(
        SourceMapEntry(
            builder_path=f"{builder}.{keyword}",
            emitted_paths=paths,
            call_site=sites[keyword],
        )
        for keyword, paths in emitted.items()
        if keyword in sites
    )


__all__ = ["DiagnosticSourceMap", "capture_keyword_sites", "entries_for_keywords"]
