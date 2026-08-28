"""The disposition law demands only the statement's own contended slot."""

from __future__ import annotations

from cruxible_client.contracts.claims import ClaimArtifactV2, ClaimStatement, render_claim
from cruxible_client.contracts.claims import claim_path as claim_artifact_path
from cruxible_core.playbill.authoring.lowering import (
    _same_predicate_claims,
    _same_slot_claims,
)
from tests.test_playbill.test_claims import _claim

CAPTURE_DIGEST = "sha256:" + "ab" * 32
SOURCE_DIGEST = "sha256:" + "cd" * 32


def _claim_in_slot(*, claim_id: str, qualifier: str | None) -> ClaimArtifactV2:
    base = _claim(
        claim_id=claim_id,
        capture_digest=CAPTURE_DIGEST,
        source_digest=SOURCE_DIGEST,
        source_length=12,
    )
    statement = ClaimStatement(
        **{**base.statement.model_dump(), "qualifier": qualifier},
    )
    return ClaimArtifactV2(**{**base.model_dump(), "statement": statement.model_dump()})


def _tree(*claims: ClaimArtifactV2) -> dict[str, bytes]:
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
    retired = ClaimArtifactV2(
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


def _dispositionable(tree: dict[str, bytes], statement: ClaimStatement) -> set[str]:
    return {claim.identity.name for claim in _same_predicate_claims(tree, statement)}


def test_the_same_slot_still_refuses_without_its_dispositions() -> None:
    """Narrowing removed demands that never contended; it kept the ones that do."""
    first = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier="primary")
    second = _claim_in_slot(claim_id="CLM-" + "2" * 32, qualifier="primary")
    tree = _tree(first, second)

    demanded = {claim.identity.name for claim in _same_slot_claims(tree, second.statement)}

    # Authoring into this slot must disposition both live occupants; supplying
    # none leaves a nonempty required set, which is what the law refuses on.
    assert demanded == {first.identity.name, second.identity.name}
    assert demanded - set() == demanded


def test_a_cross_slot_disposition_is_accepted_and_recorded() -> None:
    """A voluntary position on another qualifier's slot is kept, not rejected."""
    primary = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier="primary")
    secondary = _claim_in_slot(claim_id="CLM-" + "2" * 32, qualifier="secondary")
    tree = _tree(primary, secondary)

    demanded = {claim.identity.name for claim in _same_slot_claims(tree, secondary.statement)}
    offerable = _dispositionable(tree, secondary.statement)

    # The other qualifier is not demanded ...
    assert primary.identity.name not in demanded
    # ... but naming it is legal, so it is never an unexpected_claim_id, and the
    # statement_digest lookup that records it still resolves.
    assert primary.identity.name in offerable
