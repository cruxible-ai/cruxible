"""The §11.6.4 rendering laws, as tests.

Explicit absence without context spam is a rendering property, not a resolver
property: the resolver already returns a `none` per span, and the whole question
is what the reference surface does with forty-one of them. These pin the answer
-- one summary, no per-line `none`, clipped candidates reported, and a health
that never reads as an absence.
"""

from __future__ import annotations

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claims import (
    build_claim_citation,
    claim_path,
    claim_statement_address,
)
from cruxible_client.contracts.source_references import CoverageDescriptorV1
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageBatchSummaryV1,
    CoverageCardBudgetV1,
    CoverageCardV2,
    CoverageClaimCitationV2,
    CoverageRequestV1,
    CoverageResultV1,
    CoverageSpanRequestV1,
    CoverageSpanResultV1,
    LogicalSourceIdentityV1,
    occurrence_identity_digest,
)
from cruxible_core.playbill.coverage.render import (
    BATCH_SUMMARY_PREFIX,
    render_batch_summary,
    render_card,
    render_coverage_manifest,
    render_coverage_result,
)
from cruxible_core.playbill.coverage.resolver import resolve_coverage
from tests.test_playbill._coverage_support import (
    CITED,
    EPILOGUE,
    HANDBOOK,
    INSTANCE_ID,
    PREAMBLE,
    SCRATCH,
    capture,
    coordinate,
    index,
    manifest,
    overlay,
    profile,
    request,
    sha256,
    working,
)

HANDBOOK_BODY = PREAMBLE + CITED + EPILOGUE
EDITED_BODY = PREAMBLE + b"The reviewer rejected the migration plan.\n" + EPILOGUE


def _drifted_result() -> CoverageResultV1:
    """One governed source whose cited content was edited, plus one that was not."""

    citations = index(capture(HANDBOOK, CITED, with_handle=True))
    snapshot = overlay(
        working(HANDBOOK, EDITED_BODY),
        working(SCRATCH, b"Nothing governed here.\n"),
        citations=citations,
    )
    return resolve_coverage(
        request(HANDBOOK, SCRATCH),
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=manifest(citations, snapshot),
    )


def _ungoverned_result(count: int) -> CoverageResultV1:
    """One exact span beside `count` factual absences inside a complete boundary."""

    citations = index(capture(HANDBOOK, CITED))
    sources = [
        LogicalSourceIdentityV1(plane="external", identity=f"workspace.note{number:03d}")
        for number in range(count)
    ]
    snapshot = overlay(
        working(HANDBOOK, HANDBOOK_BODY),
        *(working(item, f"ordinary note {item.identity}\n".encode()) for item in sources),
        citations=citations,
    )
    return resolve_coverage(
        CoverageRequestV1(
            instance_id=INSTANCE_ID,
            at=coordinate(),
            spans=(
                CoverageSpanRequestV1(source=HANDBOOK),
                *(CoverageSpanRequestV1(source=item) for item in sources),
            ),
        ),
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=manifest(citations, snapshot),
    )


def _degraded_span(health: str, reason: str) -> CoverageResultV1:
    """A result whose only span carries an unhealthy boundary and no cards."""

    span = CoverageSpanResultV1(
        request=CoverageSpanRequestV1(source=SCRATCH),
        match_state="none",
        health=health,  # type: ignore[arg-type]
        absence_is_factual=False,
        coverage=CoverageDescriptorV1(
            requested_facets=("coverage",),
            reason_codes=(reason,),
        ),
    )
    return CoverageResultV1(
        at=coordinate(),
        instance_id=INSTANCE_ID,
        index_digest=sha256(b"index"),
        overlay_digest=sha256(b"overlay"),
        manifest_digest=None,
        watcher_health="absent",
        access_profile=CoverageAccessProfileV1(profile_id="coverage.test"),
        spans=(span,),
        summary=CoverageBatchSummaryV1(exact=0, drifted=0, candidate=0, none=1, returned_spans=1),
        health=health,  # type: ignore[arg-type]
        coverage=CoverageDescriptorV1(requested_facets=("coverage",), reason_codes=(reason,)),
    )


# -- the batch summary -----------------------------------------------------


def test_the_batch_summary_keeps_the_shape_the_spec_shows() -> None:
    result = _ungoverned_result(41)

    summary = render_batch_summary(result)

    assert summary[0] == "Playbill coverage: 1 exact, 0 drifted, 0 candidates, 41 none"
    assert summary[1] == (
        f"coverage complete for 42 returned spans at generation {result.at.generation_root}"
    )
    # Omitted and truncated counts are stated on every operation, including when
    # they are zero: a reader must never have to infer from silence that nothing
    # was clipped.
    assert summary[2] == "omitted cards: 0, truncated spans: 0"
    assert len(summary) == 3


def test_exactly_one_summary_is_emitted_per_operation() -> None:
    lines = render_coverage_result(_ungoverned_result(41))

    assert sum(1 for line in lines if line.startswith(BATCH_SUMMARY_PREFIX)) == 1


# -- explicit absence without context spam ---------------------------------


def test_ungoverned_spans_are_counted_once_and_never_annotated_per_line() -> None:
    result = _ungoverned_result(41)

    lines = render_coverage_result(result)

    # One annotated card for the governed minority, then the one summary. The
    # forty-one ungoverned spans produce no output at all.
    assert len(lines) == 4
    assert lines[0].startswith("exact  ")
    rendered = "\n".join(lines)
    for span in result.spans[1:]:
        assert span.match_state == "none"
        assert span.request.source.identity not in rendered


def test_a_wholly_ungoverned_operation_renders_only_its_summary() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(working(SCRATCH, b"Nothing governed here.\n"), citations=citations)
    result = resolve_coverage(
        request(SCRATCH),
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=manifest(citations, snapshot),
    )

    assert render_coverage_result(result) == render_batch_summary(result)


# -- drift renders its whole binding ---------------------------------------


def test_a_drifted_card_names_the_accepted_claim_coordinate_and_both_commitments() -> None:
    result = _drifted_result()

    lines = render_coverage_result(result)
    drift = next(line for line in lines if line.startswith("drifted  "))

    assert f"{HANDBOOK.plane}:{HANDBOOK.identity}" in drift
    assert f"expected {sha256(CITED)}" in drift
    assert f"observed {sha256(EDITED_BODY)}" in drift
    assert f"at generation {result.at.generation_root}" in drift
    assert "captures " in drift
    assert "commitment_superseded" in drift
    # The one summary counts the drift; the ungoverned scratch file does not
    # earn a line of its own.
    assert "Playbill coverage: 0 exact, 1 drifted, 0 candidates, 1 none" in lines
    assert SCRATCH.identity not in "\n".join(lines)


def _associated_v2_card(*, role: str, origin: str) -> CoverageCardV2:
    captured = capture(HANDBOOK, CITED, name=f"{role}-{origin}")
    identity = ArtifactIdentity(kind="Claim", name="CLM-" + "7a" * 16)
    citation = build_claim_citation(
        identity,
        capture_digest=captured.capture_digest,
        role=role,  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
    )
    address = claim_statement_address(claim_path(identity.name))
    return CoverageCardV2(
        match_state="exact",
        at=coordinate(),
        claim_addresses=(address,),
        capture_digests=(captured.capture_digest,),
        expected_commitment_digest=sha256(CITED),
        observed_commitment_digest=sha256(CITED),
        accepted_source=HANDBOOK,
        observed_source=HANDBOOK,
        occurrence_identity_digest=occurrence_identity_digest(
            source=HANDBOOK,
            observed_commitment_digest=sha256(CITED),
            ordinal=0,
        ),
        citation_associations=(
            CoverageClaimCitationV2(
                claim_address=address,
                capture_digest=captured.capture_digest,
                reference=citation,
                observation_trust="proposer_observed",
            ),
        ),
    )


def test_only_explicit_self_published_copy_association_gets_the_publication_variant() -> None:
    published = _associated_v2_card(role="copy", origin="self_published")
    self_source = _associated_v2_card(role="copy", origin="self_source")
    published_evidence = _associated_v2_card(role="evidence", origin="self_published")

    rendered = render_card(published)

    assert published.is_self_published_copy is True
    assert "published copy" in rendered
    assert "roles copy" in rendered
    assert "origins self_published" in rendered
    assert "not independent evidence" in rendered
    assert self_source.is_self_published_copy is False
    assert "published copy" not in render_card(self_source)
    assert published_evidence.is_self_published_copy is False
    assert "published copy" not in render_card(published_evidence)


def test_v2_drift_variant_keeps_association_and_names_both_commitments() -> None:
    exact = _associated_v2_card(role="copy", origin="self_published")
    changed = sha256(b"changed publication bytes")
    drifted = CoverageCardV2.model_validate(
        {
            **exact.model_dump(mode="json"),
            "match_state": "drifted",
            "observed_commitment_digest": changed,
            "occurrence_identity_digest": None,
            "reason_codes": ("commitment_superseded",),
        }
    )

    rendered = render_card(drifted)

    assert rendered.startswith("drifted  ")
    assert f"expected {sha256(CITED)}" in rendered
    assert f"observed {changed}" in rendered
    assert "published copy" in rendered


# -- clipped candidates are always reported --------------------------------


def test_clipped_candidate_cards_are_reported_rather_than_silently_dropped() -> None:
    citations = index(
        capture(HANDBOOK, CITED, name="first"),
        capture(None, CITED, name="second"),
    )
    snapshot = overlay(working(SCRATCH, CITED), citations=citations)
    result = resolve_coverage(
        CoverageRequestV1(
            instance_id=INSTANCE_ID,
            at=coordinate(),
            spans=(CoverageSpanRequestV1(source=SCRATCH),),
            budget=CoverageCardBudgetV1(max_cards_per_span=1, max_candidate_cards_per_span=1),
        ),
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=manifest(citations, snapshot),
    )

    assert result.summary.omitted_card_count == 1
    lines = render_coverage_result(result)
    assert any("1 card(s) clipped by budget" in line for line in lines)
    assert any(line.startswith("partial  ") for line in lines)
    assert "omitted cards: 1, truncated spans: 1" in lines


def test_indistinguishable_occurrences_are_rendered_as_an_explicit_ambiguity() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(working(HANDBOOK, PREAMBLE + CITED + CITED + EPILOGUE), citations=citations)
    result = resolve_coverage(
        request(HANDBOOK),
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=manifest(citations, snapshot),
    )

    lines = render_coverage_result(result)

    assert result.spans[0].match_state != "exact"
    assert any("indistinguishable occurrence(s), none bound" in line for line in lines)


# -- a health never reads as an absence ------------------------------------


def test_denied_and_unavailable_render_as_their_health_and_never_as_a_none() -> None:
    for health, reason in (
        ("denied", "restricted_access_class"),
        ("unavailable", "source_not_observed"),
        ("stale", "manifest_snapshot_superseded"),
    ):
        lines = render_coverage_result(_degraded_span(health, reason))

        assert lines[0] == f"{health}  {SCRATCH.plane}:{SCRATCH.identity}  [{reason}]"
        # The span is `none`, but its absence is not factual, so it is never
        # allowed to render as silence the way a complete-boundary `none` does.
        assert not any(line.startswith("none") for line in lines)


def test_restricted_coverage_renders_denied_rather_than_a_factual_absence() -> None:
    citations = index(capture(HANDBOOK, CITED, access_class="restricted"))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    result = resolve_coverage(
        request(HANDBOOK),
        index=citations,
        overlay=snapshot,
        access=profile(permitted=("instance", "public")),
        manifest=manifest(citations, snapshot),
    )

    lines = render_coverage_result(result)

    assert result.spans[0].health == "denied"
    assert result.spans[0].absence_is_factual is False
    assert any(line.startswith("denied  ") for line in lines)


# -- the manifest rendering ------------------------------------------------


def test_the_manifest_rendering_states_epoch_health_completeness_and_scope() -> None:
    result = _drifted_result()

    lines = render_coverage_manifest(result)

    assert lines[0] == "Playbill coverage manifest: epoch 0, health complete, boundary complete"
    assert lines[1] == f"instance {INSTANCE_ID} at generation {result.at.generation_root}"
    assert f"index {result.index_digest}" in lines
    assert f"overlay {result.overlay_digest}" in lines
    assert "watcher absent, access profile coverage.test" in lines
    assert "scope 2 source(s):" in lines
    assert f"  {HANDBOOK.plane}:{HANDBOOK.identity}" in lines
    assert f"  {SCRATCH.plane}:{SCRATCH.identity}" in lines
