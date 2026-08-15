"""Runtime helpers shared across CLI, HTTP, and MCP surfaces.

Legacy exports stay lazy so importing an independent runtime submodule (notably
the Playbill facade) does not initialize the graph/config runtime as a side
effect.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["CruxibleInstance", "get_manager"]


def __getattr__(name: str) -> Any:
    if name == "CruxibleInstance":
        value = import_module("cruxible_core.runtime.instance").CruxibleInstance
    elif name == "get_manager":
        value = import_module("cruxible_core.runtime.instance_manager").get_manager
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
