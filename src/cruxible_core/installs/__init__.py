"""Authoritative install ledger: who owns which installed config object.

Composition ownership (``config/composition_ownership.py``) answers only
"upstream or local?". That is enough to decide what an overlay may WRITE, and
not nearly enough to uninstall anything: once two installable artifacts have
each contributed a contract and a named query to the same local layer, nothing
in the composed config records which artifact put which name there.

This package is the missing record. It is deliberately independent of any
artifact FORMAT: the ledger stores an artifact reference (kind, id, version,
digest) supplied by its caller, so the installer that lands later can pin a
blueprint, a kit bundle, or anything else without this module knowing the
schema.
"""

from __future__ import annotations

from cruxible_core.installs.types import (
    INSTALL_PHASES,
    LEGAL_PHASE_TRANSITIONS,
    OWNED_OBJECT_KINDS,
    TERMINAL_INSTALL_PHASES,
    ArtifactRef,
    CustomizationReport,
    InstallDetail,
    InstallPhase,
    InstallPhaseEvent,
    InstallRecord,
    ObjectReference,
    OwnedObject,
    OwnedObjectKind,
    OwnershipCollision,
    UninstallBlocker,
    UninstallPreconditionReport,
    compute_install_object_digest,
    legal_next_phases,
)

__all__ = [
    "INSTALL_PHASES",
    "LEGAL_PHASE_TRANSITIONS",
    "OWNED_OBJECT_KINDS",
    "TERMINAL_INSTALL_PHASES",
    "ArtifactRef",
    "CustomizationReport",
    "InstallDetail",
    "InstallPhase",
    "InstallPhaseEvent",
    "InstallRecord",
    "ObjectReference",
    "OwnedObject",
    "OwnedObjectKind",
    "OwnershipCollision",
    "UninstallBlocker",
    "UninstallPreconditionReport",
    "compute_install_object_digest",
    "legal_next_phases",
]
