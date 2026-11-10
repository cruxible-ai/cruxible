"""The workspace-root resolver the CLI, the SDK context and the MCP adapter share.

The one implementation lives in the client contracts
(`cruxible_client.contracts.workspace_root`), the layer every surface can
import; this module names it for core callers.
"""

from __future__ import annotations

from cruxible_client.contracts.workspace_root import (
    WorkspaceBoundaryRefused,
    WorkspaceRoot,
    WorkspaceRootSource,
    refuse_unusable_root,
    resolve_workspace_root,
)

__all__ = [
    "WorkspaceBoundaryRefused",
    "WorkspaceRoot",
    "WorkspaceRootSource",
    "refuse_unusable_root",
    "resolve_workspace_root",
]
