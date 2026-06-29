"""MCP package exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["create_server"]


def create_server() -> FastMCP:
    """Create the FastMCP server lazily to avoid import cycles."""
    from cruxible_core.mcp.server import create_server as _create_server

    return _create_server()
