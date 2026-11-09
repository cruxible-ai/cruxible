"""The distributed plugin's catalog, resources and real stdio/daemon start."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from scripts.build_agent_plugin import BUNDLED_DOCS, PACKAGE, ROOT, synchronize

from cruxible_core.mcp.curation import advertised_tool_names, resolve_tool_curation
from cruxible_core.mcp.server import _install_tool_curation
from cruxible_core.mcp.tools import register_tools
from cruxible_core.runtime.permissions import PermissionMode
from tests.core_support._mcp_budget import (
    PLUGIN_PROFILE_MODEL_VISIBLE_TOKENS,
    catalog_model_visible_tokens,
)
from tests.support.short_temporary_root import short_temporary_directory

START_TOOLS = {
    "cruxible_kit_add",
    "cruxible_provider_install",
    "cruxible_line_enable",
    "cruxible_line_run",
    "cruxible_runbook_preview",
    "cruxible_runbook_run",
}


def _config(filename: str = "mcp.json") -> dict:
    return json.loads((ROOT / PACKAGE / filename).read_text())["mcpServers"]["cruxible"]


def test_generated_host_packages_are_current_and_share_the_release() -> None:
    assert synchronize(check=True) == []
    portable = json.loads((ROOT / PACKAGE / "plugin.json").read_text())
    claude = json.loads((ROOT / PACKAGE / ".claude-plugin/plugin.json").read_text())
    assert all(portable[key] == claude[key] for key in ("name", "version", "description"))
    core_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert portable["version"] in {"0.0.0", core_version}, "refresh the plugin at the release bump"
    assert _config() == _config(".mcp.json")
    assert _config()["args"] == ["--from", f"cruxible=={portable['version']}", "cruxible", "mcp"]
    # The package can be copied out of the checkout without breaking references.
    package = ROOT / PACKAGE
    skills = {path.parent.name for path in (package / "skills").glob("*/SKILL.md")}
    assert skills == {"adopt-kit", "create-state", "review-state", "automate-state"}
    # Skills link core docs as bundled copies, never as web URLs.
    bundled = {path.name for path in (package / "docs").glob("*.md")}
    assert bundled == set(BUNDLED_DOCS)
    for path in [*(package / "skills").rglob("*.md"), *(package / "docs").glob("*.md")]:
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            assert "docs.cruxible.ai" not in link, (path, link)
            if "://" in link:
                continue
            target = (path.parent / (link.split("#", 1)[0] or path.name)).resolve()
            assert target.is_relative_to(package) and target.is_file(), (path, link)


def test_plugin_admin_catalog_is_lean_and_start_tools_are_callable() -> None:
    config = _config()
    assert config["env"]["CRUXIBLE_MODE"] == "admin"
    curation = resolve_tool_curation(config["env"])
    server = FastMCP("plugin-catalog-only")
    registered = set(register_tools(server))
    names = advertised_tool_names(
        mode=PermissionMode.ADMIN, registered_tools=registered, curation=curation
    )
    assert START_TOOLS <= names
    assert len(names) == 23 and "cruxible_authoring_submit" not in names
    _install_tool_curation(server, names, mode=PermissionMode.ADMIN, curation=curation)
    tools = asyncio.run(server.list_tools())
    estimate = catalog_model_visible_tokens(tools)
    print(f"Plugin catalog: {len(tools)} tools, {estimate:.2f} estimated model-visible tokens")
    assert estimate <= PLUGIN_PROFILE_MODEL_VISIBLE_TOKENS
    team = advertised_tool_names(
        mode=PermissionMode.GOVERNED_WRITE, registered_tools=registered, curation=curation
    )
    assert "cruxible_provider_install" not in team and "cruxible_proposal_approve" not in team
    assert "cruxible_kit_add" in team
    # next's inbox repair and review-state's attestation stay reachable for authors.
    assert {"cruxible_inbox_list", "cruxible_claim_attest"} <= team


@pytest.mark.parametrize("filename", ["mcp.json", ".mcp.json"])
def test_plugin_config_starts_mcp_autostarts_scratch_daemon_and_orients(filename: str) -> None:
    """Use the local-checkout launcher while the release pin is unpublished."""

    config = _config(filename)
    scratch = short_temporary_directory("cxp")
    state = scratch / "s"
    workspace = scratch / "w"
    workspace.mkdir()
    executable = Path(sys.executable).parent / "cruxible"
    assert executable.is_file(), "the existing test environment needs the cruxible entry point"
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("CRUXIBLE_")},
        **config["env"],
        "HOME": str(scratch),
        "XDG_CONFIG_HOME": str(scratch / "config"),
        "XDG_STATE_HOME": str(scratch / "xdg-state"),
        "CRUXIBLE_STATE_ROOT": str(state),
        "CRUXIBLE_MCP_WORKSPACE_ROOT": str(workspace),
        "PYTHONPATH": os.pathsep.join(
            (
                str(ROOT),
                str(ROOT / "src"),
                str(ROOT / "packages/cruxible-client/src"),
                os.environ.get("PYTHONPATH", ""),
            )
        ),
    }

    async def exercise() -> None:
        # Only the unpublished distribution launcher changes. All catalog/default
        # settings are loaded from the actual generated host configuration.
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "cruxible_core.mcp"], env=env, cwd=workspace
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert START_TOOLS <= names and len(names) == 23
                for name in sorted(START_TOOLS):
                    # Incomplete arguments may refuse, but the default catalog
                    # must dispatch these names rather than hide them.
                    dispatched = await session.call_tool(name, {})
                    text = " ".join(
                        block.text for block in dispatched.content if hasattr(block, "text")
                    )
                    assert "excluded by MCP profile" not in text, (name, text)
                hidden = await session.call_tool("cruxible_authoring_compile", {})
                assert hidden.isError
                # First real request must auto-start a daemon in this fresh root.
                info = await session.call_tool("cruxible_server_info", {})
                assert not info.isError, info
                assert (state / "run/daemon.sock").exists()
                created = await asyncio.to_thread(
                    subprocess.run,
                    [
                        str(executable),
                        "init",
                        "--principal-id",
                        "owner",
                        "--key-dir",
                        str(scratch / "keys"),
                        "--json",
                    ],
                    cwd=workspace,
                    env={**env, "CRUXIBLE_SERVER_SOCKET": str(state / "run/daemon.sock")},
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                assert created.returncode == 0, (created.stdout, created.stderr)
                instance = json.loads(created.stdout)["instance_id"]
                orient = await session.call_tool("cruxible_orient", {"instance_id": instance})
                assert not orient.isError, orient
                body = " ".join(block.text for block in orient.content if hasattr(block, "text"))
                assert instance in body and "playbill-orient-v1" in body

    try:
        asyncio.run(exercise())
    finally:
        # Address only the state root this test created; never a user's daemon.
        if (state / "run/daemon.sock").exists():
            stopped = subprocess.run(
                [
                    str(executable),
                    "server",
                    "stop",
                    "--state-root",
                    str(state),
                    "--timeout",
                    "15",
                    "--json",
                ],
                env={**env, "CRUXIBLE_SERVER_SOCKET": str(state / "run/daemon.sock")},
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert stopped.returncode == 0, (stopped.stdout, stopped.stderr)
        shutil.rmtree(scratch)
