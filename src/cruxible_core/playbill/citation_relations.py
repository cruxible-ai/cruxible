"""Disposable accepted-coordinate citation relation facts.

The rows live in the existing immutable semantic-fact table.  They are neither
governed artifacts nor an operational store: an explicit projection rebuild can
rederive every byte from the accepted Claim members and Capture CAS envelopes.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from typing import TypeVar

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    FOREIGN_SOURCE_SELECTOR_TYPE,
    CaptureEnvelopeV1,
    CaptureFormatError,
    capture_contract_digest,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext, BodyProjectionProtocol
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_citation_references,
    parse_claim,
)
from cruxible_client.contracts.errors import PlaybillError, ProjectionFormatError
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1

RELATION_CONTRACT_SCHEMA = "playbill.citation_relation.capture_contract"
RELATION_USE_SCHEMA = "playbill.citation_relation.use"
RELATION_SOURCE_USE_SCHEMA = "playbill.citation_relation.source_use"
RELATION_EXTERNAL_USE_SCHEMA = "playbill.citation_relation.external_use"
RELATION_RETIRED_CONFLICT_SCHEMA = "playbill.citation_relation.retired_conflict"

_ACCESS = BodyAccessContext(principal_id="playbill-citation-relation", can_read_body=True)
_WITNESS_LIMIT = 8
_RelationValue = TypeVar("_RelationValue")


def _digest_identity(prefix: str, value: object) -> str:
    digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
    return f"{prefix}-{digest}"


def capture_relation_subject(capture_digest: str) -> str:
    return _digest_identity("capture", capture_digest)


def capture_contract_relation_subject(contract_digest: str) -> str:
    return _digest_identity("capture-contract", contract_digest)


def logical_source_relation_subject(source_id: str) -> str:
    return _digest_identity("logical-source", source_id)


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


def build_citation_relation_facts(
    tree: Mapping[str, bytes],
    *,
    bodies: BodyProjectionProtocol,
    previous_use_facts: tuple[ProjectionFact, ...] = (),
    previous_conflict_facts: tuple[ProjectionFact, ...] = (),
    changed_claim_paths: frozenset[str] | None = None,
) -> tuple[ProjectionFact, ...]:
    """Derive a coordinate-bound relation slice, re-reading changed Claims only.

    A normal candidate activation supplies the previous immutable use rows plus
    the exact changed Claim paths. Explicit rebuild/recovery omits both and is
    the deliberately global fallback. Derived conflict rows are regenerated
    deterministically from the carried use set; Capture bodies for untouched
    Claims are never reopened.
    """

    facts: list[ProjectionFact] = []
    for path in sorted(tree, key=lambda value: value.encode("utf-8")):
        if not path.startswith("capture-contracts/"):
            continue
        try:
            contract = parse_capture_contract(tree[path], path=path)
        except CaptureFormatError as exc:  # pragma: no cover - accepted-tree invariant
            raise ProjectionFormatError("citation relation saw an invalid CaptureContract") from exc
        digest = capture_contract_digest(contract).tagged
        facts.append(
            ProjectionFact(
                schema_id=RELATION_CONTRACT_SCHEMA,
                schema_version=1,
                subject_identity=capture_contract_relation_subject(digest),
                fact_key="accepted-contract",
                value={
                    "artifact_digest": {"$digest": digest},
                    "identity": contract.identity.model_dump(mode="json"),
                    "path": {"$path": path},
                },
            )
        )

    uses: list[dict[str, object]] = []
    replaced_uses: list[dict[str, object]] = []
    changed_uses: list[dict[str, object]] = []
    if changed_claim_paths is not None:
        for fact in previous_use_facts:
            if fact.schema_id != RELATION_USE_SCHEMA or not isinstance(fact.value, dict):
                raise ProjectionFormatError("previous citation use fact is malformed")
            previous_path = fact.value.get("claim_path")
            if not isinstance(previous_path, str):
                raise ProjectionFormatError("previous citation use fact has no Claim path")
            if previous_path in changed_claim_paths:
                replaced_uses.append(dict(fact.value))
            else:
                uses.append(dict(fact.value))
    envelopes_by_digest: dict[str, CaptureEnvelopeV1] = {}
    for path in sorted(tree, key=lambda value: value.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        if changed_claim_paths is not None and path not in changed_claim_paths:
            continue
        claim = parse_claim(tree[path], path=path)
        claim_digest = claim_artifact_digest(claim).tagged
        for citation in claim_citation_references(claim):
            envelope = envelopes_by_digest.get(citation.capture_digest)
            if envelope is None:
                try:
                    envelope = parse_capture_envelope(
                        bodies.read(citation.capture_digest, access=_ACCESS)
                    )
                except PlaybillError as exc:  # pragma: no cover - accepted-tree invariant
                    raise ProjectionFormatError(
                        "citation relation could not read an accepted Capture"
                    ) from exc
                envelopes_by_digest[citation.capture_digest] = envelope
            use: dict[str, object] = {
                "capture_contract_digest": {"$digest": envelope.capture_contract_digest},
                "capture_digest": {"$digest": citation.capture_digest},
                "citation_id": citation.citation_id,
                "claim_artifact_digest": {"$digest": claim_digest},
                "claim_identity": claim.identity.qualified,
                "claim_lifecycle": claim.lifecycle.state,
                "claim_path": path,
                "commitment": envelope.commitment.model_dump(mode="json"),
                "origin": getattr(citation, "origin", "legacy"),
                "role": getattr(citation, "role", "legacy"),
                "source": envelope.source.model_dump(mode="json"),
            }
            uses.append(use)
            changed_uses.append(use)

    touched_relation_keys: set[str] | None = None
    if changed_claim_paths is not None:
        touched_relation_keys = set()
        for use in (*replaced_uses, *changed_uses):
            capture = use.get("capture_digest")
            if isinstance(capture, dict) and isinstance(capture.get("$digest"), str):
                touched_relation_keys.add(_relation_group_key("capture", capture["$digest"]))
            source = use.get("source")
            if isinstance(source, dict) and source.get("kind") == "external":
                external = ExternalSourceReferenceV1.model_validate(source)
                touched_relation_keys.add(
                    _relation_group_key(
                        "exact_external",
                        external_source_relation_subject(external),
                    )
                )
                span = _same_version_span_key(use)
                if span is not None:
                    touched_relation_keys.add(_relation_group_key("same_version_span", span[0]))

        for fact in previous_conflict_facts:
            if not isinstance(fact.value, dict):
                raise ProjectionFormatError("previous citation conflict fact is malformed")
            relation_key = fact.value.get("relation_key")
            if not isinstance(relation_key, str):
                raise ProjectionFormatError("previous citation conflict has no relation key")
            if relation_key not in touched_relation_keys:
                facts.append(fact)

    for use in uses:
        capture = use.get("capture_digest")
        citation_id = use.get("citation_id")
        claim_identity = use.get("claim_identity")
        if (
            not isinstance(capture, dict)
            or not isinstance(capture.get("$digest"), str)
            or not isinstance(citation_id, str)
            or not isinstance(claim_identity, str)
        ):
            raise ProjectionFormatError("citation relation use is malformed")
        facts.append(
            ProjectionFact(
                schema_id=RELATION_USE_SCHEMA,
                schema_version=1,
                subject_identity=capture_relation_subject(capture["$digest"]),
                fact_key=_fact_key("use", citation_id, claim_identity),
                value=use,
            )
        )
        source = use.get("source")
        if isinstance(source, dict) and source.get("kind") == "external":
            try:
                external = ExternalSourceReferenceV1.model_validate(source)
            except ValueError as exc:
                raise ProjectionFormatError(
                    "citation relation external source is malformed"
                ) from exc
            source_fact = ProjectionFact(
                schema_id=RELATION_SOURCE_USE_SCHEMA,
                schema_version=1,
                subject_identity=logical_source_relation_subject(external.source_identity),
                fact_key=_fact_key("use", citation_id, claim_identity),
                value=use,
            )
            facts.append(source_fact)
            facts.append(
                source_fact.model_copy(
                    update={
                        "schema_id": RELATION_EXTERNAL_USE_SCHEMA,
                        "subject_identity": external_source_relation_subject(external),
                    }
                )
            )

    capture_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    external_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for use in uses:
        capture = use["capture_digest"]
        assert isinstance(capture, dict)
        capture_groups[str(capture["$digest"])].append(use)
        source = use["source"]
        if isinstance(source, dict) and source.get("kind") == "external":
            parsed = ExternalSourceReferenceV1.model_validate(source)
            external_groups[external_source_relation_subject(parsed)].append(use)

    emitted: set[tuple[str, str]] = set()
    for relation_kind, groups in (
        ("capture", capture_groups),
        ("exact_external", external_groups),
    ):
        for relation_key, grouped in groups.items():
            stored_relation_key = _relation_group_key(relation_kind, relation_key)
            if (
                touched_relation_keys is not None
                and stored_relation_key not in touched_relation_keys
            ):
                continue
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

    version_groups: dict[str, list[tuple[int, int, dict[str, object]]]] = defaultdict(list)
    for use in uses:
        span = _same_version_span_key(use)
        if span is not None:
            version_groups[span[0]].append((span[1], span[2], use))
    for version_key, version_uses in version_groups.items():
        stored_relation_key = _relation_group_key("same_version_span", version_key)
        if touched_relation_keys is not None and stored_relation_key not in touched_relation_keys:
            continue
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
    capture_precedence_subjects = {
        str(fact.value["live_claim_identity"])
        for fact in facts
        if fact.schema_id == RELATION_RETIRED_CONFLICT_SCHEMA
        and isinstance(fact.value, dict)
        and fact.value.get("relation_kind") == "capture"
        and isinstance(fact.value.get("live_claim_identity"), str)
    }
    return tuple(
        fact
        for fact in facts
        if not (
            fact.schema_id == RELATION_RETIRED_CONFLICT_SCHEMA
            and isinstance(fact.value, dict)
            and fact.value.get("relation_kind") != "capture"
            and fact.value.get("live_claim_identity") in capture_precedence_subjects
        )
    )


__all__ = [
    "RELATION_CONTRACT_SCHEMA",
    "RELATION_EXTERNAL_USE_SCHEMA",
    "RELATION_RETIRED_CONFLICT_SCHEMA",
    "RELATION_SOURCE_USE_SCHEMA",
    "RELATION_USE_SCHEMA",
    "build_citation_relation_facts",
    "capture_contract_relation_subject",
    "capture_relation_subject",
    "external_source_relation_subject",
    "logical_source_relation_subject",
    "retired_activation_live_candidates",
]
