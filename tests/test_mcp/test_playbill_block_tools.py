"""Q17: MCP stamps and checks projection blocks through the client-side adapter.

`cruxible_playbill_block_declare` took a stamp only the client could compute.
`cruxible_playbill_block_repin` takes the page and the block instead, and the
adapter in the MCP process computes the stamp, rewrites the marker and declares
the block -- the same SDK path the CLI's `block repin` runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.errors import DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode
from tests.support.store_snapshot import assert_writes_nothing
from tests.test_client.test_playbill_projection_repin import _RepinClient, _workspace


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _RepinClient:
    _workspace(tmp_path)
    client = _RepinClient()
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(handlers, "_block_client", lambda: client)
    return client


def test_repin_by_page_previews_then_stamps_and_declares(
    adapter: _RepinClient, tmp_path: Path
) -> None:
    preview = assert_writes_nothing(
        [tmp_path],
        lambda: handlers.handle_playbill_block_repin(
            "inst_projection",
            file="runbook.md",
            block="summary",
            claims=["CLM-first"],
            dry_run=True,
        ),
    )
    assert (preview.status, preview.source_id, preview.path) == (
        "would_repin",
        "corpus.runbook",
        "runbook.md",
    )
    assert adapter.declared == []

    stamped = handlers.handle_playbill_block_repin(
        "inst_projection", source="corpus.runbook", block="summary", claims=["CLM-first"]
    )

    assert stamped.status == "repinned"
    assert stamped.stamp == preview.stamp
    assert [item["block_id"] for item in adapter.declared] == ["summary"]
    assert b"<!-- playbill:block:summary:ref:" in (tmp_path / "runbook.md").read_bytes()


def test_repin_names_its_page_exactly_once(adapter: _RepinClient) -> None:
    with pytest.raises(DataValidationError, match="exactly one of file or source"):
        handlers.handle_playbill_block_repin("inst_projection", block="summary")
    with pytest.raises(DataValidationError, match="exactly one of file or source"):
        handlers.handle_playbill_block_repin(
            "inst_projection", block="summary", file="runbook.md", source="corpus.runbook"
        )
    with pytest.raises(DataValidationError, match="escapes|normalized"):
        handlers.handle_playbill_block_repin(
            "inst_projection", block="summary", file="../outside.md"
        )


def test_the_block_tools_replace_declare_on_mcp() -> None:
    assert "cruxible_playbill_block_declare" not in TOOL_PERMISSIONS
    assert TOOL_PERMISSIONS["cruxible_playbill_block_repin"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_playbill_block_sync"] == PermissionMode.READ_ONLY
