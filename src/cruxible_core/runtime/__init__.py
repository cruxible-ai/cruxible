"""Runtime helpers shared across CLI, HTTP, and MCP surfaces."""

from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.runtime.instance_manager import get_manager

__all__ = ["CruxibleInstance", "get_manager"]
