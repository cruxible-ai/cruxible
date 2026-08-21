"""One coverage resolver: the only place the two indexes are joined.

`resolve_coverage` is semantically side-effect-free in the §11.1 sense. It reads
the reverse evidence index, the working-occurrence overlay, and the published
manifest, and it returns cards. It writes no accepted state, no candidate, no
permission, no verdict input, and no evaluation episode; it does not compile,
propose, or accept; and it holds no seam through which a caller could make it
do any of those. That is not a convention here -- the module imports nothing
from the proposal, settlement, activation, compiler, or ledger-write paths, and
an architecture test asserts the closure stays that way.

The join, stated once
---------------------
For one working occurrence, with observed commitment `d` in logical source `S`:

* a citation of `d` whose accepted logical source **is** `S` is a verified
  match -- same logical source, same selection, same commitment -- and the
  occurrence is `exact`, wherever in `S` it currently sits;
* a citation of `d` whose accepted logical source is anything else, `None`
  included, is `content_equivalent`: identical bytes at a foreign occurrence,
  labeled, resolving no equivalence and inheriting no governance;
* several indistinguishable occurrences of `d` in `S` are an explicit
  ambiguity: every one of them gets a card, none of them is bound, and the span
  never reports `exact`;
* a citation whose accepted logical source is `S` and whose commitment is
  observed nowhere in `S` is `drifted`, provided the absence was actually looked
  for -- and if it was not, the boundary is `partial` rather than a drift claim
  the scan cannot support.

Coverage is a byte question
---------------------------
Only `exact_bytes` commitments participate. A `canonical_value`,
`query_result`, or `provider_statement` commitment is not addressable by a
working-source occurrence at all -- there are no bytes in the file to compare it
to -- so it is a dereference question for `open_source`, not an occurrence
question for this resolver, and skipping it costs no completeness.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, TypeAlias, cast

from cruxible_core.playbill.coverage.contracts import (
    COVERAGE_HEALTH_PROVES_FRESHNESS,
    CoverageAccessProfileV1,
    CoverageBatchSummaryV1,
    CoverageBatchSummaryV2,
    CoverageCardV1,
    CoverageCardV2,
    CoverageHealthV1,
    CoverageMatchStateV1,
    CoverageRequestV1,
    CoverageResultV1,
    CoverageResultV2,
    CoverageSpanRequestV1,
    CoverageSpanResultV1,
    CoverageSpanResultV2,
    LogicalSourceIdentityV1,
    strongest_match_state,
    weakest_health,
)
from cruxible_core.playbill.coverage.indexes import (
    EvidenceCitationIndexV1,
    EvidenceCitationIndexV2,
    EvidenceCitationV1,
    EvidenceCitationV2,
    WorkingOccurrenceOverlayV1,
    WorkingOccurrenceV1,
    evidence_citation_index_digest,
    working_occurrence_overlay_digest,
)
from cruxible_core.playbill.coverage.manifest import (
    CoverageManifestBodyV1,
    CoverageManifestBodyV2,
    coverage_manifest_digest,
    coverage_manifest_digest_v2,
)
from cruxible_core.playbill.discovery import DiscoveryMatchBasis
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.source_references import CoverageDescriptorV1

COVERAGE_FACET = "coverage"

CoverageIndexAny: TypeAlias = EvidenceCitationIndexV1 | EvidenceCitationIndexV2
CoverageCitationAny: TypeAlias = EvidenceCitationV1 | EvidenceCitationV2
CoverageCardAny: TypeAlias = CoverageCardV1 | CoverageCardV2
CoverageManifestAny: TypeAlias = CoverageManifestBodyV1 | CoverageManifestBodyV2
CoverageSpanResultAny: TypeAlias = CoverageSpanResultV1 | CoverageSpanResultV2
CoverageResultAny: TypeAlias = CoverageResultV1 | CoverageResultV2


def _manifest_floor(
    manifest: CoverageManifestAny | None,
    *,
    request: CoverageRequestV1,
    index_digest: str,
) -> tuple[CoverageHealthV1, tuple[str, ...]]:
    """The freshness floor the whole batch inherits from the published manifest.

    The overlay digest is deliberately *not* a staleness trigger. A harness that
    resolves against a snapshot newer than the last publication is the ordinary
    case, and declaring every source stale because one file changed would be the
    coarse answer; the per-source commitment comparison below is the precise one.
    """

    if manifest is None:
        return "unavailable", ("manifest_absent",)
    if manifest.instance_id != request.instance_id:
        return "stale", ("manifest_instance_mismatch",)
    if manifest.at != request.at:
        return "stale", ("manifest_coordinate_mismatch",)
    if manifest.index_digest != index_digest:
        return "stale", ("manifest_index_mismatch",)
    if manifest.watcher_health in {"degraded", "overflowed"}:
        return "stale", ("watcher_unhealthy",)
    if manifest.completeness == "partial":
        return "partial", manifest.truncation_reason_codes
    return "complete", ()


def _source_floor(
    span: CoverageSpanRequestV1,
    *,
    manifest: CoverageManifestAny | None,
    overlay: WorkingOccurrenceOverlayV1,
) -> tuple[CoverageHealthV1, tuple[str, ...]]:
    """The freshness floor for exactly one working source."""

    observed = overlay.commitment_for(span.source)
    if observed is None:
        return "unavailable", ("source_not_observed",)
    if manifest is None:
        return "complete", ()
    if not manifest.scope.covers(span.source):
        return "unavailable", ("source_outside_declared_scope",)
    published = manifest.commitment_for(span.source)
    if published is None or published.content_digest != observed.content_digest:
        return "stale", ("manifest_snapshot_superseded",)
    return "complete", ()


def _selected(
    occurrence: WorkingOccurrenceV1,
    span: CoverageSpanRequestV1,
) -> bool:
    """Whether a requested window touches this occurrence.

    The window is compared against the presentation overlay, which is the only
    place a byte offset legitimately appears: the caller asked about a region of
    the file it is looking at, and that question is about presentation. Nothing
    about the occurrence's identity is consulted.
    """

    if span.selection is None:
        return True
    return (
        occurrence.line_overlay.start_byte < span.selection.end_byte
        and span.selection.start_byte < occurrence.line_overlay.end_byte
    )


def _same_source(
    citation: CoverageCitationAny,
    source: LogicalSourceIdentityV1,
) -> bool:
    return (
        citation.accepted_source is not None
        and citation.accepted_source.sort_key == source.sort_key
    )


def _budgeted(
    cards: Sequence[CoverageCardAny],
    request: CoverageRequestV1,
) -> tuple[tuple[CoverageCardAny, ...], int]:
    """Clip from the low-priority tail and report exactly what was dropped."""

    ordered = sorted(cards, key=lambda item: item.sort_key)
    kept: list[CoverageCardAny] = []
    candidates = 0
    for card in ordered:
        if len(kept) >= request.budget.max_cards_per_span:
            break
        if card.match_state == "candidate":
            if candidates >= request.budget.max_candidate_cards_per_span:
                continue
            candidates += 1
        kept.append(card)
    return tuple(kept), len(ordered) - len(kept)


def _resolve_span(
    span: CoverageSpanRequestV1,
    *,
    request: CoverageRequestV1,
    index: CoverageIndexAny,
    overlay: WorkingOccurrenceOverlayV1,
    access: CoverageAccessProfileV1,
    batch_floor: CoverageHealthV1,
    batch_reasons: tuple[str, ...],
    manifest: CoverageManifestAny | None,
) -> CoverageSpanResultAny:
    source_floor, source_reasons = _source_floor(span, manifest=manifest, overlay=overlay)
    health = weakest_health(batch_floor, source_floor)
    reasons = set(batch_reasons) | set(source_reasons)
    if overlay.truncated:
        health = weakest_health(health, "partial")
        reasons.update(overlay.truncation_reason_codes)
    if index.truncated:
        health = weakest_health(health, "partial")
        reasons.add("evidence_index_truncated")

    occurrences = overlay.occurrences_for(span.source)
    duplicates: dict[str, int] = {}
    for item in occurrences:
        duplicates[item.observed_commitment_digest] = (
            duplicates.get(item.observed_commitment_digest, 0) + 1
        )

    cards: list[CoverageCardAny] = []
    ambiguous = 0
    withheld = False

    for occurrence in occurrences:
        if not _selected(occurrence, span):
            continue
        for citation in index.by_commitment(occurrence.observed_commitment_digest):
            if citation.digest_kind != "exact_bytes":
                continue
            if not access.permits(citation.access_class):
                withheld = True
                continue
            if not _same_source(citation, span.source):
                cards.append(
                    _card(
                        "candidate",
                        citation=citation,
                        occurrence=occurrence,
                        at=request.at,
                        basis="content_equivalent",
                        reason_codes=("foreign_occurrence",),
                    )
                )
                continue
            if duplicates[occurrence.observed_commitment_digest] > 1:
                ambiguous += 1
                cards.append(
                    _card(
                        "candidate",
                        citation=citation,
                        occurrence=occurrence,
                        at=request.at,
                        reason_codes=("occurrence_ambiguous",),
                    )
                )
                continue
            if not COVERAGE_HEALTH_PROVES_FRESHNESS[health]:
                cards.append(
                    _card(
                        "candidate",
                        citation=citation,
                        occurrence=occurrence,
                        at=request.at,
                        reason_codes=("freshness_unprovable",),
                    )
                )
                continue
            cards.append(_card("exact", citation=citation, occurrence=occurrence, at=request.at))

    observed_digests = {item.observed_commitment_digest for item in occurrences}
    whole = overlay.commitment_for(span.source)
    if whole is not None:
        # Drift is the *absence* of an occurrence, so a requested window cannot
        # filter it: there is no occurrence for the window to contain.
        for citation in index.by_logical_source(span.source):
            if citation.digest_kind != "exact_bytes":
                continue
            if citation.commitment_digest in observed_digests:
                continue
            if not overlay.scanned(citation.commitment_digest):
                # Absence was never looked for, so it is a gap in the boundary
                # rather than evidence that the cited content changed.
                health = weakest_health(health, "partial")
                reasons.add("unscanned_selection")
                continue
            if not access.permits(citation.access_class):
                withheld = True
                continue
            cards.append(
                _drift_card(
                    citation=citation,
                    observed_commitment_digest=whole.content_digest,
                    source=span.source,
                    at=request.at,
                )
            )

    if withheld:
        if access.disclose_restricted_existence:
            health = weakest_health(health, "denied")
            reasons.add("restricted_access_class")
        else:
            # Non-disclosure: report the boundary incomplete without naming, or
            # even admitting the existence of, the restricted material.
            health = weakest_health(health, "partial")
            reasons.add("boundary_incomplete")

    kept, omitted = _budgeted(cards, request)
    if omitted:
        health = weakest_health(health, "partial")
        reasons.add("card_budget_exceeded")
    if ambiguous and not any(card.match_state == "candidate" for card in kept):
        # The ambiguity cards were what the budget dropped; keeping the count
        # without a card would state an ambiguity the reader cannot inspect.
        ambiguous = 0

    match_state: CoverageMatchStateV1 = strongest_match_state(card.match_state for card in kept)

    payload = dict(
        request=span,
        match_state=match_state,
        health=health,
        absence_is_factual=match_state == "none" and health == "complete",
        ambiguous_occurrence_count=ambiguous,
        omitted_card_count=omitted,
        coverage=CoverageDescriptorV1(
            requested_facets=(COVERAGE_FACET,),
            available_facets=(COVERAGE_FACET,) if kept else (),
            omitted_for_access=(
                (COVERAGE_FACET,) if withheld and access.disclose_restricted_existence else ()
            ),
            truncated_facets=(COVERAGE_FACET,) if omitted else (),
            reason_codes=byte_sorted(tuple(reasons)),
        ),
    )
    if isinstance(index, EvidenceCitationIndexV2):
        return CoverageSpanResultV2.model_validate(
            {**payload, "cards": cast(tuple[CoverageCardV2, ...], kept)}
        )
    return CoverageSpanResultV1.model_validate({**payload, "cards": kept})


def _card(
    match_state: Literal["exact", "drifted", "candidate"],
    *,
    citation: CoverageCitationAny,
    occurrence: WorkingOccurrenceV1,
    at: AcceptedCoordinate,
    basis: DiscoveryMatchBasis | None = None,
    reason_codes: tuple[str, ...] = (),
) -> CoverageCardAny:
    payload = dict(
        match_state=match_state,
        match_basis=basis,
        at=at,
        claim_addresses=citation.claim_addresses,
        capture_digests=citation.capture_digests,
        expected_commitment_digest=citation.commitment_digest,
        observed_commitment_digest=occurrence.observed_commitment_digest,
        accepted_source=citation.accepted_source,
        observed_source=occurrence.source,
        occurrence_identity_digest=occurrence.identity_digest,
        line_overlay=occurrence.line_overlay,
        dereference_handle_digest=citation.dereference_handle_digest,
        dependent_claim_count=citation.dependent_claim_count,
        reason_codes=byte_sorted(reason_codes),
    )
    if isinstance(citation, EvidenceCitationV2):
        return CoverageCardV2.model_validate(
            {**payload, "citation_associations": citation.citation_associations}
        )
    return CoverageCardV1.model_validate(payload)


def _drift_card(
    *,
    citation: CoverageCitationAny,
    observed_commitment_digest: str,
    source: LogicalSourceIdentityV1,
    at: AcceptedCoordinate,
) -> CoverageCardAny:
    payload = dict(
        match_state="drifted",
        at=at,
        claim_addresses=citation.claim_addresses,
        capture_digests=citation.capture_digests,
        expected_commitment_digest=citation.commitment_digest,
        observed_commitment_digest=observed_commitment_digest,
        accepted_source=source,
        observed_source=source,
        dereference_handle_digest=citation.dereference_handle_digest,
        dependent_claim_count=citation.dependent_claim_count,
        reason_codes=("commitment_superseded",),
    )
    if isinstance(citation, EvidenceCitationV2):
        return CoverageCardV2.model_validate(
            {**payload, "citation_associations": citation.citation_associations}
        )
    return CoverageCardV1.model_validate(payload)


def _resolve_coverage_any(
    request: CoverageRequestV1,
    *,
    index: CoverageIndexAny,
    overlay: WorkingOccurrenceOverlayV1,
    access: CoverageAccessProfileV1,
    manifest: CoverageManifestAny | None = None,
) -> CoverageResultAny:
    is_v2 = isinstance(index, EvidenceCitationIndexV2)
    if is_v2 != isinstance(manifest, CoverageManifestBodyV2) and manifest is not None:
        raise ValueError("coverage index and manifest versions must agree")

    index_digest = evidence_citation_index_digest(index)
    overlay_digest = working_occurrence_overlay_digest(overlay)
    if index.at != request.at:
        raise ValueError("coverage resolves against one accepted coordinate at a time")

    batch_floor, batch_reasons = _manifest_floor(
        manifest, request=request, index_digest=index_digest
    )
    spans = tuple(
        _resolve_span(
            span,
            request=request,
            index=index,
            overlay=overlay,
            access=access,
            batch_floor=batch_floor,
            batch_reasons=batch_reasons,
            manifest=manifest,
        )
        for span in request.spans
    )

    counts = {state: 0 for state in ("exact", "drifted", "candidate", "none")}
    for span_result in spans:
        counts[span_result.match_state] += 1
    health = weakest_health(*(item.health for item in spans))
    reasons = {code for item in spans for code in item.coverage.reason_codes}
    truncated = any(item.coverage.truncated_facets for item in spans)
    withheld = any(item.coverage.omitted_for_access for item in spans)
    manifest_digest = None
    if isinstance(manifest, CoverageManifestBodyV2):
        manifest_digest = coverage_manifest_digest_v2(manifest).tagged
    elif manifest is not None:
        manifest_digest = coverage_manifest_digest(manifest).tagged

    common = dict(
        at=request.at,
        instance_id=request.instance_id,
        index_digest=index_digest,
        overlay_digest=overlay_digest,
        manifest_digest=manifest_digest,
        epoch=None if manifest is None else manifest.epoch,
        watcher_health="absent" if manifest is None else manifest.watcher_health,
        access_profile=access,
        scope=overlay.scope if manifest is None else manifest.scope.sources,
        health=health,
        coverage=CoverageDescriptorV1(
            requested_facets=(COVERAGE_FACET,),
            available_facets=(COVERAGE_FACET,) if any(item.cards for item in spans) else (),
            omitted_for_access=(COVERAGE_FACET,) if withheld else (),
            truncated_facets=(COVERAGE_FACET,) if truncated else (),
            reason_codes=byte_sorted(tuple(reasons)),
        ),
    )
    summary = dict(
        exact=counts["exact"],
        drifted=counts["drifted"],
        candidate=counts["candidate"],
        none=counts["none"],
        returned_spans=len(spans),
        omitted_card_count=sum(item.omitted_card_count for item in spans),
    )
    if is_v2:
        return CoverageResultV2.model_validate(
            {
                **common,
                "spans": cast(tuple[CoverageSpanResultV2, ...], spans),
                "summary": CoverageBatchSummaryV2.model_validate(summary),
            }
        )
    return CoverageResultV1.model_validate(
        {
            **common,
            "spans": spans,
            "summary": CoverageBatchSummaryV1.model_validate(summary),
        }
    )


def resolve_coverage(
    request: CoverageRequestV1,
    *,
    index: EvidenceCitationIndexV1,
    overlay: WorkingOccurrenceOverlayV1,
    access: CoverageAccessProfileV1,
    manifest: CoverageManifestBodyV1 | None = None,
) -> CoverageResultV1:
    """Resolve every requested span against accepted state and the working snapshot.

    Pure: the same request, index, overlay, access profile, and manifest always
    produce byte-identical results, and producing them changes nothing anywhere.
    """

    return _resolve_coverage_any(
        request,
        index=index,
        overlay=overlay,
        access=access,
        manifest=manifest,
    )


def resolve_coverage_v2(
    request: CoverageRequestV1,
    *,
    index: EvidenceCitationIndexV2,
    overlay: WorkingOccurrenceOverlayV1,
    access: CoverageAccessProfileV1,
    manifest: CoverageManifestBodyV2 | None = None,
) -> CoverageResultV2:
    """Resolve association-native coverage through the same semantic pipeline."""

    return cast(
        CoverageResultV2,
        _resolve_coverage_any(
            request,
            index=index,
            overlay=overlay,
            access=access,
            manifest=manifest,
        ),
    )


__all__ = ["COVERAGE_FACET", "resolve_coverage", "resolve_coverage_v2"]
