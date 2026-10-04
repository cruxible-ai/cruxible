"""Guardrail: no doc, prompt or source names a read surface the read cut removed.

Agents read state through orient, query and get. A document, an MCP prompt or
a repair that still names a removed tool, CLI command or client method teaches
a call that no longer exists. The CHANGELOG is history and may name them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CUT_MCP_TOOLS = (
    "search",
    "discover",
    "expand",
    "list_claims",
    "claim_values",
    "get_claim",
    "claim_history",
    "explain_claim",
    "list_subjects",
    "get_subject",
    "subject_history",
    "list_claim_types",
    "get_claim_type",
    "list_documents",
    "get_document",
    "dereference",
    "history",
    "explain",
    "list_principals",
    "policies_in_force",
    "list_query_definitions",
    "get_query_definition",
    "run_query",
)
CUT_CLIENT_METHODS = (
    "list_playbill_principals",
    "list_playbill_documents",
    "get_playbill_document",
    "dereference_playbill_document",
    "playbill_document_history",
    "explain_playbill_subject",
    "list_playbill_subjects",
    "list_playbill_subject_index",
    "get_playbill_subject",
    "playbill_subject_history",
    "list_playbill_claim_types",
    "get_playbill_claim_type",
    "list_playbill_claims",
    "read_playbill_claim_values",
    "get_playbill_claim",
    "playbill_claim_history",
    "explain_playbill_claim",
    "list_playbill_query_definitions",
    "list_playbill_policies_in_force",
    "get_playbill_query_definition",
    "run_playbill_query",
    "discover_playbill",
    "search_playbill",
    "expand_playbill",
)
_TOOL = re.compile(r"\bcruxible_(?:playbill_)?(?:%s)\b" % "|".join(CUT_MCP_TOOLS))
_METHOD = re.compile(r"\b(?:%s)\(" % "|".join(CUT_CLIENT_METHODS))
_CLI = re.compile(
    r"\b(?:cruxible (?:playbill )?|playbill )(?:"
    r"claim (?:list|values|get|history|explain)"
    r"|subject (?:list|get|history)"
    r"|claim-type (?:list|get)"
    r"|document (?:list|get|body|history)"
    r"|principal list|policy list"
    r"|query (?:list|get|run)"
    r"|discover|search|list|expand|explain"
    r")\b"
)


def _texts() -> list[Path]:
    docs = [
        *(ROOT / "docs").rglob("*.md"),
        ROOT / "README.md",
        ROOT / "packages" / "cruxible-client" / "README.md",
        *(ROOT / "skills").rglob("*.md"),
    ]
    sources = [
        *(ROOT / "src").rglob("*.py"),
        *(ROOT / "packages" / "cruxible-client" / "src").rglob("*.py"),
        *(ROOT / "benchmarks").rglob("*.py"),
    ]
    return [path for path in (*docs, *sources) if path.is_file()]


@pytest.mark.parametrize("pattern", [_TOOL, _METHOD, _CLI], ids=["mcp", "client", "cli"])
def test_no_doc_prompt_or_source_names_a_cut_read_surface(pattern: re.Pattern[str]) -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()[:120]}"
        for path in _texts()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], "names a removed read surface:\n" + "\n".join(offenders)


def test_no_client_method_takes_a_cut_read_surface_under_its_new_name() -> None:
    from cruxible_client.transport.http import CruxibleClient

    renamed = {
        name.replace("playbill_", "", 1) if "playbill_" in name else name.replace("_playbill", "")
        for name in CUT_CLIENT_METHODS
    }
    assert sorted(name for name in renamed if hasattr(CruxibleClient, name)) == []


def test_the_mcp_instructions_teach_the_three_read_verbs() -> None:
    from cruxible_core.mcp.server import BASE_INSTRUCTIONS

    for verb in ("cruxible_orient", "cruxible_query", "cruxible_get"):
        assert verb in BASE_INSTRUCTIONS
    assert ".cruxible/floor/current/<kind>/<id>.yaml" in BASE_INSTRUCTIONS
