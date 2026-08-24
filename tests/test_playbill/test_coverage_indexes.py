"""PC-F2 disposable coverage indexes.

Three laws are under test here and nowhere else:

* the evidence index is the reverse of accepted citation -- commitment digest to
  citing Captures and Claims -- and it invents nothing the ledger did not say;
* a source-occurrence identity is stable under relocation, because byte offsets
  and line numbers are a presentation overlay outside its preimage;
* both indexes are disposable: deleting them and rebuilding from the same
  accepted state and the same snapshot reproduces the same digests, and
  truncating a scan costs recall while never costing honesty.
"""

from __future__ import annotations

import pytest

from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifactV2,
    ClaimBackingV2,
    build_claim_citation,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageError,
    occurrence_identity_digest,
)
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV2,
    CoverageScanBudgetV1,
    accepted_logical_source,
    build_evidence_citation_index,
    build_evidence_citation_index_v2,
    build_working_occurrence_overlay,
    evidence_citation_index_digest,
    working_occurrence_overlay_digest,
)
from tests.test_playbill._coverage_support import (
    CATALOG,
    CITED,
    EPILOGUE,
    HANDBOOK,
    PREAMBLE,
    SCRATCH,
    capture,
    coordinate,
    index,
    overlay,
    sha256,
    source_reference,
    working,
)
from tests.test_playbill.test_claims import _claim

# -- the reverse evidence index -------------------------------------------


def test_the_evidence_index_reverses_capture_citation_into_commitment_lookup() -> None:
    citations = index(capture(HANDBOOK, CITED, with_handle=True))

    assert len(citations.citations) == 1
    row = citations.citations[0]
    assert row.commitment_digest == sha256(CITED)
    assert row.byte_length == len(CITED)
    assert row.accepted_source == HANDBOOK
    assert row.capture_digests == citations.citations[0].capture_digests
    assert row.dereference_handle_digest is not None
    assert citations.by_commitment(sha256(CITED)) == (row,)
    assert citations.by_logical_source(HANDBOOK) == (row,)
    assert citations.by_logical_source(SCRATCH) == ()
    # The scan the overlay runs is seeded from exactly the byte-addressable
    # commitments, never from every digest the ledger happens to hold.
    assert citations.wanted_selections() == ((sha256(CITED), len(CITED)),)


def test_a_content_addressed_capture_names_no_logical_source() -> None:
    citations = index(capture(None, CITED, name="cas-note"))

    assert citations.citations[0].accepted_source is None
    assert accepted_logical_source(source_reference(HANDBOOK)) == HANDBOOK
    assert accepted_logical_source(source_reference(SCRATCH)) == SCRATCH
    # A CAS reference is addressed by its content, so there is no source an edit
    # could relocate that content within, and the resolver must treat a byte
    # match against it as foreign rather than invent a source.
    assert citations.by_logical_source(HANDBOOK) == ()


def test_several_captures_of_one_commitment_collapse_to_the_strictest_access() -> None:
    citations = index(
        capture(HANDBOOK, CITED, name="open", access_class="public"),
        capture(HANDBOOK, CITED, name="sealed", access_class="restricted"),
    )

    assert len(citations.citations) == 1
    row = citations.citations[0]
    assert row.access_class == "restricted"
    assert len(row.capture_digests) == 2


def test_v2_index_keeps_two_claim_roles_on_one_capture_as_distinct_associations() -> None:
    legacy_capture = capture(HANDBOOK, CITED)
    v2_capture = CaptureCitationInputV2.model_validate(
        {
            **legacy_capture.model_dump(mode="json"),
            "tag": "playbill-coverage-capture-citation-input-v2",
            "observation_trust": "provider_receipted",
        }
    )

    accepted: list[AcceptedClaim] = []
    for claim_id, role in (
        ("CLM-" + "11" * 16, "evidence"),
        ("CLM-" + "22" * 16, "copy"),
    ):
        legacy = _claim(
            claim_id=claim_id,
            capture_digest=legacy_capture.capture_digest,
            source_digest=sha256(CITED),
            source_length=len(CITED),
        )
        artifact = ClaimArtifactV2(
            identity=legacy.identity,
            statement=legacy.statement,
            backing=ClaimBackingV2(
                referent_context=legacy.backing.referent_context,
                capture_digests=legacy.backing.capture_digests,
                citations=(
                    build_claim_citation(
                        legacy.identity,
                        capture_digest=legacy_capture.capture_digest,
                        role=role,  # type: ignore[arg-type]
                        origin="independent",
                    ),
                ),
                source_mappings=legacy.backing.source_mappings,
            ),
            authority=legacy.authority,
            pins=legacy.pins,
        )
        accepted.append(
            AcceptedClaim(
                path=claim_path(claim_id),
                claim=artifact,
                statement_digest=claim_statement_digest(artifact.statement).tagged,
                artifact_digest=claim_artifact_digest(artifact).tagged,
            )
        )

    citations = build_evidence_citation_index_v2(
        at=coordinate(),
        captures=(v2_capture,),
        claims=accepted,
    )
    associations = citations.citations[0].citation_associations

    assert {item.reference.role for item in associations} == {"copy", "evidence"}
    assert {item.claim_address.artifact_path for item in associations} == {
        claim_path("CLM-" + "11" * 16),
        claim_path("CLM-" + "22" * 16),
    }
    assert {item.observation_trust for item in associations} == {"provider_receipted"}


# -- the working-source occurrence overlay ---------------------------------


def test_an_occurrence_identity_survives_relocation_within_its_source() -> None:
    citations = index(capture(HANDBOOK, CITED))
    before = overlay(working(HANDBOOK, PREAMBLE + CITED + EPILOGUE), citations=citations)
    after = overlay(
        working(HANDBOOK, PREAMBLE + EPILOGUE + b"\n\n" + CITED),
        citations=citations,
    )

    cited_before = next(
        item for item in before.occurrences if item.observed_commitment_digest == sha256(CITED)
    )
    cited_after = next(
        item for item in after.occurrences if item.observed_commitment_digest == sha256(CITED)
    )

    assert cited_before.identity_digest == cited_after.identity_digest
    assert cited_before.identity_digest == occurrence_identity_digest(
        source=HANDBOOK,
        observed_commitment_digest=sha256(CITED),
        ordinal=0,
    )
    # Only the presentation overlay moved.
    assert cited_before.line_overlay != cited_after.line_overlay
    assert cited_before.line_overlay.start_line != cited_after.line_overlay.start_line


def test_duplicate_occurrences_receive_distinct_stable_ordinals() -> None:
    citations = index(capture(HANDBOOK, CITED))
    snapshot = overlay(
        working(HANDBOOK, PREAMBLE + CITED + EPILOGUE + CITED),
        citations=citations,
    )

    cited = tuple(
        item for item in snapshot.occurrences if item.observed_commitment_digest == sha256(CITED)
    )
    assert tuple(item.ordinal for item in cited) == (0, 1)
    assert len({item.identity_digest for item in cited}) == 2
    assert cited[0].line_overlay.start_byte < cited[1].line_overlay.start_byte


def test_a_working_snapshot_names_each_logical_source_at_most_once() -> None:
    citations = index(capture(HANDBOOK, CITED))

    with pytest.raises(CoverageError, match="at most once"):
        build_working_occurrence_overlay(
            (working(HANDBOOK, CITED), working(HANDBOOK, PREAMBLE)),
            wanted=citations.wanted_selections(),
        )


def test_a_scan_budget_bounds_recall_and_states_the_truncation() -> None:
    citations = index(capture(HANDBOOK, CITED))
    content = PREAMBLE + CITED + EPILOGUE
    starved = build_working_occurrence_overlay(
        (working(HANDBOOK, content),),
        wanted=citations.wanted_selections(),
        budget=CoverageScanBudgetV1(max_scanned_bytes=0),
    )

    assert starved.truncated is True
    assert starved.truncation_reason_codes == ("scan_budget_exceeded",)
    # The whole-source occurrence is free, so the source is still observed --
    # what shrank is the ability to find a cited selection inside it.
    assert starved.commitment_for(HANDBOOK) is not None
    assert starved.scanned(sha256(CITED)) is False
    assert all(item.observed_commitment_digest != sha256(CITED) for item in starved.occurrences)


# -- disposability ---------------------------------------------------------


def test_deleting_and_rebuilding_both_indexes_reproduces_their_digests() -> None:
    def build() -> tuple[str, str]:
        citations = build_evidence_citation_index(
            at=coordinate(),
            captures=(
                capture(HANDBOOK, CITED, with_handle=True),
                capture(SCRATCH, PREAMBLE, name="scratch"),
            ),
        )
        snapshot = build_working_occurrence_overlay(
            (
                working(CATALOG, EPILOGUE),
                working(HANDBOOK, PREAMBLE + CITED + EPILOGUE),
                working(SCRATCH, PREAMBLE),
            ),
            wanted=citations.wanted_selections(),
        )
        return (
            evidence_citation_index_digest(citations),
            working_occurrence_overlay_digest(snapshot),
        )

    assert build() == build()


def test_index_input_order_does_not_change_the_index() -> None:
    handbook = capture(HANDBOOK, CITED, with_handle=True)
    scratch = capture(SCRATCH, PREAMBLE, name="scratch")

    forward = build_evidence_citation_index(at=coordinate(), captures=(handbook, scratch))
    reverse = build_evidence_citation_index(at=coordinate(), captures=(scratch, handbook))

    assert evidence_citation_index_digest(forward) == evidence_citation_index_digest(reverse)
