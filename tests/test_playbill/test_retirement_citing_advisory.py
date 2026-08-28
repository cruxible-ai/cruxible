"""Retiring a Claim names the live Claims left citing its Captures."""

from __future__ import annotations

from typing import cast

from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifact,
    ClaimArtifactV2,
    ClaimBackingV2,
    build_claim_citation,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    render_claim,
)
from cruxible_core.playbill.claim_retirement import _citing_claims
from cruxible_core.playbill.coverage.indexes import live_claim_paths_by_capture
from tests.test_playbill.test_claims import _claim

CAPTURE_A = "sha256:" + "ab" * 32
CAPTURE_B = "sha256:" + "ef" * 32
SOURCE_DIGEST = "sha256:" + "cd" * 32


def _cited_claim(*, claim_id: str, capture: str, state: str = "live") -> ClaimArtifactV2:
    legacy: ClaimArtifact = _claim(
        claim_id=claim_id,
        capture_digest=capture,
        source_digest=SOURCE_DIGEST,
        source_length=12,
    )
    citation = build_claim_citation(
        legacy.identity,
        capture_digest=capture,
        role="evidence",
        origin="independent",
    )
    claim = ClaimArtifactV2(
        identity=legacy.identity,
        statement=legacy.statement,
        backing=ClaimBackingV2(
            referent_context=legacy.backing.referent_context,
            capture_digests=(capture,),
            citations=(citation,),
            source_mappings=legacy.backing.source_mappings,
        ),
        authority=legacy.authority,
        pins=legacy.pins,
    )
    if state == "live":
        return claim
    return ClaimArtifactV2(
        **{**claim.model_dump(), "lifecycle": {**claim.lifecycle.model_dump(), "state": state}}
    )


def _accepted(claim: ClaimArtifactV2) -> AcceptedClaim:
    path = claim_path(claim.identity.name)
    return AcceptedClaim(
        path=path,
        claim=claim,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
    )


def _tree(*claims: ClaimArtifactV2) -> dict[str, bytes]:
    return {claim_path(claim.identity.name): render_claim(claim) for claim in claims}


def test_the_reverse_index_maps_a_capture_to_its_live_citing_claims() -> None:
    first = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_A)
    second = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_A)
    elsewhere = _cited_claim(claim_id="CLM-" + "3" * 32, capture=CAPTURE_B)

    index = live_claim_paths_by_capture([_accepted(first), _accepted(second), _accepted(elsewhere)])

    assert index[CAPTURE_A] == tuple(
        sorted(
            (claim_path(first.identity.name), claim_path(second.identity.name)),
            key=lambda item: item.encode("utf-8"),
        )
    )
    assert index[CAPTURE_B] == (claim_path(elsewhere.identity.name),)


def test_a_retired_claim_never_counts_as_a_live_citer() -> None:
    live = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_A)
    retired = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_A, state="retired")

    index = live_claim_paths_by_capture([_accepted(live), _accepted(retired)])

    assert index[CAPTURE_A] == (claim_path(live.identity.name),)


def test_retirement_names_the_live_claim_left_citing_the_same_capture() -> None:
    """The W1-P7 regression: this citing Claim used to be stranded silently."""
    retiring = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_A)
    citing = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_A)
    root_path = claim_path(retiring.identity.name)

    advisory = _citing_claims(_tree(retiring, citing), root_path=root_path, root=retiring)

    assert [item.claim_path for item in advisory] == [claim_path(citing.identity.name)]
    assert advisory[0].artifact_identity == citing.identity
    assert advisory[0].capture_digests == (CAPTURE_A,)


def test_a_claim_citing_a_different_capture_is_not_advised() -> None:
    retiring = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_A)
    unrelated = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_B)
    root_path = claim_path(retiring.identity.name)

    assert _citing_claims(_tree(retiring, unrelated), root_path=root_path, root=retiring) == ()


def test_the_retiring_claim_never_advises_about_itself() -> None:
    retiring = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_A)
    root_path = claim_path(retiring.identity.name)

    assert _citing_claims(_tree(retiring), root_path=root_path, root=retiring) == ()


def test_the_queue_row_names_the_citing_claim_and_the_retired_claim_it_cites() -> None:
    """The other half of W1-P7: after the retirement, the strand becomes actionable."""
    from cruxible_core.service.playbill_next import _stranded_citation_items

    citing = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_A)
    retired_identity = "Claim:CLM-" + "1" * 32

    (row,) = _stranded_citation_items(
        {CAPTURE_A: {retired_identity}},
        live=[_accepted(citing)],
    )

    assert row.reason == "claim_cites_retired"
    assert row.severity == "warning"
    assert row.subject_identity == citing.identity.qualified
    assert row.related_identities == (retired_identity,)
    detail = cast(dict[str, object], row.detail)
    assert detail["citing_claim"] == citing.identity.qualified
    assert detail["retired_claims"] == [retired_identity]
    assert detail["capture_digests"] == [CAPTURE_A]
    assert row.repair.target == citing.identity.qualified


def test_a_live_claim_citing_no_retired_capture_produces_no_row() -> None:
    from cruxible_core.service.playbill_next import _stranded_citation_items

    citing = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_A)

    assert _stranded_citation_items({CAPTURE_B: {"Claim:gone"}}, live=[_accepted(citing)]) == ()
