"""Q17: MCP stamps and checks projection blocks through the client-side adapter.

`cruxible_playbill_block_declare` took a stamp only the client could compute.
`cruxible_playbill_block_repin` takes the page and the block instead, and the
adapter in the MCP process computes the stamp, rewrites the marker and declares
the block -- the same SDK path the CLI's `block repin` runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.errors import ChangeRefusedError, DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode, reset_permissions
from tests.support.store_snapshot import assert_writes_nothing
from tests.test_client.test_playbill_projection_repin import _RepinClient, _workspace
from tests.test_mcp.test_playbill_protocol_curation import _protocol_session, _run


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
    assert TOOL_PERMISSIONS["cruxible_playbill_block_detach"] == PermissionMode.GOVERNED_WRITE


def test_a_read_only_agent_checks_blocks_but_never_edits_a_page(
    adapter: _RepinClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-004: the read tool has no page edit, and the edit is a write-tier tool.

    Driven through the real MCP door, which applies the permission tier.
    """

    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    reset_permissions()
    page = tmp_path / "runbook.md"
    before = page.read_bytes()
    server = create_server()

    async def exercise() -> tuple[dict[str, set[str]], tuple[bool, str], bool]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            detach = await session.call_tool(
                "cruxible_playbill_block_detach",
                {"instance_id": "inst_projection", "files": ["runbook.md"], "dry_run": False},
            )
            text = " ".join(block.text for block in detach.content if hasattr(block, "text"))
            # Extra arguments are not part of the read: whatever the caller
            # sends, the read tool has no page edit to reach.
            synced = await session.call_tool(
                "cruxible_playbill_block_sync",
                {
                    "instance_id": "inst_projection",
                    "files": ["runbook.md"],
                    "detach": ["runbook.md"],
                    "check": False,
                },
            )
            schemas = {
                tool.name: set(tool.inputSchema.get("properties", {})) for tool in listed.tools
            }
            return schemas, (bool(detach.isError), text), bool(synced.isError)

    schemas, (detach_refused, detach_text), _sync_error = _run(exercise())

    assert schemas["cruxible_playbill_block_sync"] == {"instance_id", "files", "all_sources"}
    assert "cruxible_playbill_block_detach" not in schemas
    assert detach_refused and "GOVERNED_WRITE" in detach_text
    assert page.read_bytes() == before
    reset_permissions()


def test_detaching_previews_and_commits_pinned_to_the_pages_bytes(
    adapter: _RepinClient, tmp_path: Path
) -> None:
    page = tmp_path / "runbook.md"

    preview = assert_writes_nothing(
        [tmp_path],
        lambda: handlers.handle_playbill_block_detach(
            "inst_projection", files=["runbook.md"], dry_run=True
        ),
    )
    assert preview.status == "would_detach"
    assert preview.coordinate.subject == "workspace_pages"

    page.write_bytes(page.read_bytes() + b"edited after the preview\n")
    with pytest.raises(ChangeRefusedError) as moved:
        handlers.handle_playbill_block_detach(
            "inst_projection", files=["runbook.md"], at=preview.coordinate.digest
        )
    assert moved.value.error_code == "playbill.preview.state_moved"

    fresh = handlers.handle_playbill_block_detach(
        "inst_projection", files=["runbook.md"], dry_run=True
    )
    done = handlers.handle_playbill_block_detach(
        "inst_projection", files=["runbook.md"], at=fresh.coordinate.digest
    )
    assert done.status == "detached"
