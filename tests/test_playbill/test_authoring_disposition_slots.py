"""The disposition law demands only the statement's own contended slot."""

from __future__ import annotations

from cruxible_client.contracts.claims import ClaimArtifact, ClaimStatement, render_claim
from cruxible_client.contracts.claims import claim_path as claim_artifact_path
from cruxible_core.playbill.authoring.lowering import (
    _same_predicate_claims,
    _same_slot_claims,
)
from tests.test_playbill.test_claims import _claim

CAPTURE_DIGEST = "sha256:" + "ab" * 32
SOURCE_DIGEST = "sha256:" + "cd" * 32


def _claim_in_slot(*, claim_id: str, qualifier: str | None) -> ClaimArtifact:
    base = _claim(
        claim_id=claim_id,
        capture_digest=CAPTURE_DIGEST,
        source_digest=SOURCE_DIGEST,
        source_length=12,
    )
    statement = ClaimStatement(
        **{**base.statement.model_dump(), "qualifier": qualifier},
    )
    return ClaimArtifact(**{**base.model_dump(), "statement": statement.model_dump()})


def _tree(*claims: ClaimArtifact) -> dict[str, bytes]:
    return {claim_artifact_path(claim.identity.name): render_claim(claim) for claim in claims}


def test_the_demanded_set_is_only_the_statements_own_qualifier_slot() -> None:
    primary = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier="primary")
    secondary = _claim_in_slot(claim_id="CLM-" + "2" * 32, qualifier="secondary")
    unqualified = _claim_in_slot(claim_id="CLM-" + "3" * 32, qualifier=None)
    tree = _tree(primary, secondary, unqualified)

    demanded = _same_slot_claims(tree, primary.statement)

    assert [claim.identity.name for claim in demanded] == [primary.identity.name]


def test_an_unqualified_statement_does_not_contend_with_qualified_siblings() -> None:
    primary = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier="primary")
    unqualified = _claim_in_slot(claim_id="CLM-" + "3" * 32, qualifier=None)
    tree = _tree(primary, unqualified)

    demanded = _same_slot_claims(tree, unqualified.statement)

    assert [claim.identity.name for claim in demanded] == [unqualified.identity.name]


def test_a_many_cardinality_sibling_in_the_same_slot_is_still_demanded() -> None:
    """Same slot is the test, not same value: cardinality is the type's business."""
    first = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier=None)
    second = _claim_in_slot(claim_id="CLM-" + "2" * 32, qualifier=None)
    tree = _tree(first, second)

    demanded = _same_slot_claims(tree, first.statement)

    assert {claim.identity.name for claim in demanded} == {
        first.identity.name,
        second.identity.name,
    }


def test_every_qualifier_stays_voluntarily_dispositionable() -> None:
    primary = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier="primary")
    secondary = _claim_in_slot(claim_id="CLM-" + "2" * 32, qualifier="secondary")
    tree = _tree(primary, secondary)

    offerable = _same_predicate_claims(tree, primary.statement)

    assert {claim.identity.name for claim in offerable} == {
        primary.identity.name,
        secondary.identity.name,
    }


def test_a_retired_claim_in_the_slot_is_neither_demanded_nor_offerable() -> None:
    live = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier=None)
    retired_source = _claim_in_slot(claim_id="CLM-" + "2" * 32, qualifier=None)
    retired = ClaimArtifact(
        **{
            **retired_source.model_dump(),
            "lifecycle": {**retired_source.lifecycle.model_dump(), "state": "retired"},
        }
    )
    tree = _tree(live, retired)

    assert [claim.identity.name for claim in _same_slot_claims(tree, live.statement)] == [
        live.identity.name
    ]
    assert [claim.identity.name for claim in _same_predicate_claims(tree, live.statement)] == [
        live.identity.name
    ]
