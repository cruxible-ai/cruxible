"""Citation group derivation and complete conflict/witness computation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TypeVar

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    FOREIGN_SOURCE_SELECTOR_TYPE,
)
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1

RELATION_RETIRED_CONFLICT_SCHEMA = "playbill.citation_relation.retired_conflict"

_WITNESS_LIMIT = 8
_RelationValue = TypeVar("_RelationValue")


def _digest_identity(prefix: str, value: object) -> str:
    digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
    return f"{prefix}-{digest}"


def external_source_relation_subject(source: ExternalSourceReferenceV1) -> str:
    return _digest_identity(
        "external-source",
        {
            "coordinate": source.coordinate,
            "coordinate_type": source.coordinate_type,
            "selector": source.selector,
            "selector_type": source.selector_type,
            "source_identity": source.source_identity,
        },
    )


def _fact_key(prefix: str, *values: str) -> str:
    return _digest_identity(prefix, list(values))


def _relation_group_key(relation_kind: str, key: str) -> str:
    return f"{relation_kind}:{key}"


def _bounded(values: set[str]) -> list[str]:
    return sorted(values, key=lambda value: value.encode("utf-8"))[:_WITNESS_LIMIT]


def retired_activation_live_candidates(
    active_retired: Mapping[str, object],
    active_live: Mapping[str, _RelationValue],
) -> tuple[_RelationValue, ...]:
    """Visit active live spans only on a retired-set empty-to-nonempty edge."""

    if active_retired:
        return ()
    return tuple(
        active_live[key] for key in sorted(active_live, key=lambda value: value.encode("utf-8"))
    )


def _same_version_span_key(use: Mapping[str, object]) -> tuple[str, int, int] | None:
    source = use.get("source")
    if not isinstance(source, Mapping):
        return None
    if (
        source.get("kind") != "external"
        or source.get("coordinate_type") != FOREIGN_SOURCE_COORDINATE_TYPE
        or source.get("selector_type") != FOREIGN_SOURCE_SELECTOR_TYPE
        or not isinstance(source.get("source_identity"), str)
        or not isinstance(source.get("coordinate"), Mapping)
        or not isinstance(source.get("selector"), Mapping)
    ):
        return None
    selector = source["selector"]
    assert isinstance(selector, Mapping)
    window = selector.get("working_selection", selector)
    if not isinstance(window, Mapping):
        return None
    start, end = window.get("start_byte"), window.get("end_byte")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start < end
    ):
        return None
    version_key = _digest_identity(
        "source-version",
        {
            "coordinate": source["coordinate"],
            "coordinate_type": source["coordinate_type"],
            "source_identity": source["source_identity"],
        },
    )
    return version_key, start, end


def _conflict_group_facts(
    capture_groups: Mapping[str, list[dict[str, object]]],
    external_groups: Mapping[str, list[dict[str, object]]],
    version_groups: Mapping[str, list[tuple[int, int, dict[str, object]]]],
) -> list[ProjectionFact]:
    """Frozen conflicts/witnesses over normalized complete groups, without body I/O."""
    facts: list[ProjectionFact] = []
    emitted: set[tuple[str, str]] = set()
    for relation_kind, groups in (
        ("capture", capture_groups),
        ("exact_external", external_groups),
    ):
        for relation_key, grouped in groups.items():
            stored_relation_key = _relation_group_key(relation_kind, relation_key)
            retired = [item for item in grouped if item["claim_lifecycle"] == "retired"]
            live = [item for item in grouped if item["claim_lifecycle"] == "live"]
            if not retired or not live:
                continue
            retired_claims = {str(item["claim_identity"]) for item in retired}
            retired_citations = {str(item["citation_id"]) for item in retired}
            for item in live:
                live_identity = str(item["claim_identity"])
                dedup = (live_identity, relation_key)
                if dedup in emitted:
                    continue
                emitted.add(dedup)
                facts.append(
                    ProjectionFact(
                        schema_id=RELATION_RETIRED_CONFLICT_SCHEMA,
                        schema_version=1,
                        subject_identity="claim-cites-retired",
                        fact_key=_fact_key("conflict", live_identity, relation_key),
                        value={
                            "live_capture_digest": item["capture_digest"],
                            "live_citation_id": item["citation_id"],
                            "live_claim_artifact_digest": item["claim_artifact_digest"],
                            "live_claim_identity": live_identity,
                            "relation_key": stored_relation_key,
                            "relation_kind": relation_kind,
                            "retired_citation_count": len(retired_citations),
                            "retired_citation_witnesses": _bounded(retired_citations),
                            "retired_claim_count": len(retired_claims),
                            "retired_claim_witnesses": _bounded(retired_claims),
                        },
                    )
                )

    for version_key, version_uses in version_groups.items():
        stored_relation_key = _relation_group_key("same_version_span", version_key)
        span_events = [
            (position, order, str(version_use["claim_lifecycle"]), version_use)
            for start, end, version_use in version_uses
            for position, order in ((start, 1), (end, 0))
        ]
        active_retired: dict[str, dict[str, object]] = {}
        active_live: dict[str, dict[str, object]] = {}
        emitted_live: set[str] = set()

        def emit(live: dict[str, object]) -> None:
            live_identity = str(live["claim_identity"])
            if live_identity in emitted_live:
                return
            if not active_retired:
                return
            retired_claims = {str(item["claim_identity"]) for item in active_retired.values()}
            retired_citations = {str(item["citation_id"]) for item in active_retired.values()}
            facts.append(
                ProjectionFact(
                    schema_id=RELATION_RETIRED_CONFLICT_SCHEMA,
                    schema_version=1,
                    subject_identity="claim-cites-retired",
                    fact_key=_fact_key("span-conflict", live_identity, version_key),
                    value={
                        "live_capture_digest": live["capture_digest"],
                        "live_citation_id": live["citation_id"],
                        "live_claim_artifact_digest": live["claim_artifact_digest"],
                        "live_claim_identity": live_identity,
                        "relation_key": stored_relation_key,
                        "relation_kind": "same_version_span",
                        "retired_citation_count": len(retired_citations),
                        "retired_citation_witnesses": _bounded(retired_citations),
                        "retired_claim_count": len(retired_claims),
                        "retired_claim_witnesses": _bounded(retired_claims),
                    },
                )
            )
            emitted_live.add(live_identity)

        for _position, order, lifecycle, event_use in sorted(
            span_events,
            key=lambda event: (
                event[0],
                event[1],
                event[2].encode("ascii"),
                str(event[3]["citation_id"]).encode("ascii"),
            ),
        ):
            citation_id = str(event_use["citation_id"])
            if order == 0:
                if lifecycle == "retired":
                    active_retired.pop(citation_id, None)
                else:
                    active_live.pop(citation_id, None)
                continue
            if lifecycle == "retired":
                live_candidates = retired_activation_live_candidates(
                    active_retired,
                    active_live,
                )
                active_retired[citation_id] = event_use
                for active_live_use in live_candidates:
                    emit(active_live_use)
            else:
                active_live[citation_id] = event_use
                emit(event_use)
    return facts


__all__ = [
    "RELATION_RETIRED_CONFLICT_SCHEMA",
    "external_source_relation_subject",
    "retired_activation_live_candidates",
]
