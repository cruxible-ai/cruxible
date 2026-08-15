"""Storage backend compatibility exports.

The exports stay lazy so a focused backend such as Playbill projection storage
does not initialize the legacy graph repository merely by importing its parent
package.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "GraphRepositoryProtocol",
    "SQLiteGraphRepository",
    "SQLiteStorageBackend",
    "SQLiteUnitOfWork",
    "StorageBackendProtocol",
    "StorageDatabaseError",
    "StorageIntegrityError",
    "UnitOfWorkProtocol",
]

_EXPORT_MODULES = {
    "GraphRepositoryProtocol": "cruxible_core.storage.protocols",
    "StorageBackendProtocol": "cruxible_core.storage.protocols",
    "UnitOfWorkProtocol": "cruxible_core.storage.protocols",
    "SQLiteGraphRepository": "cruxible_core.storage.sqlite",
    "SQLiteStorageBackend": "cruxible_core.storage.sqlite",
    "SQLiteUnitOfWork": "cruxible_core.storage.sqlite",
    "StorageDatabaseError": "cruxible_core.storage.sqlite",
    "StorageIntegrityError": "cruxible_core.storage.sqlite",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
