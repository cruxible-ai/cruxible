"""Shared Mermaid rendering helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MermaidLegendItem:
    """One procedural legend row for a Mermaid diagram."""

    visual: str
    meaning: str


def escape_mermaid_label(value: str) -> str:
    """Escape user-controlled text for use inside a Mermaid quoted label."""
    return value.replace('"', '\\"').replace("\n", "<br/>")


def mermaid_id(raw: str) -> str:
    """Return a Mermaid-safe identifier."""
    return re.sub(r"[^A-Za-z0-9_]", "_", raw)


def render_mermaid_legend(items: Sequence[MermaidLegendItem]) -> list[str]:
    """Render a compact Markdown legend for Mermaid diagrams."""
    if not items:
        return []
    lines = ["**Diagram legend:**", "", "| Visual | Meaning |", "| --- | --- |"]
    for item in items:
        lines.append(
            f"| {_escape_markdown_table_cell(item.visual)} | "
            f"{_escape_markdown_table_cell(item.meaning)} |"
        )
    return lines


def render_mermaid_inline_legend(items: Sequence[MermaidLegendItem]) -> list[str]:
    """Render a single-line Markdown legend for compact pages."""
    if not items:
        return []
    parts = [
        f"{item.visual} = {item.meaning.rstrip('.')}"
        for item in items
    ]
    return [f"**Diagram legend:** {'; '.join(parts)}."]


def _escape_markdown_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br/>")
