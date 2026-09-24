"""What a Claim shares with retired Claims, as review context for its explanation.

Retiring a Claim does not retire the evidence it cited. A live Claim that
shares a capture, an exact external source, or a cited span with a retired one
is not broken by that, and a retired Claim whose cited passage is still in a
document is not work by itself; both are what a reviewer wants to see beside
the Claim. A real dependency on a retired Claim is a queue row of its own
(`claim_dependency_stale`) and is not repeated here.

Accepted-state relations come from the bound citation projection. Relations
that need the current bytes of a source -- a span a retired Claim cited that
now overlaps this Claim's, or a retired passage no live Claim covers -- come
only from an explicit workspace observation, exactly as `next` observes it;
the daemon reads no workspace.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cruxible_client.contracts.canonical import Sha256Value
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    FOREIGN_SOURCE_SELECTOR_TYPE,
)
from cruxible_client.contracts.documents import document_path
from cruxible_client.contracts.errors import PlaybillError, ProposalIntegrityError
from cruxible_core.coverage.contracts import LogicalSourceIdentityV1
from cruxible_core.coverage.indexes import WorkingOccurrenceV1
from cruxible_core.indexes.evidence.citation_sql import CitationSourceUse
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance

if TYPE_CHECKING:
    from cruxible_core.service.discovery.next import (
        PlaybillNextSourceObservationV4,
        PlaybillNextWorkspaceObservationV1,
    )

_WITNESS_LIMIT = 8
#: Accepted-state relation kinds, strongest first.
_ACCEPTED_RELATION_ORDER = ("capture", "exact_external", "same_version_span")

RetiredRelationKind = Literal[
    "capture",
    "exact_external",
    "same_version_span",
    "current_span_overlap",
]


class _StrictRetirementContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimWorkingSpanV1(_StrictRetirementContextModel):
    """A byte window in the observed current bytes of one workspace source."""

    tag: Literal["playbill-claim-working-span-v1"] = "playbill-claim-working-span-v1"
    source_id: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)


class ClaimRetiredRelationV1(_StrictRetirementContextModel):
    """One piece of evidence or one cited span this Claim shares with retired Claims."""

    tag: Literal["playbill-claim-retired-relation-v1"] = "playbill-claim-retired-relation-v1"
    relation_kind: RetiredRelationKind
    live_citation_id: str
    live_capture_digest: str
    # The accepted citation group both sides belong to; absent on a span that
    # overlaps only in the observed current bytes.
    relation_key: str | None = Field(default=None, exclude_if=lambda value: value is None)
    working_span: ClaimWorkingSpanV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    retired_claim_count: int = Field(ge=1)
    retired_claim_witnesses: tuple[str, ...]
    retired_citation_count: int = Field(ge=1)
    retired_citation_witnesses: tuple[str, ...]

    @model_validator(mode="after")
    def _shape(self) -> "ClaimRetiredRelationV1":
        Sha256Value.from_tagged(self.live_capture_digest)
        observed = self.relation_kind == "current_span_overlap"
        if observed != (self.working_span is not None) or observed == (
            self.relation_key is not None
        ):
            raise ValueError(
                "an observed span overlap carries a working span; an accepted relation its key"
            )
        return self


class RetiredClaimSourceSpanV1(_StrictRetirementContextModel):
    """A passage retired Claims cited that is still in a document and no live Claim covers."""

    tag: Literal["playbill-retired-claim-source-span-v1"] = "playbill-retired-claim-source-span-v1"
    document_id: str
    source_id: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    retired_claim_count: int = Field(ge=1)
    retired_claim_witnesses: tuple[str, ...]
    retired_citation_count: int = Field(ge=1)
    occurrence_identity_witnesses: tuple[str, ...]


class ClaimRetirementContextV1(_StrictRetirementContextModel):
    tag: Literal["playbill-claim-retirement-context-v1"] = "playbill-claim-retirement-context-v1"
    shared_with_retired: tuple[ClaimRetiredRelationV1, ...] = ()
    retired_source_spans: tuple[RetiredClaimSourceSpanV1, ...] = ()


def _bounded(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda value: value.encode("utf-8"))[:_WITNESS_LIMIT])


def _digest_value(value: object) -> str | None:
    if isinstance(value, Mapping) and isinstance(value.get("$digest"), str):
        value = value["$digest"]
    if not isinstance(value, str):
        return None
    try:
        Sha256Value.from_tagged(value)
    except ValueError:
        return None
    return value


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("retired relation count is not an integer")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError("retired relation witnesses are not strings")
    return tuple(cast(Iterable[str], value))


def _accepted_relation(value: object) -> ClaimRetiredRelationV1:
    if not isinstance(value, Mapping):
        raise ValueError("retired conflict has an invalid value")
    capture = _digest_value(value.get("live_capture_digest"))
    if capture is None:
        raise ValueError("retired conflict has no live capture digest")
    return ClaimRetiredRelationV1.model_validate(
        {
            "relation_kind": value.get("relation_kind"),
            "live_citation_id": value.get("live_citation_id"),
            "live_capture_digest": capture,
            "relation_key": value.get("relation_key"),
            "retired_claim_count": _count(value.get("retired_claim_count")),
            "retired_claim_witnesses": _strings(value.get("retired_claim_witnesses")),
            "retired_citation_count": _count(value.get("retired_citation_count")),
            "retired_citation_witnesses": _strings(value.get("retired_citation_witnesses")),
        }
    )


def _unique_occurrence(
    use: CitationSourceUse,
    observed: PlaybillNextSourceObservationV4,
) -> WorkingOccurrenceV1 | None:
    expected_source = LogicalSourceIdentityV1(plane="external", identity=use.source_identity)
    if observed.scan_notes or observed.marker_notes:
        return None
    if not any(
        proof.source == expected_source
        and proof.commitment_digest == use.commitment_digest
        and proof.byte_length == use.byte_length
        for proof in observed.commitment_scan_proofs
    ):
        return None
    occurrences = tuple(
        occurrence
        for occurrence in observed.occurrences
        if occurrence.source == expected_source
        and occurrence.observed_commitment_digest == use.commitment_digest
        and occurrence.byte_length == use.byte_length
    )
    return occurrences[0] if len(occurrences) == 1 else None


_Placed = tuple[int, int, CitationSourceUse, WorkingOccurrenceV1]


def _placed_uses(
    uses: Iterable[CitationSourceUse],
    observed: PlaybillNextSourceObservationV4,
) -> list[_Placed]:
    """Place each accepted use at its one unambiguous occurrence in the current bytes."""

    placed: list[_Placed] = []
    for use in uses:
        if (
            use.coordinate_type != FOREIGN_SOURCE_COORDINATE_TYPE
            or use.selector_type != FOREIGN_SOURCE_SELECTOR_TYPE
        ):
            continue
        occurrence = _unique_occurrence(use, observed)
        if (
            occurrence is None
            or occurrence.line_overlay.start_byte >= occurrence.line_overlay.end_byte
        ):
            continue
        placed.append(
            (
                occurrence.line_overlay.start_byte,
                occurrence.line_overlay.end_byte,
                use,
                occurrence,
            )
        )
    return placed


def _span_overlaps(
    identity: str,
    source_id: str,
    placed: list[_Placed],
) -> list[ClaimRetiredRelationV1]:
    retired = [entry for entry in placed if entry[2].lifecycle == "retired"]
    relations: list[ClaimRetiredRelationV1] = []
    for start, end, use, _occurrence in placed:
        if use.claim_identity != identity or use.lifecycle != "live":
            continue
        overlapping = [entry[2] for entry in retired if entry[0] < end and start < entry[1]]
        if not overlapping:
            continue
        claims = {entry.claim_identity for entry in overlapping}
        citations = {entry.citation_id for entry in overlapping}
        relations.append(
            ClaimRetiredRelationV1(
                relation_kind="current_span_overlap",
                live_citation_id=use.citation_id,
                live_capture_digest=use.capture_digest,
                working_span=ClaimWorkingSpanV1(
                    source_id=source_id, start_byte=start, end_byte=end
                ),
                retired_claim_count=len(claims),
                retired_claim_witnesses=_bounded(claims),
                retired_citation_count=len(citations),
                retired_citation_witnesses=_bounded(citations),
            )
        )
    return relations


def _uncovered_retired_spans(
    identity: str,
    *,
    document_id: str,
    source_id: str,
    placed: list[_Placed],
) -> list[RetiredClaimSourceSpanV1]:
    """Retired passages, merged where they touch, that no live Claim's span covers."""

    live_union: list[tuple[int, int]] = []
    for start, end in sorted(
        (entry[0], entry[1]) for entry in placed if entry[2].lifecycle == "live"
    ):
        if live_union and start <= live_union[-1][1]:
            live_union[-1] = (live_union[-1][0], max(live_union[-1][1], end))
        else:
            live_union.append((start, end))
    uncovered = [
        entry
        for entry in placed
        if entry[2].lifecycle == "retired"
        and not any(
            entry[0] < live_end and entry[1] > live_start for live_start, live_end in live_union
        )
    ]
    components: list[list[_Placed]] = []
    for entry in sorted(
        uncovered, key=lambda item: (item[0], item[1], item[2].citation_id.encode("ascii"))
    ):
        if components and entry[0] <= max(item[1] for item in components[-1]):
            components[-1].append(entry)
        else:
            components.append([entry])
    spans: list[RetiredClaimSourceSpanV1] = []
    for component in components:
        claims = {entry[2].claim_identity for entry in component}
        if identity not in claims:
            continue
        spans.append(
            RetiredClaimSourceSpanV1(
                document_id=document_id,
                source_id=source_id,
                start_byte=min(entry[0] for entry in component),
                end_byte=max(entry[1] for entry in component),
                retired_claim_count=len(claims),
                retired_claim_witnesses=_bounded(claims),
                retired_citation_count=len({entry[2].citation_id for entry in component}),
                occurrence_identity_witnesses=_bounded(
                    entry[3].identity_digest for entry in component
                ),
            )
        )
    return spans


def validate_retirement_workspace_observation(
    value: PlaybillNextWorkspaceObservationV1 | Mapping[str, object] | None,
) -> PlaybillNextWorkspaceObservationV1 | None:
    """Accept the same workspace observation `next` reads, refusing a malformed one."""

    from cruxible_core.service.discovery.next import (
        PlaybillNextWorkspaceObservationInvalid,
        PlaybillNextWorkspaceObservationV1,
    )

    if value is None or isinstance(value, PlaybillNextWorkspaceObservationV1):
        return value
    try:
        return PlaybillNextWorkspaceObservationV1.model_validate(value)
    except ValidationError as exc:
        raise PlaybillNextWorkspaceObservationInvalid(
            f"{PlaybillNextWorkspaceObservationInvalid.code}: {exc}"
        ) from exc


def claim_retirement_context(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claim_identity: str,
    cited_sources: frozenset[str],
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> ClaimRetirementContextV1 | None:
    """Read one Claim's retirement relations; None when it shares nothing with a retired one."""

    from cruxible_core.service.discovery.next import PlaybillNextSourceObservationV4

    observed_sources = {
        item.source_id: item
        for item in (() if observation is None else observation.source_observations or ())
        if isinstance(item, PlaybillNextSourceObservationV4) and item.source_id in cited_sources
    }
    try:
        with instance.bind_accepted_projection(coordinate) as projection:
            accepted = [
                _accepted_relation(fact.value)
                for fact in projection.citations.conflicts(claim_identities=(claim_identity,))
                if isinstance(fact.value, Mapping)
                and fact.value.get("live_claim_identity") == claim_identity
            ]
            uses_by_source = {
                source_id: projection.citations.uses_for_source(source_id)
                for source_id in sorted(observed_sources, key=lambda item: item.encode("utf-8"))
            }
    except (PlaybillError, ValueError, ValidationError) as exc:
        raise ProposalIntegrityError(
            "playbill.claim.retirement_context_invalid: citation relation projection is invalid"
        ) from exc

    relations = sorted(
        accepted,
        key=lambda item: (
            _ACCEPTED_RELATION_ORDER.index(item.relation_kind),
            (item.relation_key or "").encode("utf-8"),
        ),
    )
    spans: list[RetiredClaimSourceSpanV1] = []
    for source_id, uses in uses_by_source.items():
        if not any(use.claim_identity == claim_identity for use in uses):
            continue
        observed = observed_sources[source_id]
        placed = _placed_uses(uses, observed)
        # An accepted relation already names what this Claim shares; the
        # current bytes add a finding only where accepted state has none.
        if not accepted:
            relations.extend(_span_overlaps(claim_identity, source_id, placed))
        document_id = observed.document_id
        if (
            document_id is not None
            and instance.blob_at(coordinate.git_oid, document_path(document_id)) is not None
        ):
            spans.extend(
                _uncovered_retired_spans(
                    claim_identity,
                    document_id=document_id,
                    source_id=source_id,
                    placed=placed,
                )
            )
    if not relations and not spans:
        return None
    return ClaimRetirementContextV1(
        shared_with_retired=tuple(relations),
        retired_source_spans=tuple(spans),
    )


__all__ = [
    "ClaimRetiredRelationV1",
    "ClaimRetirementContextV1",
    "ClaimWorkingSpanV1",
    "RetiredClaimSourceSpanV1",
    "claim_retirement_context",
    "validate_retirement_workspace_observation",
]
