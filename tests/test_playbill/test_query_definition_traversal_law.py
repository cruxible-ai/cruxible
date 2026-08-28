"""A relation traversal must name a Subject-typed predicate."""

from __future__ import annotations


def test_a_traversal_over_a_literal_predicate_is_refused_naming_the_claim_type() -> None:
    """Reverse traversal used to answer silence; the definition never accepts now."""
    from cruxible_client.contracts.claim_types import (
        AcceptedClaimType,
        claim_type_digest,
        claim_type_path,
    )
    from cruxible_client.contracts.query.grammar import (
        QueryEntryV1,
        QueryTraversalStepV1,
    )
    from cruxible_core.playbill.proposals import _literal_object_traversal
    from tests.test_playbill.test_claims import _claim_type

    claim_type = _claim_type()  # object_kind="literal"
    accepted = AcceptedClaimType(
        path=claim_type_path(claim_type.predicate),
        claim_type=claim_type,
        artifact_digest=claim_type_digest(claim_type).tagged,
    )

    class _Definition:
        traversal = (
            QueryTraversalStepV1(
                binding="st",
                from_binding="svc",
                predicate=claim_type.predicate,
                direction="reverse",
            ),
        )
        entry = QueryEntryV1(binding="svc", subject_kinds=("project.work_item",))

    diagnostic = _literal_object_traversal(
        _Definition(),  # type: ignore[arg-type]
        claim_types={claim_type.identity.qualified: accepted},
    )

    assert diagnostic is not None
    assert diagnostic.code == "playbill.query_definition.traversal_object_not_subject"
    assert "object_kind='subject'" in diagnostic.message
    assert claim_type.predicate in diagnostic.message
    assert diagnostic.subject is not None
    assert diagnostic.subject.artifact_path == accepted.path
