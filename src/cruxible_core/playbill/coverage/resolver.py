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
from typing import Literal

from cruxible_client.contracts.discovery import DiscoveryMatchBasis
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.source_references import CoverageDescriptorV1
from cruxible_core.playbill.coverage.contracts import (
    COVERAGE_HEALTH_PROVES_FRESHNESS,
    COVERAGE_MATCH_STATES,
    CoverageAccessProfileV1,
    CoverageBatchSummaryV3,
    CoverageCardV2,
    CoverageHealthV1,
    CoverageMatchStateV1,
    CoverageRequestV1,
    CoverageResultV3,
    CoverageSpanRequestV1,
    CoverageSpanResultV3,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
    strongest_match_state,
    weakest_health,
)
from cruxible_core.playbill.coverage.indexes import (
    EvidenceCitationIndexV2,
    EvidenceCitationV2,
    WorkingOccurrenceOverlayV2,
    WorkingOccurrenceV1,
    evidence_citation_index_digest,
    working_occurrence_overlay_digest,
)
from cruxible_core.playbill.coverage.manifest import (
    CoverageManifestBodyV2,
    coverage_manifest_digest_v2,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

COVERAGE_FACET = "coverage"


def _source_floor(
    span: CoverageSpanRequestV1,
    *,
    manifest: CoverageManifestBodyV2 | None,
    overlay: WorkingOccurrenceOverlayV2,
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
    citation: EvidenceCitationV2,
    source: LogicalSourceIdentityV1,
) -> bool:
    return (
        citation.accepted_source is not None
        and citation.accepted_source.sort_key == source.sort_key
    )


def _budgeted(
    cards: Sequence[CoverageCardV2],
    request: CoverageRequestV1,
) -> tuple[tuple[CoverageCardV2, ...], int]:
    """Clip from the low-priority tail and report exactly what was dropped."""

    ordered = sorted(cards, key=lambda item: item.sort_key)
    kept: list[CoverageCardV2] = []
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


def _resolve_span_v3(
    span: CoverageSpanRequestV1,
    *,
    request: CoverageRequestV1,
    index: EvidenceCitationIndexV2,
    overlay: WorkingOccurrenceOverlayV2,
    access: CoverageAccessProfileV1,
    manifest_floor: CoverageHealthV1,
    manifest_reasons: tuple[str, ...],
    manifest: CoverageManifestBodyV2 | None,
    window_observations: tuple[PlaybillCitationWindowObservationV1, ...],
) -> CoverageSpanResultV3:
    """Resolve one source from its own proof boundary, never a global scan bit."""

    source_floor, source_reasons = _source_floor(span, manifest=manifest, overlay=overlay)
    health = weakest_health(manifest_floor, source_floor)
    reasons = set(manifest_reasons) | set(source_reasons)
    if index.truncated:
        health = weakest_health(health, "partial")
        reasons.add("evidence_index_truncated")

    # A source-local absence is factual only when this source was searched for
    # every visible accepted selection.  The card fold below is citation-local,
    # so it cannot by itself notice a wanted selection omitted by the scanner's
    # global budget.
    visible_wanted = {
        (citation.commitment_digest, citation.byte_length or 0)
        for citation in index.citations
        if isinstance(citation, EvidenceCitationV2)
        and citation.digest_kind == "exact_bytes"
        and access.permits(citation.access_class)
    }
    if any(
        not overlay.scanned(span.source, commitment_digest, byte_length)
        for commitment_digest, byte_length in visible_wanted
    ):
        health = weakest_health(health, "partial")
        reasons.add("unscanned_selection")

    occurrences = overlay.occurrences_for(span.source)
    duplicates: dict[str, int] = {}
    for item in occurrences:
        duplicates[item.observed_commitment_digest] = (
            duplicates.get(item.observed_commitment_digest, 0) + 1
        )

    cards: list[CoverageCardV2] = []
    ambiguous = 0
    withheld = False
    visible_local: list[EvidenceCitationV2] = []
    for citation in index.by_logical_source(span.source):
        if not isinstance(citation, EvidenceCitationV2) or citation.digest_kind != "exact_bytes":
            continue
        if access.permits(citation.access_class):
            visible_local.append(citation)
        else:
            withheld = True

    for occurrence in occurrences:
        if not _selected(occurrence, span):
            continue
        for citation in index.by_commitment(occurrence.observed_commitment_digest):
            if (
                not isinstance(citation, EvidenceCitationV2)
                or citation.digest_kind != "exact_bytes"
            ):
                continue
            if not access.permits(citation.access_class):
                withheld = True
                continue
            byte_length = citation.byte_length or 0
            locally_scanned = overlay.scanned(span.source, citation.commitment_digest, byte_length)
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
            if not locally_scanned:
                health = weakest_health(health, "partial")
                reasons.add("unscanned_selection")
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
        for citation in visible_local:
            if citation.commitment_digest in observed_digests:
                continue
            byte_length = citation.byte_length or 0
            if not overlay.scanned(span.source, citation.commitment_digest, byte_length):
                health = weakest_health(health, "partial")
                reasons.add("unscanned_selection")
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
            health = weakest_health(health, "partial")
            reasons.add("boundary_incomplete")

    kept, omitted = _budgeted(cards, request)
    if omitted:
        health = weakest_health(health, "partial")
        reasons.add("card_budget_exceeded")
    if ambiguous and not any(card.match_state == "candidate" for card in kept):
        ambiguous = 0

    allowed_ids = {
        association.reference.citation_id
        for citation in visible_local
        for association in citation.citation_associations
    }
    overlay_occurrences: dict[tuple[str, int], set[str]] = {}
    for occurrence in occurrences:
        overlay_occurrences.setdefault(
            (occurrence.observed_commitment_digest, occurrence.byte_length), set()
        ).add(occurrence.identity_digest)
    kept_occurrences: dict[tuple[str, int], set[str]] = {}
    for card in kept:
        if card.match_state not in {"exact", "candidate"}:
            continue
        identity = card.occurrence_identity_digest
        overlay_value = card.line_overlay
        observed_digest = card.observed_commitment_digest
        if identity is None or overlay_value is None or observed_digest is None:
            continue
        kept_occurrences.setdefault(
            (
                observed_digest,
                overlay_value.end_byte - overlay_value.start_byte,
            ),
            set(),
        ).add(identity)
    proofs = tuple(
        proof
        for proof in overlay.source_scan_proofs
        if proof.source == span.source
        and (proof.commitment_digest, proof.byte_length) in visible_wanted
        and overlay_occurrences.get((proof.commitment_digest, proof.byte_length), set())
        == kept_occurrences.get((proof.commitment_digest, proof.byte_length), set())
    )
    windows = tuple(
        item
        for item in window_observations
        if item.source == span.source and item.citation_id in allowed_ids
    )
    match_state: CoverageMatchStateV1 = strongest_match_state(card.match_state for card in kept)
    return CoverageSpanResultV3(
        request=span,
        match_state=match_state,
        health=health,
        absence_is_factual=match_state == "none" and health == "complete",
        cards=kept,
        ambiguous_occurrence_count=ambiguous,
        omitted_card_count=omitted,
        commitment_scan_proofs=proofs,
        citation_window_observations=windows,
        coverage=CoverageDescriptorV1(
            requested_facets=(COVERAGE_FACET,),
            available_facets=(COVERAGE_FACET,) if kept else (),
            omitted_for_access=(
                (COVERAGE_FACET,) if withheld and access.disclose_restricted_existence else ()
            ),
            truncated_facets=(
                (COVERAGE_FACET,) if omitted or "unscanned_selection" in reasons else ()
            ),
            reason_codes=byte_sorted(tuple(reasons)),
        ),
    )


def _card(
    match_state: Literal["exact", "drifted", "candidate"],
    *,
    citation: EvidenceCitationV2,
    occurrence: WorkingOccurrenceV1,
    at: AcceptedCoordinate,
    basis: DiscoveryMatchBasis | None = None,
    reason_codes: tuple[str, ...] = (),
) -> CoverageCardV2:
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
    return CoverageCardV2.model_validate(
        {**payload, "citation_associations": citation.citation_associations}
    )


def _drift_card(
    *,
    citation: EvidenceCitationV2,
    observed_commitment_digest: str,
    source: LogicalSourceIdentityV1,
    at: AcceptedCoordinate,
) -> CoverageCardV2:
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
    return CoverageCardV2.model_validate(
        {**payload, "citation_associations": citation.citation_associations}
    )


def _manifest_floor_v3(
    manifest: CoverageManifestBodyV2 | None,
    *,
    request: CoverageRequestV1,
    index_digest: str,
) -> tuple[CoverageHealthV1, tuple[str, ...]]:
    """Validate the manifest without collapsing global scan health into each source."""

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
    return "complete", ()


def resolve_coverage_v3(
    request: CoverageRequestV1,
    *,
    index: EvidenceCitationIndexV2,
    overlay: WorkingOccurrenceOverlayV2,
    access: CoverageAccessProfileV1,
    manifest: CoverageManifestBodyV2 | None = None,
    window_observations: tuple[PlaybillCitationWindowObservationV1, ...] = (),
) -> CoverageResultV3:
    """Resolve association-native coverage with source-local proof health."""

    index_digest = evidence_citation_index_digest(index)
    overlay_digest = working_occurrence_overlay_digest(overlay)
    if index.at != request.at:
        raise ValueError("coverage resolves against one accepted coordinate at a time")
    manifest_floor, manifest_reasons = _manifest_floor_v3(
        manifest, request=request, index_digest=index_digest
    )
    spans = tuple(
        _resolve_span_v3(
            span,
            request=request,
            index=index,
            overlay=overlay,
            access=access,
            manifest_floor=manifest_floor,
            manifest_reasons=manifest_reasons,
            manifest=manifest,
            window_observations=window_observations,
        )
        for span in request.spans
    )

    global_reasons = set(overlay.truncation_reason_codes)
    if index.truncated:
        global_reasons.add("evidence_index_truncated")
    if manifest is None:
        global_reasons.add("manifest_absent")
    else:
        global_reasons.update(manifest.truncation_reason_codes)
        if manifest.watcher_health in {"degraded", "overflowed"}:
            global_reasons.add("watcher_unhealthy")
        if manifest.instance_id != request.instance_id:
            global_reasons.add("manifest_instance_mismatch")
        if manifest.at != request.at:
            global_reasons.add("manifest_coordinate_mismatch")
        if manifest.index_digest != index_digest:
            global_reasons.add("manifest_index_mismatch")
    global_scan_complete = not global_reasons

    counts = {state: 0 for state in COVERAGE_MATCH_STATES}
    for span in spans:
        counts[span.match_state] += 1
    summary = CoverageBatchSummaryV3(
        exact=counts["exact"],
        drifted=counts["drifted"],
        candidate=counts["candidate"],
        none=counts["none"],
        returned_spans=len(spans),
        omitted_card_count=sum(item.omitted_card_count for item in spans),
    )
    health = weakest_health(
        *(span.health for span in spans),
        *("stale",) if (manifest and manifest.watcher_health in {"degraded", "overflowed"}) else (),
        *("partial",) if not global_scan_complete else (),
    )
    reason_codes = {
        *global_reasons,
        *(code for span in spans for code in span.coverage.reason_codes),
    }
    withheld = any(span.coverage.omitted_for_access for span in spans)
    truncated = not global_scan_complete or any(span.coverage.truncated_facets for span in spans)
    manifest_digest = None if manifest is None else coverage_manifest_digest_v2(manifest).tagged
    return CoverageResultV3(
        at=request.at,
        instance_id=request.instance_id,
        index_digest=index_digest,
        overlay_digest=overlay_digest,
        manifest_digest=manifest_digest,
        epoch=None if manifest is None else manifest.epoch,
        watcher_health="absent" if manifest is None else manifest.watcher_health,
        access_profile=access,
        scope=overlay.scope if manifest is None else manifest.scope.sources,
        spans=spans,
        summary=summary,
        health=health,
        global_scan_complete=global_scan_complete,
        truncation_reason_codes=byte_sorted(tuple(global_reasons)),
        coverage=CoverageDescriptorV1(
            requested_facets=(COVERAGE_FACET,),
            available_facets=(COVERAGE_FACET,) if any(span.cards for span in spans) else (),
            omitted_for_access=(COVERAGE_FACET,) if withheld else (),
            truncated_facets=(COVERAGE_FACET,) if truncated else (),
            reason_codes=byte_sorted(tuple(reason_codes)),
        ),
    )


__all__ = [
    "COVERAGE_FACET",
    "resolve_coverage_v3",
]
