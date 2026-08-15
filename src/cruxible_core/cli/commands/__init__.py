"""Lazy exports for the surviving Playbill CLI command modules."""

from __future__ import annotations

import importlib
from typing import Any

_COMMAND_MODULES = {
    "connect_group": "context",
    "credential_group": "credentials",
    "playbill_group": "playbill",
    "server_group": "server",
}


def __getattr__(name: str) -> Any:
    module_name = _COMMAND_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{module_name}")
    command = getattr(module, name)
    globals()[name] = command
    return command


def __dir__() -> list[str]:
    return sorted({*globals(), *_COMMAND_MODULES})


__all__ = list(_COMMAND_MODULES)
