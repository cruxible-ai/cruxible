"""Frozen registry and dispatch for core-owned resolution-contract subjects."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from cruxible_core.errors import (
    MalformedReservedSubjectError,
    ReservedSubjectError,
    RetiredReservedKindError,
    UnknownReservedSubjectError,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import ProcedureStoreProtocol
from cruxible_core.query.entity_state import entity_matches_query_state
from cruxible_core.resolution_contracts.types import compute_entity_content_digest

ReservedKindStatus = Literal["live", "retired"]


@dataclass(frozen=True)
class ReservedSubjectKind:
    """One append-only reserved-kind registration."""

    status: ReservedKindStatus
    opener_dispatch_key: str
    subject_resolver_dispatch_key: str


@dataclass(frozen=True)
class SubjectResolution:
    """Read-time state of one contract subject."""

    present: bool
    content_digest: str | None
    live: bool


_PROCEDURE_KIND = ReservedSubjectKind(
    status="live",
    opener_dispatch_key="Procedure",
    subject_resolver_dispatch_key="Procedure",
)

RESERVED_SUBJECT_KINDS: Mapping[str, ReservedSubjectKind] = MappingProxyType(
    {"Procedure": _PROCEDURE_KIND}
)
"""Immutable append-only registry. Keys are case-sensitive and never reused."""

_RESERVED_NAME = re.compile(r"^cruxible\.([^.]+)$")


def _authorize_procedure_open(internal_authority: bool) -> None:
    if not internal_authority:
        raise ReservedSubjectError(
            "resolution contract subject kind 'cruxible.Procedure' is reserved; "
            "contracts for it are opened only by procedure acceptance"
        )


def _resolve_procedure_subject(
    procedure_store: ProcedureStoreProtocol | None,
    entity_id: str,
) -> SubjectResolution:
    if procedure_store is None:
        return SubjectResolution(present=False, content_digest=None, live=False)
    procedure = procedure_store.get_procedure(entity_id)
    if procedure is None:
        return SubjectResolution(present=False, content_digest=None, live=False)
    return SubjectResolution(
        present=True,
        content_digest=procedure.definition_digest,
        live=procedure.status == "live",
    )


RESERVED_SUBJECT_OPENERS: Mapping[str, Callable[[bool], None]] = MappingProxyType(
    {"Procedure": _authorize_procedure_open}
)
RESERVED_SUBJECT_RESOLVERS: Mapping[
    str, Callable[[ProcedureStoreProtocol | None, str], SubjectResolution]
] = MappingProxyType({"Procedure": _resolve_procedure_subject})


def classify_reserved_subject_for_open(
    entity_type: str,
    *,
    internal_authority: bool,
) -> ReservedSubjectKind | None:
    """Classify before any subject lookup, declaration check, or replay check."""
    match = _RESERVED_NAME.fullmatch(entity_type)
    if match is None:
        if entity_type == "cruxible" or entity_type.startswith("cruxible."):
            raise MalformedReservedSubjectError(
                "reserved resolution-contract subjects use exactly "
                "'cruxible.<Kind>' with one non-empty case-sensitive kind segment"
            )
        return None

    kind_name = match.group(1)
    entry = RESERVED_SUBJECT_KINDS.get(kind_name)
    if entry is None:
        raise UnknownReservedSubjectError(
            f"reserved resolution-contract subject kind 'cruxible.{kind_name}' is unknown"
        )
    if entry.status == "retired":
        raise RetiredReservedKindError(
            f"reserved resolution-contract subject kind 'cruxible.{kind_name}' is retired; "
            "existing contracts remain readable but no new contract may be opened"
        )
    RESERVED_SUBJECT_OPENERS[entry.opener_dispatch_key](internal_authority)
    return entry


def resolve_contract_subject(
    graph: EntityGraph,
    procedure_store: ProcedureStoreProtocol | None,
    *,
    entity_type: str,
    entity_id: str,
) -> SubjectResolution:
    """Resolve presence, digest, and liveness through one registry-aware seam."""
    match = _RESERVED_NAME.fullmatch(entity_type)
    if match is not None:
        entry = RESERVED_SUBJECT_KINDS.get(match.group(1))
        if entry is None:
            return SubjectResolution(present=False, content_digest=None, live=False)
        resolver = RESERVED_SUBJECT_RESOLVERS[entry.subject_resolver_dispatch_key]
        # Retirement blocks only opening. Resolver registrations remain usable
        # forever so historical contracts stay listable and verifiable.
        return resolver(procedure_store, entity_id)
    if entity_type == "cruxible" or entity_type.startswith("cruxible."):
        return SubjectResolution(present=False, content_digest=None, live=False)

    subject = graph.get_entity(entity_type, entity_id)
    if subject is None:
        return SubjectResolution(present=False, content_digest=None, live=False)
    return SubjectResolution(
        present=True,
        content_digest=compute_entity_content_digest(
            subject.entity_type,
            subject.entity_id,
            dict(subject.properties),
        ),
        live=bool(entity_matches_query_state(subject.metadata, "live")),
    )


__all__ = [
    "RESERVED_SUBJECT_KINDS",
    "RESERVED_SUBJECT_OPENERS",
    "RESERVED_SUBJECT_RESOLVERS",
    "ReservedSubjectKind",
    "SubjectResolution",
    "classify_reserved_subject_for_open",
    "resolve_contract_subject",
]
