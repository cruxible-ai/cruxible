"""Caller-side adapter code shared by the CLI and MCP surfaces.

Modules here run in the caller's process, beside the workspace, and reach the
daemon only through the client they are handed; they belong to no one surface.
"""
