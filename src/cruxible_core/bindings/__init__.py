"""Compute-slot binding ledger: which provider fills a procedure's slot here.

Bindings are DEPLOYMENT RECORDS held in state, never configuration. See
``cruxible_core.bindings.types`` for the shapes and
``cruxible_core.service.bindings`` for the governed verbs.
"""

from cruxible_core.bindings.store import BindingStore, BindingStoreProtocol
from cruxible_core.bindings.types import (
    BINDING_CHANGE_KINDS,
    BINDING_STATUSES,
    BindingChangeKind,
    BindingStatus,
    BindingWriteResult,
    NearMatchCandidate,
    NearMatchReport,
    ProviderDescriptor,
    SlotBinding,
    SlotBindingHistoryResult,
    SlotBindingListResult,
    SlotBindingRevision,
    SlotInterface,
)

__all__ = [
    "BINDING_CHANGE_KINDS",
    "BINDING_STATUSES",
    "BindingChangeKind",
    "BindingStatus",
    "BindingStore",
    "BindingStoreProtocol",
    "BindingWriteResult",
    "NearMatchCandidate",
    "NearMatchReport",
    "ProviderDescriptor",
    "SlotBinding",
    "SlotBindingHistoryResult",
    "SlotBindingListResult",
    "SlotBindingRevision",
    "SlotInterface",
]
