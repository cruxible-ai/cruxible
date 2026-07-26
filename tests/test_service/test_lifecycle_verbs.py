"""Receipted claim/entity lifecycle adjudication semantics."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import (
    ConfigError,
    DataValidationError,
    EntityNotFoundError,
    PermissionDeniedError,
    TerminalLifecycleWriteRefusedError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import RelationshipReviewState
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, mint_claim_id
from cruxible_core.runtime.permissions import (
    PermissionMode,
    init_permissions,
    reset_permissions,
)
from cruxible_core.service import (
    service_add_entities,
    service_attest,
    service_get_receipt,
    service_list,
    service_list_attestations,
    service_query_inline_surface,
    service_retire_entity,
    service_retract_claim,
    service_supersede_claim,
    service_supersede_entity,
)
from cruxible_core.temporal import utc_now


def _actor(actor_id: str = "lifecycle-reviewer") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="service_account",
        actor_id=actor_id,
        org_id="org-lifecycle",
        operation_id=f"op-{actor_id}",
        timestamp=utc_now(),
    )


def _fits_claims(instance: CruxibleInstance) -> tuple[str, str]:
    graph = instance.load_graph()
    predecessor = graph.get_relationship("Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", "fits")
    successor = graph.get_relationship("Part", "BP-1001", "Vehicle", "V-2024-ACCORD-SPORT", "fits")
    assert predecessor is not None and predecessor.claim_id is not None
    assert successor is not None and successor.claim_id is not None
    return predecessor.claim_id, successor.claim_id


def _replaces_claim(instance: CruxibleInstance) -> str:
    relationship = instance.load_graph().get_relationship(
        "Part", "BP-1002", "Part", "BP-1001", "replaces"
    )
    assert relationship is not None and relationship.claim_id is not None
    return relationship.claim_id


def _assert_receipt_has_adjudication(
    instance: CruxibleInstance,
    receipt_id: str | None,
    *,
    operation_type: str,
    to_status: str,
) -> None:
    assert receipt_id is not None
    receipt = service_get_receipt(instance, receipt_id)
    assert receipt.operation_type == operation_type
    assert receipt.committed is True
    assert receipt.actor_context is not None
    details = [node.detail.get("lifecycle_adjudication") for node in receipt.nodes]
    adjudication = next(detail for detail in details if detail is not None)
    assert adjudication["transition"]["to"] == to_status
    assert adjudication["reason"]
    assert "subject" in adjudication
    assert "successor_ref" in adjudication


def test_supersede_claim_writes_both_pointers_close_fields_and_receipt(
    populated_instance: CruxibleInstance,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)

    result = service_supersede_claim(
        populated_instance,
        predecessor_id,
        successor_id,
        reason="catalog claim replaced",
        actor_context=_actor(),
        evidence_ref={"source": "catalog", "source_record_id": "fitment-2026"},
    )

    predecessor = populated_instance.load_graph().find_relationship_by_claim_id(predecessor_id)
    successor = populated_instance.load_graph().find_relationship_by_claim_id(successor_id)
    assert predecessor is not None and successor is not None
    predecessor_lifecycle = predecessor.metadata.assertion.lifecycle
    successor_lifecycle = successor.metadata.assertion.lifecycle
    assert predecessor_lifecycle.status == "superseded"
    assert predecessor_lifecycle.reason == "catalog claim replaced"
    assert predecessor_lifecycle.closed_at is not None
    assert predecessor_lifecycle.closed_by == "lifecycle-reviewer"
    assert predecessor_lifecycle.superseded_by is not None
    assert predecessor_lifecycle.superseded_by.model_dump(exclude_none=True) == {
        "claim_id": successor_id
    }
    assert successor_lifecycle.status == "active"
    assert successor_lifecycle.supersedes is not None
    assert successor_lifecycle.supersedes.model_dump(exclude_none=True) == {
        "claim_id": predecessor_id
    }
    assert result.successor is not None
    _assert_receipt_has_adjudication(
        populated_instance,
        result.receipt_id,
        operation_type="lifecycle_supersede",
        to_status="superseded",
    )


def test_claim_supersede_updates_exact_parallel_edge_identities(
    populated_instance: CruxibleInstance,
) -> None:
    predecessor_id, untouched_id = _fits_claims(populated_instance)
    graph = populated_instance.load_graph()
    predecessor = graph.find_relationship_by_claim_id(predecessor_id)
    assert predecessor is not None
    successor_id = mint_claim_id()
    successor_key = graph.add_relationship(
        RelationshipInstance(
            relationship_type=predecessor.relationship_type,
            from_type=predecessor.from_type,
            from_id=predecessor.from_id,
            to_type=predecessor.to_type,
            to_id=predecessor.to_id,
            claim_id=successor_id,
            properties=dict(predecessor.properties),
        )
    )
    populated_instance.save_graph(graph)

    service_supersede_claim(
        populated_instance,
        predecessor_id,
        successor_id,
        reason="parallel claim replacement",
        actor_context=_actor(),
    )

    stored = populated_instance.load_graph()
    old = stored.find_relationship_by_claim_id(predecessor_id)
    new = stored.find_relationship_by_claim_id(successor_id)
    untouched = stored.find_relationship_by_claim_id(untouched_id)
    assert old is not None and new is not None and untouched is not None
    assert new.edge_key == successor_key
    assert old.metadata.assertion.lifecycle.superseded_by is not None
    assert old.metadata.assertion.lifecycle.superseded_by.claim_id == successor_id
    assert new.metadata.assertion.lifecycle.supersedes is not None
    assert new.metadata.assertion.lifecycle.supersedes.claim_id == predecessor_id
    assert untouched.metadata.assertion.lifecycle.status == "active"


def _add_parallel_fits_edge(
    instance: CruxibleInstance,
    *,
    template_claim_id: str,
    properties: dict[str, Any],
) -> str:
    """Add a SECOND edge on an existing tuple, with different properties."""
    graph = instance.load_graph()
    template = graph.find_relationship_by_claim_id(template_claim_id)
    assert template is not None
    claim_id = mint_claim_id()
    graph.add_relationship(
        RelationshipInstance(
            relationship_type=template.relationship_type,
            from_type=template.from_type,
            from_id=template.from_id,
            to_type=template.to_type,
            to_id=template.to_id,
            claim_id=claim_id,
            properties=properties,
        )
    )
    instance.save_graph(graph)
    return claim_id


def test_retract_carries_the_target_edges_properties_verbatim(
    populated_instance: CruxibleInstance,
) -> None:
    """A lifecycle-only transition must not rewrite content from a SIBLING edge.

    Two parallel edges share a tuple with DIFFERENT properties. Retracting one
    addresses it by claim_id, so its own properties must come back byte-
    identical — not merged with, or replaced by, the tuple's first match. The
    sibling must be entirely untouched.
    """
    first_id, _ = _fits_claims(populated_instance)
    second_id = _add_parallel_fits_edge(
        populated_instance,
        template_claim_id=first_id,
        properties={"verified": False},
    )

    before = populated_instance.load_graph()
    first_before = before.find_relationship_by_claim_id(first_id)
    second_before = before.find_relationship_by_claim_id(second_id)
    assert first_before is not None and second_before is not None
    # Precondition: the two edges genuinely differ, or the test proves nothing.
    assert first_before.properties != second_before.properties

    service_retract_claim(
        populated_instance,
        second_id,
        reason="parallel claim withdrawn",
        actor_context=_actor(),
    )

    after = populated_instance.load_graph()
    first_after = after.find_relationship_by_claim_id(first_id)
    second_after = after.find_relationship_by_claim_id(second_id)
    assert first_after is not None and second_after is not None

    # The retracted edge kept its OWN content, exactly.
    assert second_after.properties == second_before.properties
    assert second_after.metadata.assertion.lifecycle.status == "retracted"
    # The sibling kept its content AND its live standing.
    assert first_after.properties == first_before.properties
    assert first_after.metadata.assertion.lifecycle.status == "active"


def test_retract_succeeds_after_the_types_schema_grows_a_required_property(
    populated_instance: CruxibleInstance,
) -> None:
    """Schema drift must not make an existing claim un-adjudicable.

    A required property added to the relationship type AFTER an edge was written
    would fail content re-validation. Adjudication introduces no new content, so
    it must not be gated on today's schema admitting yesterday's row — otherwise
    exactly the stale claims a schema change was made to retire become the ones
    that can never be retracted.
    """
    from tests.test_cli.conftest import CAR_PARTS_YAML

    claim_id, _ = _fits_claims(populated_instance)
    drifted = CAR_PARTS_YAML.replace(
        """      source:
        type: string
        optional: true
""",
        """      source:
        type: string
        optional: true
      grade:
        type: string
""",
    )
    assert "grade" in drifted
    populated_instance.get_config_path().write_text(drifted)

    result = service_retract_claim(
        populated_instance,
        claim_id,
        reason="stale fitment claim, retired with the schema change",
        actor_context=_actor(),
    )

    assert result.action == "retract"
    stored = populated_instance.load_graph().find_relationship_by_claim_id(claim_id)
    assert stored is not None
    assert stored.metadata.assertion.lifecycle.status == "retracted"
    # No phantom value was invented for the newly-required property.
    assert "grade" not in stored.properties


def test_claim_supersede_refuses_a_successor_that_already_supersedes(
    populated_instance: CruxibleInstance,
) -> None:
    """A successor records ONE predecessor; a second supersession is refused.

    ``supersedes`` is a single pointer, so B->C after A->C would overwrite C's
    back-reference and strand A's ``superseded_by`` pointing at a claim that no
    longer points back — the unpaired corpus D3's both-direction pointers exist
    to prevent.
    """
    a_id, c_id = _fits_claims(populated_instance)
    b_id = _add_parallel_fits_edge(
        populated_instance,
        template_claim_id=a_id,
        properties={"verified": True, "source": "second-opinion"},
    )

    service_supersede_claim(
        populated_instance,
        a_id,
        c_id,
        reason="first supersession",
        actor_context=_actor(),
    )

    with pytest.raises(ConfigError) as excinfo:
        service_supersede_claim(
            populated_instance,
            b_id,
            c_id,
            reason="second supersession onto the same successor",
            actor_context=_actor(),
        )
    # The refusal NAMES the supersession already on record.
    assert a_id in str(excinfo.value)
    assert "already supersedes" in str(excinfo.value)

    stored = populated_instance.load_graph()
    a_after = stored.find_relationship_by_claim_id(a_id)
    b_after = stored.find_relationship_by_claim_id(b_id)
    c_after = stored.find_relationship_by_claim_id(c_id)
    assert a_after is not None and b_after is not None and c_after is not None
    # The first supersession stands, paired, and B is untouched.
    assert c_after.metadata.assertion.lifecycle.supersedes is not None
    assert c_after.metadata.assertion.lifecycle.supersedes.claim_id == a_id
    assert a_after.metadata.assertion.lifecycle.status == "superseded"
    assert b_after.metadata.assertion.lifecycle.status == "active"
    assert b_after.metadata.assertion.lifecycle.superseded_by is None


def test_entity_supersede_refuses_a_successor_that_already_supersedes(
    populated_instance: CruxibleInstance,
) -> None:
    """Entity supersession refuses successor reuse for the same reason as claims."""
    service_add_entities(
        populated_instance,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-2001",
                properties={
                    "part_number": "BP-2001",
                    "name": "Successor Pads",
                    "category": "brakes",
                },
            )
        ],
    )

    service_supersede_entity(
        populated_instance,
        "Part",
        "BP-1001",
        "Part",
        "BP-2001",
        reason="first supersession",
        actor_context=_actor(),
    )

    with pytest.raises(ConfigError) as excinfo:
        service_supersede_entity(
            populated_instance,
            "Part",
            "BP-1002",
            "Part",
            "BP-2001",
            reason="second supersession onto the same successor",
            actor_context=_actor(),
        )
    assert "already supersedes" in str(excinfo.value)
    assert "Part:BP-1001" in str(excinfo.value)

    graph = populated_instance.load_graph()
    successor = graph.get_entity("Part", "BP-2001")
    second = graph.get_entity("Part", "BP-1002")
    assert successor is not None and second is not None
    assert successor.metadata.lifecycle is not None
    assert successor.metadata.lifecycle.supersedes is not None
    assert successor.metadata.lifecycle.supersedes.entity_id == "BP-1001"
    assert second.metadata.lifecycle_status() == "live"


def test_retired_entity_still_accepts_governed_writes(
    populated_instance: CruxibleInstance,
) -> None:
    """Retirement blocks DIRECT re-adds; it does not brick governed ingest.

    The chokepoint cannot distinguish a re-add from an update (the row exists
    either way), so the guard narrows on SOURCE. Refusing every source would let
    one retirement permanently fail an unattended ``workflow_apply`` re-ingest —
    the apply loop raises out rather than reporting per-row outcomes, so a
    single retired id would take the whole batch down forever. Governed sources
    therefore still reach a retired entity; that is a stated gap pending the
    deferred reinstate verb, and it is the receipted, attributable half of the
    trade.
    """
    from cruxible_core.graph.operations import apply_entity, validate_entity

    service_retire_entity(
        populated_instance,
        "Part",
        "BP-1001",
        reason="part discontinued",
        actor_context=_actor(),
    )

    config = populated_instance.load_config()
    graph = populated_instance.load_graph()
    validated = validate_entity(
        config,
        graph,
        "Part",
        "BP-1001",
        {"part_number": "BP-1001", "name": "Ceramic Brake Pads (re-ingested)"},
    )
    # A governed source passes through the chokepoint...
    apply_entity(graph, validated, config=config, source="workflow_apply")
    populated_instance.save_graph(graph)
    reingested = populated_instance.load_graph().get_entity("Part", "BP-1001")
    assert reingested is not None
    assert reingested.properties["name"] == "Ceramic Brake Pads (re-ingested)"
    # ...and retirement is NOT silently undone by it.
    assert reingested.metadata.lifecycle_status() == "retired"

    # ...while a direct write is still refused.
    with pytest.raises(DataValidationError, match="Part:BP-1001 is retired"):
        service_add_entities(
            populated_instance,
            [
                EntityInstance(
                    entity_type="Part",
                    entity_id="BP-1001",
                    properties={"name": "Doppelganger"},
                )
            ],
        )


def test_retract_claim_stays_resolvable_with_settled_state_and_receipt(
    populated_instance: CruxibleInstance,
) -> None:
    claim_id, _ = _fits_claims(populated_instance)
    claim = populated_instance.load_graph().find_relationship_by_claim_id(claim_id)
    assert claim is not None
    service_attest(
        populated_instance,
        relationship_type=claim.relationship_type,
        from_type=claim.from_type,
        from_id=claim.from_id,
        to_type=claim.to_type,
        to_id=claim.to_id,
        claim_id=claim_id,
        stance="support",
        evidence_refs=[{"source": "catalog", "source_record_id": "retracted-summary"}],
        observed_at=utc_now(),
        actor_context=_actor("observer"),
    )

    result = service_retract_claim(
        populated_instance,
        claim_id,
        reason="manufacturer withdrew fitment",
        actor_context=_actor(),
    )

    retracted = populated_instance.load_graph().find_relationship_by_claim_id(claim_id)
    assert retracted is not None
    assert retracted.metadata.assertion.lifecycle.status == "retracted"
    assert retracted.metadata.assertion.lifecycle.closed_at is not None
    assert retracted.metadata.assertion.lifecycle.closed_by == "lifecycle-reviewer"
    assert result.claim.claim_id == claim_id
    summary = service_list_attestations(populated_instance).items[0]
    assert summary.unresolved_target is False
    assert summary.current_claim_state == "retracted"
    _assert_receipt_has_adjudication(
        populated_instance,
        result.receipt_id,
        operation_type="lifecycle_retract",
        to_status="retracted",
    )


def test_lifecycle_status_reader_distinguishes_retracted_from_superseded(
    populated_instance: CruxibleInstance,
) -> None:
    claim_id, _ = _fits_claims(populated_instance)
    service_retract_claim(
        populated_instance,
        claim_id,
        reason="manufacturer withdrew fitment",
        actor_context=_actor(),
    )

    retracted = service_list(
        populated_instance,
        "edges",
        relationship_type="fits",
        lifecycle_status="retracted",
    )
    superseded = service_list(
        populated_instance,
        "edges",
        relationship_type="fits",
        lifecycle_status="superseded",
    )

    assert [item["claim_id"] for item in retracted.items] == [claim_id]
    assert claim_id not in {item["claim_id"] for item in superseded.items}
    definition = {
        "name": "settled_fits",
        "mode": "collection",
        "returns": "fits",
        "result_shape": "relationship",
        "dedupe": "path",
        "relationship_state": "all",
    }
    query_retracted = service_query_inline_surface(
        populated_instance,
        definition,
        {},
        lifecycle_status="retracted",
    )
    query_superseded = service_query_inline_surface(
        populated_instance,
        definition,
        {},
        lifecycle_status="superseded",
    )
    assert [cast(RelationshipInstance, item).claim_id for item in query_retracted.items] == [
        claim_id
    ]
    assert claim_id not in {
        cast(RelationshipInstance, item).claim_id for item in query_superseded.items
    }


def test_superseded_attestation_summary_surfaces_successor_pointer(
    populated_instance: CruxibleInstance,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)
    predecessor = populated_instance.load_graph().find_relationship_by_claim_id(predecessor_id)
    assert predecessor is not None
    service_attest(
        populated_instance,
        relationship_type=predecessor.relationship_type,
        from_type=predecessor.from_type,
        from_id=predecessor.from_id,
        to_type=predecessor.to_type,
        to_id=predecessor.to_id,
        claim_id=predecessor_id,
        stance="support",
        evidence_refs=[{"source": "catalog", "source_record_id": "fitment-2026"}],
        observed_at=utc_now(),
        actor_context=_actor("observer"),
    )
    service_supersede_claim(
        populated_instance,
        predecessor_id,
        successor_id,
        reason="catalog claim replaced",
        actor_context=_actor(),
    )

    result = service_list_attestations(populated_instance)
    summary = next(item for item in result.items if item.attestation.claim_id == predecessor_id)
    assert summary.current_claim_state == "superseded"
    assert summary.successor_ref is not None
    assert summary.successor_ref.model_dump(exclude_none=True) == {"claim_id": successor_id}


def test_supersede_entity_writes_typed_pointers_without_migrating_edges(
    populated_instance: CruxibleInstance,
) -> None:
    before_successor_edges = [
        relationship.claim_id
        for relationship in populated_instance.load_graph().iter_relationships()
        if (relationship.from_type, relationship.from_id) == ("Part", "BP-1002")
        or (relationship.to_type, relationship.to_id) == ("Part", "BP-1002")
    ]

    result = service_supersede_entity(
        populated_instance,
        "Part",
        "BP-1001",
        "Part",
        "BP-1002",
        reason="part number replaced",
        actor_context=_actor(),
    )

    graph = populated_instance.load_graph()
    predecessor = graph.get_entity("Part", "BP-1001")
    successor = graph.get_entity("Part", "BP-1002")
    assert predecessor is not None and successor is not None
    assert predecessor.metadata.lifecycle is not None
    assert successor.metadata.lifecycle is not None
    assert predecessor.metadata.lifecycle.status == "superseded"
    assert predecessor.metadata.lifecycle.closed_at is not None
    assert predecessor.metadata.lifecycle.closed_by == "lifecycle-reviewer"
    assert predecessor.metadata.lifecycle.superseded_by is not None
    assert predecessor.metadata.lifecycle.superseded_by.model_dump(exclude_none=True) == {
        "entity_type": "Part",
        "entity_id": "BP-1002",
    }
    assert successor.metadata.lifecycle.supersedes is not None
    assert successor.metadata.lifecycle.supersedes.model_dump(exclude_none=True) == {
        "entity_type": "Part",
        "entity_id": "BP-1001",
    }
    after_successor_edges = [
        relationship.claim_id
        for relationship in graph.iter_relationships()
        if (relationship.from_type, relationship.from_id) == ("Part", "BP-1002")
        or (relationship.to_type, relationship.to_id) == ("Part", "BP-1002")
    ]
    assert after_successor_edges == before_successor_edges
    _assert_receipt_has_adjudication(
        populated_instance,
        result.receipt_id,
        operation_type="lifecycle_supersede",
        to_status="superseded",
    )


def test_retire_entity_reports_live_edges_it_strands(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_retire_entity(
        populated_instance,
        "Part",
        "BP-1001",
        reason="part discontinued",
        actor_context=_actor(),
    )

    assert result.stranded_live_edge_count == 3
    retired = populated_instance.load_graph().get_entity("Part", "BP-1001")
    assert retired is not None and retired.metadata.lifecycle is not None
    assert retired.metadata.lifecycle.status == "retired"
    receipt = service_get_receipt(populated_instance, result.receipt_id or "")
    adjudication = next(
        node.detail["lifecycle_adjudication"]
        for node in receipt.nodes
        if "lifecycle_adjudication" in node.detail
    )
    assert adjudication["stranded_live_edge_count"] == 3
    retired_entities = service_list(
        populated_instance,
        "entities",
        entity_type="Part",
        lifecycle_status="retired",
    )
    assert [item.entity_id for item in retired_entities.items] == ["BP-1001"]


@pytest.mark.parametrize("missing", ["reason", "actor"])
@pytest.mark.parametrize(
    "verb",
    ["supersede_claim", "retract_claim", "supersede_entity", "retire_entity"],
)
def test_every_lifecycle_verb_requires_reason_and_actor_with_refusal_receipt(
    populated_instance: CruxibleInstance,
    verb: str,
    missing: str,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)
    kwargs: dict[str, Any] = {
        "reason": "" if missing == "reason" else "settled by reviewer",
        "actor_context": None if missing == "actor" else _actor(),
    }
    calls: dict[str, Callable[[], Any]] = {
        "supersede_claim": lambda: service_supersede_claim(
            populated_instance, predecessor_id, successor_id, **kwargs
        ),
        "retract_claim": lambda: service_retract_claim(
            populated_instance, predecessor_id, **kwargs
        ),
        "supersede_entity": lambda: service_supersede_entity(
            populated_instance, "Part", "BP-1001", "Part", "BP-1002", **kwargs
        ),
        "retire_entity": lambda: service_retire_entity(
            populated_instance, "Part", "BP-1001", **kwargs
        ),
    }

    with pytest.raises(ConfigError) as exc_info:
        calls[verb]()
    assert exc_info.value.mutation_receipt_id is not None
    receipt = service_get_receipt(
        populated_instance,
        exc_info.value.mutation_receipt_id,
    )
    assert receipt.committed is False


@pytest.mark.parametrize(
    ("successor", "match"),
    [
        ("missing", "not found"),
        ("wrong_type", "same relationship type"),
        ("self", "cannot supersede itself"),
    ],
)
def test_claim_supersede_refuses_invalid_successors_with_receipts(
    populated_instance: CruxibleInstance,
    successor: str,
    match: str,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)
    if successor == "missing":
        successor_id = "CLM-missing"
    elif successor == "wrong_type":
        successor_id = _replaces_claim(populated_instance)
    elif successor == "self":
        successor_id = predecessor_id

    with pytest.raises(ConfigError, match=match) as exc_info:
        service_supersede_claim(
            populated_instance,
            predecessor_id,
            successor_id,
            reason="invalid successor",
            actor_context=_actor(),
        )
    assert exc_info.value.mutation_receipt_id is not None


def test_claim_supersede_refuses_non_live_successor(
    populated_instance: CruxibleInstance,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)
    service_retract_claim(
        populated_instance,
        successor_id,
        reason="successor withdrawn",
        actor_context=_actor(),
    )

    with pytest.raises(ConfigError, match="must be live") as exc_info:
        service_supersede_claim(
            populated_instance,
            predecessor_id,
            successor_id,
            reason="bad successor",
            actor_context=_actor(),
        )
    assert exc_info.value.mutation_receipt_id is not None


@pytest.mark.parametrize(
    ("successor_type", "successor_id", "match", "error_type"),
    [
        # Naming an entity that does not exist is a NOT-FOUND, not a bad
        # request: the standard class carries it (and the HTTP mapper answers
        # 404 rather than 400).
        ("Part", "missing", "not found", EntityNotFoundError),
        ("Vehicle", "V-2024-CIVIC-EX", "same entity type", ConfigError),
        ("Part", "BP-1001", "cannot supersede itself", ConfigError),
    ],
)
def test_entity_supersede_refuses_invalid_successors_with_receipts(
    populated_instance: CruxibleInstance,
    successor_type: str,
    successor_id: str,
    match: str,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match=match) as exc_info:
        service_supersede_entity(
            populated_instance,
            "Part",
            "BP-1001",
            successor_type,
            successor_id,
            reason="invalid successor",
            actor_context=_actor(),
        )
    assert exc_info.value.mutation_receipt_id is not None


def test_entity_supersede_refuses_non_live_successor(
    populated_instance: CruxibleInstance,
) -> None:
    service_retire_entity(
        populated_instance,
        "Part",
        "BP-1002",
        reason="successor retired",
        actor_context=_actor(),
    )

    with pytest.raises(ConfigError, match="must be live") as exc_info:
        service_supersede_entity(
            populated_instance,
            "Part",
            "BP-1001",
            "Part",
            "BP-1002",
            reason="bad successor",
            actor_context=_actor(),
        )
    assert exc_info.value.mutation_receipt_id is not None


def test_settled_predecessors_are_not_eligible(
    populated_instance: CruxibleInstance,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)
    service_retract_claim(
        populated_instance,
        predecessor_id,
        reason="claim withdrawn",
        actor_context=_actor(),
    )
    with pytest.raises(ConfigError, match="not eligible") as claim_exc:
        service_supersede_claim(
            populated_instance,
            predecessor_id,
            successor_id,
            reason="cannot replace settled predecessor",
            actor_context=_actor(),
        )
    assert claim_exc.value.mutation_receipt_id is not None

    service_retire_entity(
        populated_instance,
        "Part",
        "BP-1001",
        reason="entity retired",
        actor_context=_actor(),
    )
    with pytest.raises(ConfigError, match="not eligible") as entity_exc:
        service_supersede_entity(
            populated_instance,
            "Part",
            "BP-1001",
            "Part",
            "BP-1002",
            reason="cannot replace settled predecessor",
            actor_context=_actor(),
        )
    assert entity_exc.value.mutation_receipt_id is not None


def test_claim_predecessor_eligibility_uses_status_not_effective_window(
    populated_instance: CruxibleInstance,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)
    graph = populated_instance.load_graph()
    predecessor = graph.find_relationship_by_claim_id(predecessor_id)
    assert predecessor is not None
    assertion = predecessor.metadata.assertion
    lifecycle = assertion.lifecycle.model_copy(
        update={"effective_until": utc_now() - timedelta(days=1)}
    )
    predecessor.metadata = predecessor.metadata.model_copy(
        update={"assertion": assertion.model_copy(update={"lifecycle": lifecycle})}
    )
    graph.update_relationship_state(
        predecessor.from_type,
        predecessor.from_id,
        predecessor.to_type,
        predecessor.to_id,
        predecessor.relationship_type,
        edge_key=predecessor.edge_key,
        metadata=predecessor.metadata,
    )
    populated_instance.save_graph(graph)

    result = service_supersede_claim(
        populated_instance,
        predecessor_id,
        successor_id,
        reason="replace naturally expired claim",
        actor_context=_actor(),
    )
    assert result.claim.metadata.assertion.lifecycle.status == "superseded"


@pytest.mark.parametrize("review_status", ["pending", "rejected"])
def test_claim_predecessor_pending_or_rejected_is_refused(
    populated_instance: CruxibleInstance,
    review_status: str,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)
    graph = populated_instance.load_graph()
    predecessor = graph.find_relationship_by_claim_id(predecessor_id)
    assert predecessor is not None
    assertion = predecessor.metadata.assertion.model_copy(
        update={"review": RelationshipReviewState(status=review_status)}  # type: ignore[arg-type]
    )
    predecessor.metadata = predecessor.metadata.model_copy(update={"assertion": assertion})
    graph.update_relationship_state(
        predecessor.from_type,
        predecessor.from_id,
        predecessor.to_type,
        predecessor.to_id,
        predecessor.relationship_type,
        edge_key=predecessor.edge_key,
        metadata=predecessor.metadata,
    )
    populated_instance.save_graph(graph)

    with pytest.raises(ConfigError, match="not eligible") as exc_info:
        service_supersede_claim(
            populated_instance,
            predecessor_id,
            successor_id,
            reason="invalid predecessor state",
            actor_context=_actor(),
        )
    assert exc_info.value.mutation_receipt_id is not None


@pytest.mark.parametrize(
    "verb",
    ["supersede_claim", "retract_claim", "supersede_entity", "retire_entity"],
)
def test_every_lifecycle_service_tier_refusal_is_receipted(
    populated_instance: CruxibleInstance,
    verb: str,
) -> None:
    predecessor_id, successor_id = _fits_claims(populated_instance)
    calls: dict[str, Callable[[], Any]] = {
        "supersede_claim": lambda: service_supersede_claim(
            populated_instance,
            predecessor_id,
            successor_id,
            reason="reviewer-only act",
            actor_context=_actor(),
        ),
        "retract_claim": lambda: service_retract_claim(
            populated_instance,
            predecessor_id,
            reason="reviewer-only act",
            actor_context=_actor(),
        ),
        "supersede_entity": lambda: service_supersede_entity(
            populated_instance,
            "Part",
            "BP-1001",
            "Part",
            "BP-1002",
            reason="reviewer-only act",
            actor_context=_actor(),
        ),
        "retire_entity": lambda: service_retire_entity(
            populated_instance,
            "Part",
            "BP-1001",
            reason="reviewer-only act",
            actor_context=_actor(),
        ),
    }
    reset_permissions()
    init_permissions(PermissionMode.GOVERNED_WRITE)
    try:
        with pytest.raises(PermissionDeniedError) as exc_info:
            calls[verb]()
        assert exc_info.value.mutation_receipt_id is not None
        assert (
            service_get_receipt(populated_instance, exc_info.value.mutation_receipt_id).committed
            is False
        )
    finally:
        reset_permissions()
        init_permissions(PermissionMode.ADMIN)


def test_concurrent_retract_of_successor_is_seen_before_supersede_validation(
    populated_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    from cruxible_core.service import artifact_lifecycle

    predecessor_id, successor_id = _fits_claims(populated_instance)
    second_instance = CruxibleInstance.load(populated_instance.get_root_path())

    # An ORDERED log, not just a final outcome: the property under test is
    # WHEN the superseding writer read its successor, and only the order proves
    # it. A load-before-receipt implementation would produce the same final
    # state (a refusal) by reading a still-active successor and refusing for
    # some other reason later — or worse, by succeeding on stale state.
    timeline: list[str] = []
    timeline_lock = threading.Lock()

    def note(event: str) -> None:
        with timeline_lock:
            timeline.append(event)

    retract_holds_transaction = threading.Event()
    release_retract = threading.Event()
    supersede_at_write_boundary = threading.Event()

    original_apply = artifact_lifecycle._apply_claim_lifecycle
    original_get_claim = artifact_lifecycle._get_claim
    original_mutation_receipt = artifact_lifecycle.mutation_receipt

    def pausing_apply(*args: Any, **kwargs: Any) -> Any:
        relationship = kwargs["relationship"]
        if kwargs["source"] == "lifecycle_retract" and relationship.claim_id == successor_id:
            # The retract is now INSIDE its BEGIN IMMEDIATE, holding the writer
            # lock with the retraction not yet committed.
            retract_holds_transaction.set()
            assert release_retract.wait(timeout=10)
        return original_apply(*args, **kwargs)

    def observing_get_claim(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("role") == "successor":
            note("supersede:read-successor")
        return original_get_claim(*args, **kwargs)

    @contextlib.contextmanager
    def observing_mutation_receipt(
        instance: Any,
        operation_type: Any,
        parameters: Any,
        **kwargs: Any,
    ) -> Any:
        # Set BEFORE __enter__, i.e. before the BEGIN IMMEDIATE this thread is
        # about to block on. Releasing the retract only after this fires is what
        # makes the interleaving deterministic instead of a sleep-free race: the
        # superseding thread is provably at its write boundary, not merely
        # spawned.
        if operation_type == "lifecycle_supersede":
            supersede_at_write_boundary.set()
        with original_mutation_receipt(instance, operation_type, parameters, **kwargs) as ctx:
            yield ctx

    monkeypatch.setattr(artifact_lifecycle, "_apply_claim_lifecycle", pausing_apply)
    monkeypatch.setattr(artifact_lifecycle, "_get_claim", observing_get_claim)
    monkeypatch.setattr(artifact_lifecycle, "mutation_receipt", observing_mutation_receipt)
    errors: dict[str, BaseException] = {}

    def retract() -> None:
        try:
            service_retract_claim(
                populated_instance,
                successor_id,
                reason="concurrent withdrawal",
                actor_context=_actor("retractor"),
            )
            note("retract:committed")
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors["retract"] = exc

    def supersede() -> None:
        try:
            service_supersede_claim(
                second_instance,
                predecessor_id,
                successor_id,
                reason="must observe concurrent withdrawal",
                actor_context=_actor("superseder"),
            )
        except BaseException as exc:
            errors["supersede"] = exc

    retract_thread = threading.Thread(target=retract)
    supersede_thread = threading.Thread(target=supersede)
    retract_thread.start()
    assert retract_holds_transaction.wait(timeout=10)
    supersede_thread.start()
    # Gate the release on the SUPERSEDE thread reaching its write boundary.
    assert supersede_at_write_boundary.wait(timeout=10)
    release_retract.set()
    retract_thread.join(timeout=10)
    supersede_thread.join(timeout=10)

    assert not retract_thread.is_alive()
    assert not supersede_thread.is_alive()
    assert "retract" not in errors

    # THE ordering proof: the superseding writer read its successor only after
    # the retraction had committed. It could not have read it earlier — its read
    # lives inside a BEGIN IMMEDIATE that the retract's own transaction held.
    assert "retract:committed" in timeline
    assert "supersede:read-successor" in timeline
    assert timeline.index("supersede:read-successor") > timeline.index("retract:committed"), (
        f"successor was read outside the write boundary; timeline={timeline}"
    )

    # ...and the refusal names the state that in-boundary read observed, rather
    # than being any refusal that happens to land.
    assert isinstance(errors.get("supersede"), ConfigError)
    assert "must be live" in str(errors["supersede"])
    assert "retracted" in str(errors["supersede"])
    predecessor = second_instance.load_graph().find_relationship_by_claim_id(predecessor_id)
    successor = second_instance.load_graph().find_relationship_by_claim_id(successor_id)
    assert predecessor is not None and successor is not None
    assert predecessor.metadata.assertion.lifecycle.status == "active"
    assert successor.metadata.assertion.lifecycle.status == "retracted"


def test_readding_retired_entity_refuses_and_teaches_future_reinstate(
    populated_instance: CruxibleInstance,
) -> None:
    service_retire_entity(
        populated_instance,
        "Part",
        "BP-1001",
        reason="part discontinued",
        actor_context=_actor(),
    )

    with pytest.raises(
        DataValidationError,
        match=r"Part:BP-1001 is retired.*deferred reinstate adjudication",
    ) as exc_info:
        service_add_entities(
            populated_instance,
            [
                EntityInstance(
                    entity_type="Part",
                    entity_id="BP-1001",
                    properties={"name": "Doppelganger"},
                )
            ],
        )
    assert exc_info.value.mutation_receipt_id is not None


def test_trusted_transition_capability_has_only_lifecycle_service_callers() -> None:
    import ast
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src" / "cruxible_core"
    callers = []
    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and any(
                keyword.arg == "trusted_lifecycle_transition"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            for node in ast.walk(tree)
        ):
            callers.append(str(path.relative_to(src_root)))
    assert callers == ["service/artifact_lifecycle.py"]


def test_free_write_teaching_message_names_the_verbs_of_the_refused_kind() -> None:
    """The refusal teaches the verbs that apply to the write that was refused.

    Kind-aware, not a menu of all four: a caller who free-wrote an entity
    lifecycle is told about ``cruxible entity ...``, and nothing about the
    relationship verbs, which cannot help them. The shared
    "terminal lifecycle transitions require" phrasing is pinned by the HTTP
    parity test and stays stable across both kinds.
    """
    relationship_message = str(
        TerminalLifecycleWriteRefusedError("relationship", "retracted", "active")
    )
    assert "terminal lifecycle transitions require" in relationship_message
    assert "cruxible relationship supersede" in relationship_message
    assert "cruxible relationship retract" in relationship_message
    assert "cruxible entity" not in relationship_message
    assert "wi-lifecycle-verbs" not in relationship_message

    entity_message = str(TerminalLifecycleWriteRefusedError("entity", "retired", "live"))
    assert "terminal lifecycle transitions require" in entity_message
    assert "cruxible entity supersede" in entity_message
    assert "cruxible entity retire" in entity_message
    assert "cruxible relationship" not in entity_message
    assert "wi-lifecycle-verbs" not in entity_message
