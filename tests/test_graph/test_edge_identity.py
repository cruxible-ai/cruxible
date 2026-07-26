"""Edge identity: minting, the add-side enforcement boundary, and the guards.

The invariants under test, stated once:

* the id is minted at exactly one place (``apply_relationship``'s create
  branch) and never re-minted afterwards;
* CONSTRUCTING an id-less ``RelationshipInstance`` is normal and must keep
  working -- candidates, wire references, and dry-run previews all do it. ADDING
  one to a graph is the programming error;
* ordering does NOT move to ``claim_id``; ``edge_key`` remains the tiebreaker.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.graph.claim_target import (
    ClaimTargetConflictError,
    resolve_claim_target,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.legacy_identity import (
    backfill_legacy_graph,
    legacy_identity_map_digest,
    record_minted_identities,
)
from cruxible_core.graph.operations import apply_relationship, validate_relationship
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    mint_claim_id,
)

CONFIG_YAML = """\
version: "1.0"
name: edge_identity_fixture
description: Minimal part/vehicle fitment config

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
  Part:
    properties:
      part_number:
        type: string
        primary_key: true

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      confidence:
        type: float
        optional: true
"""


@pytest.fixture
def config(tmp_path) -> CoreConfig:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    return load_config(path)


def _seeded_graph() -> EntityGraph:
    graph = EntityGraph()
    graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
    graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V1", properties={}))
    return graph


def _apply(graph: EntityGraph, config: CoreConfig, **props) -> RelationshipInstance:
    validated = validate_relationship(config, graph, "Part", "P1", "fits", "Vehicle", "V1", props)
    return apply_relationship(
        graph,
        validated,
        "batch_direct_write",
        "batch_direct_write",
        config=config,
    )


def test_constructing_an_id_less_relationship_is_normal() -> None:
    """The model must NOT require the id: references and candidates have none."""
    reference = RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id="P1",
        to_type="Vehicle",
        to_id="V1",
    )
    assert reference.claim_id is None


def test_adding_an_id_less_relationship_to_a_graph_raises() -> None:
    graph = _seeded_graph()
    with pytest.raises(ValueError, match="missing claim_id"):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
            )
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_claim_id_is_refused_at_the_model(blank: str) -> None:
    """Blank is not "absent". It used to slip past every ``is None`` guard.

    The add-side guard tested ``is None`` and the durable INSERT tested
    falsiness, so ``""`` failed late at persistence and whitespace-only became
    DURABLE -- an identity no lookup could ever match.
    """
    with pytest.raises(ValidationError, match="non-empty identifier"):
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="P1",
            to_type="Vehicle",
            to_id="V1",
            claim_id=blank,
        )


def test_adding_a_duplicate_claim_id_raises() -> None:
    graph = _seeded_graph()
    claim_id = mint_claim_id()
    for _ in range(1):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                claim_id=claim_id,
            )
        )
    with pytest.raises(ValueError, match="Duplicate claim_id"):
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                claim_id=claim_id,
            )
        )


def test_apply_relationship_mints_on_create_and_returns_the_durable_edge(
    config: CoreConfig,
) -> None:
    graph = _seeded_graph()
    durable = _apply(graph, config, confidence=0.5)
    assert durable.claim_id is not None
    assert durable.claim_id.startswith("CLM-")
    stored = graph.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
    assert stored is not None
    assert stored.claim_id == durable.claim_id


def test_update_never_re_mints_the_claim_id(config: CoreConfig) -> None:
    graph = _seeded_graph()
    created = _apply(graph, config, confidence=0.5)
    updated = _apply(graph, config, confidence=0.9)
    assert updated.claim_id == created.claim_id
    assert updated.properties["confidence"] == 0.9


def test_add_relationship_preserves_an_incoming_id(config: CoreConfig) -> None:
    """Every channel (to_dict, snapshot, publish/pull, backup) relies on this."""
    graph = _seeded_graph()
    created = _apply(graph, config, confidence=0.5)
    round_tripped = EntityGraph.from_dict(graph.to_dict())
    reloaded = round_tripped.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
    assert reloaded is not None
    assert reloaded.claim_id == created.claim_id


def test_from_dict_refuses_duplicate_ids_in_serialized_data(config: CoreConfig) -> None:
    graph = _seeded_graph()
    _apply(graph, config, confidence=0.5)
    payload = graph.to_dict()
    duplicated = dict(payload)
    duplicated["edges"] = [dict(payload["edges"][0]), dict(payload["edges"][0])]
    duplicated["edges"][1]["key"] = 99
    with pytest.raises(ValueError, match="Duplicate claim_id"):
        EntityGraph.from_dict(duplicated)


# ---------------------------------------------------------------- merge guard


def _identified_edge(claim_id: str, *, from_id: str = "P1") -> RelationshipInstance:
    return RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id="V1",
        claim_id=claim_id,
    )


def test_merge_refuses_a_duplicate_identity_tuple() -> None:
    base = _seeded_graph()
    base.add_relationship(_identified_edge(mint_claim_id()))
    overlay = _seeded_graph()
    overlay.add_relationship(_identified_edge(mint_claim_id()))
    with pytest.raises(ValueError, match="collides with an existing base relationship"):
        EntityGraph.merge_graphs(base, overlay)


def test_merge_refuses_a_duplicate_claim_id() -> None:
    shared = mint_claim_id()
    base = _seeded_graph()
    base.add_relationship(_identified_edge(shared))
    overlay = _seeded_graph()
    overlay.add_entity(EntityInstance(entity_type="Part", entity_id="P2", properties={}))
    overlay.add_relationship(_identified_edge(shared, from_id="P2"))
    with pytest.raises(ValueError, match="already present in the base graph"):
        EntityGraph.merge_graphs(base, overlay)


# ------------------------------------------------------------- legacy backfill


def _legacy_graph_dict(graph: EntityGraph) -> dict:
    """Serialize a graph the way a PRE-IDENTITY image would: no claim ids."""
    payload = graph.to_dict()
    for edge in payload["edges"]:
        edge.pop("claim_id", None)
    return payload


def test_backfill_mints_for_legacy_edges_only(config: CoreConfig) -> None:
    graph = _seeded_graph()
    kept = _apply(graph, config, confidence=0.5)
    graph.add_entity(EntityInstance(entity_type="Part", entity_id="P2", properties={}))
    graph.add_relationship(_identified_edge(mint_claim_id(), from_id="P2"))

    payload = graph.to_dict()
    # Strip the id from ONE edge to simulate a mixed legacy image.
    for edge in payload["edges"]:
        if edge["source"] == "Part:P2":
            edge["claim_id"] = None
    legacy = EntityGraph.from_dict(payload)

    minted = backfill_legacy_graph(legacy)
    assert [item.from_id for item in minted] == ["P2"]
    # The already-identified edge is PRESERVED, never re-minted.
    survivor = legacy.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
    assert survivor is not None
    assert survivor.claim_id == kept.claim_id


def test_backfill_reuses_the_reconcile_map_across_re_pulls(config: CoreConfig) -> None:
    """A no-op re-pull of the same pre-upgrade release must not re-mint."""
    source = _seeded_graph()
    _apply(source, config, confidence=0.5)
    legacy_payload = _legacy_graph_dict(source)

    first = EntityGraph.from_dict(legacy_payload)
    minted = backfill_legacy_graph(first)
    reconcile = record_minted_identities({}, minted)

    second = EntityGraph.from_dict(legacy_payload)
    backfill_legacy_graph(second, reuse=reconcile)

    first_edge = first.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
    second_edge = second.get_relationship("Part", "P1", "Vehicle", "V1", "fits")
    assert first_edge is not None and second_edge is not None
    assert first_edge.claim_id == second_edge.claim_id


def _parallel_legacy_payload(config: CoreConfig, *, count: int) -> dict:
    """A PRE-IDENTITY image carrying ``count`` parallel edges on ONE 5-tuple.

    The multigraph has always permitted these (``apply_relationship`` upserts by
    tuple, but pre-identity images were written by anything that could add an
    edge), and they are exactly the shape a tuple-keyed reconcile map cannot
    represent with a single id.
    """
    source = _seeded_graph()
    _apply(source, config, confidence=0.1)
    payload = _legacy_graph_dict(source)
    template = payload["edges"][0]
    payload["edges"] = [
        {
            **template,
            "key": index,
            "properties": {**template.get("properties", {}), "confidence": 0.1 * (index + 1)},
        }
        for index in range(count)
    ]
    return payload


def test_parallel_legacy_edges_keep_their_ids_across_re_pulls(config: CoreConfig) -> None:
    """PARALLEL edges are why the map value is a LIST.

    A tuple->single-id map can reconcile exactly one of a tuple's parallel
    edges, so every other one re-mints on every pull, forever -- unbounded churn
    dressed up as a bounded fix. With an ordered list, the Nth parallel edge
    keeps the Nth id across arbitrarily many re-pulls.
    """
    legacy_payload = _parallel_legacy_payload(config, count=3)

    def ids_of(graph: EntityGraph) -> list[str | None]:
        return [edge.claim_id for edge in graph.iter_relationships("fits")]

    first = EntityGraph.from_dict(legacy_payload)
    reconcile = record_minted_identities({}, backfill_legacy_graph(first))
    original = ids_of(first)
    assert len(original) == 3
    assert len(set(original)) == 3
    assert reconcile[("fits", "Part", "P1", "Vehicle", "V1")] == original

    # Three further re-pulls of the same pre-identity release: nothing churns.
    for _ in range(3):
        repulled = EntityGraph.from_dict(legacy_payload)
        reconcile = record_minted_identities({}, backfill_legacy_graph(repulled, reuse=reconcile))
        assert ids_of(repulled) == original
    assert reconcile[("fits", "Part", "P1", "Vehicle", "V1")] == original


def test_the_map_remembers_a_parallel_edge_that_briefly_disappears(config: CoreConfig) -> None:
    """A release that drops one of three parallel edges must not forget its id."""
    full_payload = _parallel_legacy_payload(config, count=3)

    first = EntityGraph.from_dict(full_payload)
    reconcile = record_minted_identities({}, backfill_legacy_graph(first))
    original = reconcile[("fits", "Part", "P1", "Vehicle", "V1")]

    shrunk_payload = _parallel_legacy_payload(config, count=3)
    shrunk_payload["edges"] = shrunk_payload["edges"][:-1]
    shrunk = EntityGraph.from_dict(shrunk_payload)
    reconcile = record_minted_identities(reconcile, backfill_legacy_graph(shrunk, reuse=reconcile))
    assert reconcile[("fits", "Part", "P1", "Vehicle", "V1")] == original

    restored = EntityGraph.from_dict(full_payload)
    backfill_legacy_graph(restored, reuse=reconcile)
    assert [edge.claim_id for edge in restored.iter_relationships("fits")] == original


def test_identity_map_digest_changes_when_ids_churn(config: CoreConfig) -> None:
    source = _seeded_graph()
    _apply(source, config, confidence=0.5)
    legacy_payload = _legacy_graph_dict(source)

    first = record_minted_identities(
        {}, backfill_legacy_graph(EntityGraph.from_dict(legacy_payload))
    )
    second = record_minted_identities(
        {}, backfill_legacy_graph(EntityGraph.from_dict(legacy_payload))
    )
    assert legacy_identity_map_digest(first) != legacy_identity_map_digest(second)
    assert legacy_identity_map_digest(first) == legacy_identity_map_digest(dict(first))


# ----------------------------------------------------- disambiguator precedence


def test_claim_id_wins_over_edge_key(config: CoreConfig) -> None:
    graph = _seeded_graph()
    created = _apply(graph, config, confidence=0.5)
    resolved = resolve_claim_target(
        graph,
        relationship_type="fits",
        from_type="Part",
        from_id="P1",
        to_type="Vehicle",
        to_id="V1",
        claim_id=created.claim_id,
    )
    assert resolved.resolved_by == "claim_id"
    assert resolved.relationship is not None
    assert resolved.relationship.claim_id == created.claim_id


def test_disagreeing_disambiguators_refuse(config: CoreConfig) -> None:
    graph = _seeded_graph()
    created = _apply(graph, config, confidence=0.5)
    assert created.edge_key is not None
    with pytest.raises(ClaimTargetConflictError, match="disagree"):
        resolve_claim_target(
            graph,
            relationship_type="fits",
            from_type="Part",
            from_id="P1",
            to_type="Vehicle",
            to_id="V1",
            claim_id=created.claim_id,
            edge_key=created.edge_key + 1000,
        )


def test_claim_id_from_another_tuple_refuses(config: CoreConfig) -> None:
    graph = _seeded_graph()
    graph.add_entity(EntityInstance(entity_type="Part", entity_id="P2", properties={}))
    created = _apply(graph, config, confidence=0.5)
    with pytest.raises(ClaimTargetConflictError, match="is not the requested claim"):
        resolve_claim_target(
            graph,
            relationship_type="fits",
            from_type="Part",
            from_id="P2",
            to_type="Vehicle",
            to_id="V1",
            claim_id=created.claim_id,
        )


def test_unknown_claim_id_with_no_live_tuple_is_unresolved_not_a_conflict() -> None:
    """Nothing to be confused WITH: the absent-claim path is unchanged."""
    graph = _seeded_graph()
    resolved = resolve_claim_target(
        graph,
        relationship_type="fits",
        from_type="Part",
        from_id="P1",
        to_type="Vehicle",
        to_id="V1",
        claim_id="CLM-doesnotexist",
    )
    assert resolved.relationship is None


def test_a_stale_claim_id_over_a_live_tuple_refuses_and_names_both_ids(
    config: CoreConfig,
) -> None:
    """The exact situation the disambiguator exists to surface.

    The caller holds an id this instance does not have while the tuple IS live:
    its claim is gone and a different one now occupies that tuple. Falling back
    to the tuple would retarget the write onto a claim the caller never saw --
    silently, and with a fabricated "raced" warning to explain it.
    """
    graph = _seeded_graph()
    live = _apply(graph, config, confidence=0.5)
    with pytest.raises(ClaimTargetConflictError) as excinfo:
        resolve_claim_target(
            graph,
            relationship_type="fits",
            from_type="Part",
            from_id="P1",
            to_type="Vehicle",
            to_id="V1",
            claim_id="CLM-staleref00000",
        )
    message = str(excinfo.value)
    assert "CLM-staleref00000" in message
    assert live.claim_id is not None and live.claim_id in message


# ------------------------------------------------------------------- ordering


def test_ordering_still_uses_edge_key_not_claim_id(config: CoreConfig) -> None:
    """Random ids cannot be an ordering token; edge_key stays the tiebreaker.

    Two applies of the same logical writes mint DIFFERENT ids, so an ordering
    keyed on the id would differ between a workflow preview and its apply while
    the apply digest stayed identical. Sorting by edge_key is replay-stable.
    """
    graph = _seeded_graph()
    graph.add_entity(EntityInstance(entity_type="Part", entity_id="P2", properties={}))
    first = _apply(graph, config, confidence=0.5)
    second_validated = validate_relationship(
        config, graph, "Part", "P2", "fits", "Vehicle", "V1", {}
    )
    second = apply_relationship(
        graph,
        second_validated,
        "batch_direct_write",
        "batch_direct_write",
        config=config,
    )
    assert first.edge_key is not None and second.edge_key is not None
    assert first.edge_key < second.edge_key

    expansion = graph.expand_neighborhood("Vehicle", "V1", depth=1)
    keys = [edge["edge_key"] for edge in expansion.edges]
    assert keys == sorted(keys)
