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


def test_a_subject_object_predicate_traversal_is_accepted() -> None:
    """The guard must not refuse the traversal it was built to allow."""
    from cruxible_client.contracts.claim_types import (
        AcceptedClaimType,
        claim_type_digest,
        claim_type_path,
    )
    from cruxible_client.contracts.query.grammar import QueryEntryV1, QueryTraversalStepV1
    from cruxible_core.playbill.proposals import _literal_object_traversal
    from tests.test_playbill.test_claims import _claim_type

    literal = _claim_type()
    subject_typed = literal.model_copy(
        update={
            "object_kind": "subject",
            "literal_schema": None,
            "allowed_object_subject_kinds": ("project.work_item",),
        }
    )
    accepted = AcceptedClaimType(
        path=claim_type_path(subject_typed.predicate),
        claim_type=subject_typed,
        artifact_digest=claim_type_digest(subject_typed).tagged,
    )

    class _Definition:
        traversal = (
            QueryTraversalStepV1(
                binding="st",
                from_binding="svc",
                predicate=subject_typed.predicate,
                direction="reverse",
            ),
        )
        entry = QueryEntryV1(binding="svc", subject_kinds=("project.work_item",))

    assert (
        _literal_object_traversal(
            _Definition(),  # type: ignore[arg-type]
            claim_types={subject_typed.identity.qualified: accepted},
        )
        is None
    )


def test_the_refusal_reaches_the_proposal_as_a_member_diagnostic() -> None:
    """The law refuses at proposal evaluation, not only in the helper."""
    from cruxible_client.contracts.claim_types import (
        AcceptedClaimType,
        claim_type_digest,
        claim_type_path,
    )
    from cruxible_client.contracts.query.grammar import QueryEntryV1, QueryTraversalStepV1
    from cruxible_core.playbill.proposals import _literal_object_traversal
    from tests.test_playbill.test_claims import _claim_type

    claim_type = _claim_type()
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
                direction="forward",
            ),
        )
        entry = QueryEntryV1(binding="svc", subject_kinds=("project.work_item",))

    diagnostic = _literal_object_traversal(
        _Definition(),  # type: ignore[arg-type]
        claim_types={claim_type.identity.qualified: accepted},
    )

    assert diagnostic is not None
    # The member evaluator returns exactly this as its refusal set, so a
    # proposal carrying such a definition is refused rather than accepted.
    assert diagnostic.severity == "error"
    assert diagnostic.code.startswith("playbill.query_definition.")
