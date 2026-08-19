"""PC-F donor half of the semantic modeling-parity oracle.

DONOR-DEPENDENT MODULE -- PC-F slice F8 DELETES THIS FILE WHOLE.

Every test here runs the LEGACY query surface (``cruxible_core.query``,
``cruxible_core.config``, ``cruxible_core.graph``, ``cruxible_core.predicate``)
over the three donor domains and asserts that what it produces is exactly the
``donor_rows`` recorded in the pinned oracle
``tests/data/playbill_parity/modeling-parity-oracle-v1.json``.

That is the whole point of the split. While the donor island stands, this module
is what makes the pin honest: the recorded legacy expectations are not asserted
copies of the Claim-native answer, they are re-derived from the legacy engine on
every run. When F8 purges the donors, this module goes with them and
``test_modeling_parity.py`` keeps asserting the Claim-native surface against the
same pinned rows -- the oracle survives its oracle.

The evidence half is here too: each deferred donor feature is EXERCISED against
a real donor world so the maintainer decision recorded in the oracle's
``deferred_donor_features`` block rests on observed behavior, not on a reading
of the schema.
"""

from __future__ import annotations

import inspect
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.constraint_rules import parse_constraint_rule
from cruxible_core.config.schema import (
    ConstraintSchema,
    NamedQuerySchema,
    QueryPredicateSpec,
    TraversalStep,
)
from cruxible_core.query.engine import execute_query, execute_query_definition
from cruxible_core.query.evaluate import evaluate_graph
from cruxible_core.query.types import dump_query_row
from cruxible_core.service.mutations import service_batch_direct_write
from cruxible_core.service.types import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
)
from tests.test_playbill.test_modeling_parity import ORACLE, case, deferred_feature

DONOR_CONFIGS = Path(__file__).resolve().parents[1] / "data" / "config_donors"


def _instant(day: int, hour: int) -> datetime:
    return datetime(2026, 8, day, hour, 0, tzinfo=UTC)


def _instance(domain: str, tmp_path: Path) -> CruxibleInstance:
    shutil.copy(DONOR_CONFIGS / domain / "config.yaml", tmp_path / "config.yaml")
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _rows(result: Any) -> list[dict[str, Any]]:
    """Return one comparable row list from a legacy query result.

    Projected queries compare on their selected values; unprojected queries
    compare on the identity of the result entity. Neither carries a receipt, a
    path, or any other wire detail -- PC-F compares query MEANING.
    """

    rows: list[dict[str, Any]] = []
    for row in result.results:
        data = dump_query_row(row)
        if "values" in data:
            rows.append(dict(data["values"]))
        elif "result" in data:
            rows.append({"id": data["result"]["entity_id"]})
        else:
            rows.append({"id": data["entity_id"]})
    return rows


# -- donor worlds ---------------------------------------------------------


def _work_item(identifier: str, title: str, summary: str, kind: str, status: str, priority: str):
    return EntityWriteInput(
        entity_type="WorkItem",
        entity_id=identifier,
        properties={
            "work_item_id": identifier,
            "title": title,
            "summary": summary,
            "type": kind,
            "status": status,
            "priority": priority,
        },
    )


@pytest.fixture
def agent_operation(tmp_path: Path) -> CruxibleInstance:
    """The agent-operation donor world the parity cases read."""

    instance = _instance("agent-operation", tmp_path)
    notes = {
        "sn-1": ("implementation_note", "engine shape", _instant(10, 9)),
        "sn-2": ("review_note", "review verdict", _instant(11, 9)),
        "sn-3": ("scratchpad", "pad one", _instant(10, 12)),
        "sn-4": ("scratchpad", "pad two", _instant(12, 12)),
    }
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                _work_item(
                    "wi-1",
                    "Land the Claim-native query engine",
                    "PC-F slice work",
                    "feature",
                    "active",
                    "high",
                ),
                _work_item(
                    "wi-2",
                    "Fix the ordering tiebreak",
                    "blocked on review",
                    "bug",
                    "blocked",
                    "critical",
                ),
                _work_item(
                    "wi-3", "Retire the donor island", "purge prep", "cleanup", "active", "medium"
                ),
                _work_item(
                    "wi-4", "Archive the old kits", "deferred for now", "docs", "deferred", "low"
                ),
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-1",
                    properties={
                        "review_request_id": "rr-1",
                        "title": "Review PC-F",
                        "status": "requested",
                    },
                ),
                *(
                    EntityWriteInput(
                        entity_type="StateNote",
                        entity_id=note,
                        properties={
                            "note_id": note,
                            "kind": kind,
                            "title": title,
                            "summary": note,
                            "created_at": created_at,
                        },
                    )
                    for note, (kind, title, created_at) in notes.items()
                ),
            ],
            relationships=[
                *(
                    BatchRelationshipWriteInput(
                        from_type="StateNote",
                        from_id=note,
                        relationship_type="state_note_about_work_item",
                        to_type="WorkItem",
                        to_id="wi-1",
                    )
                    for note in notes
                ),
                BatchRelationshipWriteInput(
                    from_type="StateNote",
                    from_id="sn-2",
                    relationship_type="state_note_about_review_request",
                    to_type="ReviewRequest",
                    to_id="rr-1",
                ),
            ],
        ),
    )
    return instance


@pytest.fixture
def project_domain(tmp_path: Path) -> CruxibleInstance:
    """The project-domain donor world the parity cases read."""

    instance = _instance("project-domain", tmp_path)
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="ProductArea",
                    entity_id="pa-core",
                    properties={"area_id": "pa-core", "name": "Core runtime"},
                ),
                EntityWriteInput(
                    entity_type="ProductArea",
                    entity_id="pa-ui",
                    properties={"area_id": "pa-ui", "name": "Inspection UI"},
                ),
                _work_item(
                    "wi-a", "Port the traversal semantics", "a", "feature", "active", "high"
                ),
                _work_item("wi-b", "Delete the overlay authority", "b", "cleanup", "closed", "low"),
                _work_item("wi-c", "Unattached work", "c", "research", "active", "medium"),
            ],
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="WorkItem",
                    from_id=item,
                    relationship_type="work_item_targets_area",
                    to_type="ProductArea",
                    to_id="pa-core",
                )
                for item in ("wi-a", "wi-b")
            ],
        ),
    )
    return instance


@pytest.fixture
def supply_chain(tmp_path: Path) -> CruxibleInstance:
    """The supply-chain blast-radius donor world the parity cases read."""

    instance = _instance("supply-chain-blast-radius", tmp_path)
    incidents = {
        "inc-1": ("Fab fire at tier-2 supplier", "critical", "supplier", "open", "2026-08-10"),
        "inc-2": ("Port congestion", "medium", "geography", "open", "2026-08-11"),
        "inc-3": ("Resolved customs hold", "high", "geography", "closed", "2026-08-01"),
    }
    work = {
        "wi-s1": ("Qualify alternate supplier", "active", "critical"),
        "wi-s2": ("Old mitigation", "closed", "low"),
        "wi-s3": ("Expedite inventory", "blocked", "high"),
    }
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                *(
                    EntityWriteInput(
                        entity_type="Incident",
                        entity_id=identifier,
                        properties={
                            "incident_id": identifier,
                            "title": title,
                            "severity": severity,
                            "scope_type": scope_type,
                            "status": status,
                            "reported_at": reported_at,
                        },
                    )
                    for identifier, (
                        title,
                        severity,
                        scope_type,
                        status,
                        reported_at,
                    ) in incidents.items()
                ),
                EntityWriteInput(
                    entity_type="Supplier",
                    entity_id="sup-1",
                    properties={
                        "supplier_id": "sup-1",
                        "name": "Northwind Components",
                        "primary_geography": "TW",
                    },
                ),
                *(
                    _work_item(identifier, title, identifier, "operations", status, priority)
                    for identifier, (title, status, priority) in work.items()
                ),
            ],
            relationships=[
                *(
                    BatchRelationshipWriteInput(
                        from_type="WorkItem",
                        from_id=identifier,
                        relationship_type="work_item_addresses_incident",
                        to_type="Incident",
                        to_id="inc-1",
                    )
                    for identifier in work
                ),
                BatchRelationshipWriteInput(
                    from_type="Incident",
                    from_id="inc-1",
                    relationship_type="incident_impacts_supplier",
                    to_type="Supplier",
                    to_id="sup-1",
                    properties={"match_basis": "direct", "rationale": "single-source part"},
                ),
            ],
        ),
    )
    return instance


def _run(instance: CruxibleInstance, name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return _rows(execute_query(instance.load_config(), instance.load_graph(), name, params))


# -- the donor half of every recorded parity case -------------------------


class TestDonorRowsReproduceThePin:
    """Each recorded ``donor_rows`` is re-derived from the legacy engine."""

    def test_agent_operation_work_queue(self, agent_operation: CruxibleInstance) -> None:
        entry = case("ao.work_queue")
        assert _run(agent_operation, entry["donor_query"], {}) == entry["donor_rows"]

    def test_agent_operation_work_item_scratchpad(self, agent_operation: CruxibleInstance) -> None:
        entry = case("ao.work_item_scratchpad")
        rows = _run(agent_operation, entry["donor_query"], {"work_item_id": "wi-1"})
        assert rows == entry["donor_rows"]

    def test_agent_operation_state_notes_for_work_item(
        self, agent_operation: CruxibleInstance
    ) -> None:
        entry = case("ao.state_notes_for_work_item")
        rows = _run(agent_operation, entry["donor_query"], {"work_item_id": "wi-1"})
        assert rows == entry["donor_rows"]

    def test_project_domain_work_items_for_area(self, project_domain: CruxibleInstance) -> None:
        entry = case("pd.work_items_for_area")
        rows = _run(project_domain, entry["donor_query"], {"area_id": "pa-core"})
        assert rows == entry["donor_rows"]

    def test_supply_chain_incident_work_items(self, supply_chain: CruxibleInstance) -> None:
        entry = case("sc.incident_work_items")
        rows = _run(supply_chain, entry["donor_query"], {"incident_id": "inc-1"})
        assert rows == entry["donor_rows"]

    def test_supply_chain_open_incidents_by_severity(self, supply_chain: CruxibleInstance) -> None:
        entry = case("sc.open_incidents_by_severity")
        assert _run(supply_chain, entry["donor_query"], {}) == entry["donor_rows"]


class TestDonorSupersededBehavior:
    """The two behaviors PC-F deliberately does NOT reproduce."""

    def test_a_second_status_write_silently_replaces_the_first(
        self, agent_operation: CruxibleInstance
    ) -> None:
        """The donor property store cannot hold two competing observations.

        This is the recorded ``ao.single_valued_conflict`` divergence: the second
        write wins, the first is unrecoverable from any read, and nothing in the
        result says a disagreement ever existed.
        """

        entry = case("ao.single_valued_conflict")
        service_batch_direct_write(
            agent_operation,
            BatchDirectWriteInput(
                entities=[
                    _work_item(
                        "wi-1",
                        "Land the Claim-native query engine",
                        "PC-F slice work",
                        "feature",
                        "blocked",
                        "high",
                    )
                ]
            ),
        )
        stored = agent_operation.load_graph().get_entity("WorkItem", "wi-1")
        assert stored is not None
        assert stored.properties["status"] == "blocked"
        assert _run(agent_operation, entry["donor_query"], {}) == entry["donor_rows"]

    def test_the_donor_read_has_no_evaluation_time_axis(
        self, agent_operation: CruxibleInstance
    ) -> None:
        """The recorded ``ao.evaluation_time_axis`` divergence, mechanically.

        A legacy read cannot be asked "as of when". There is no parameter to
        supply and no interval on a stored property to compare one against, so
        the recorded donor rows are the ONLY answer this world has.
        """

        entry = case("ao.evaluation_time_axis")
        parameters = set(inspect.signature(execute_query).parameters)
        assert "evaluation_time" not in parameters
        assert parameters == {
            "config",
            "graph",
            "query_name",
            "params",
            "relationship_state",
            "lifecycle_status",
        }
        assert "effective_from" not in NamedQuerySchema.model_fields
        assert "evaluation_time" not in NamedQuerySchema.model_fields
        assert _run(agent_operation, entry["donor_query"], {}) == entry["donor_rows"]


# -- deferred donor features: what the legacy surface really expresses -----


class TestDeferredDonorFeatureEvidence:
    """Each deferred feature, exercised on a real donor world.

    These are the observations behind the maintainer recommendation recorded in
    the oracle. Nothing here is ported; PC-F stops at the evidence.
    """

    @staticmethod
    def _note_step(**overrides: Any) -> TraversalStep:
        fields: dict[str, Any] = {
            "relationship": "state_note_about_work_item",
            "direction": "incoming",
            "as": "note",
        }
        fields.update(overrides)
        return TraversalStep(**fields)

    @staticmethod
    def _note_query(step: TraversalStep) -> NamedQuerySchema:
        return NamedQuerySchema(
            mode="traversal",
            entry_point="WorkItem",
            returns="StateNote",
            result_shape="entity",
            dedupe="entity",
            traversal=[step],
        )

    def _run_inline(
        self,
        instance: CruxibleInstance,
        name: str,
        schema: NamedQuerySchema,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return _rows(
            execute_query_definition(
                instance.load_config(), instance.load_graph(), name, schema, params
            )
        )

    def test_where_related_scopes_a_candidate_on_a_second_edge_it_never_returns(
        self, agent_operation: CruxibleInstance
    ) -> None:
        """The semi-join and anti-join forms, both on one real note thread.

        The related edge hangs off the CANDIDATE and its far endpoint is never
        bound into a row: the query returns notes while the condition they
        survive is a fact about a review request.
        """

        feature = deferred_feature("where_related")
        example = feature["donor_example"]
        assert example["relationship"] == "state_note_about_review_request"

        control = self._run_inline(
            agent_operation,
            "inline_control",
            self._note_query(self._note_step()),
            {"work_item_id": "wi-1"},
        )
        assert control == feature["donor_control_rows"]

        semi = self._run_inline(
            agent_operation,
            "inline_where_related",
            self._note_query(
                self._note_step(
                    where_related=[
                        {
                            "relationship": example["relationship"],
                            "direction": example["direction"],
                            "target": example["target"],
                        }
                    ]
                )
            ),
            {"work_item_id": "wi-1"},
        )
        assert semi == feature["donor_semi_join_rows"]

        anti = self._run_inline(
            agent_operation,
            "inline_where_not_related",
            self._note_query(
                self._note_step(
                    where_not_related=[
                        {
                            "relationship": example["relationship"],
                            "direction": example["direction"],
                        }
                    ]
                )
            ),
            {"work_item_id": "wi-1"},
        )
        assert anti == feature["donor_anti_join_rows"]
        assert [row["id"] for row in semi] + [row["id"] for row in anti] != []
        assert {row["id"] for row in semi}.isdisjoint({row["id"] for row in anti})
        assert {row["id"] for row in semi} | {row["id"] for row in anti} == {
            row["id"] for row in control
        }

    def test_a_step_constraint_compares_a_candidate_against_a_bound_parameter(
        self, agent_operation: CruxibleInstance
    ) -> None:
        """The step constraint DSL: ``target.<property> <op> $<param>``."""

        feature = deferred_feature("constraint_dsl_step")
        example = feature["donor_example"]
        assert example["constraint"] == "target.kind == $kind"
        rows = self._run_inline(
            agent_operation,
            "inline_constraint",
            self._note_query(
                self._note_step(
                    constraint=example["constraint"],
                    constraint_value_type=example["constraint_value_type"],
                )
            ),
            dict(example["params"]),
        )
        assert rows == feature["donor_rows"]

    def test_a_graph_constraint_is_a_standing_invariant_not_a_filter(
        self, project_domain: CruxibleInstance
    ) -> None:
        """The graph constraint DSL reports findings; it filters no row."""

        feature = deferred_feature("constraint_dsl_graph")
        rule = feature["donor_example"]["rule"]
        parsed = parse_constraint_rule(rule)
        assert parsed is not None
        assert parsed.relationship == "work_item_targets_area"
        assert (parsed.from_property, parsed.operator, parsed.to_property) == (
            "title",
            "==",
            "name",
        )

        config = project_domain.load_config()
        with_constraint = config.model_copy(
            update={
                "constraints": [
                    ConstraintSchema(
                        name="work_item_title_matches_area_name",
                        rule=rule,
                        severity=feature["donor_example"]["severity"],
                        description="Contrived invariant: exercised for PC-F evidence only.",
                    )
                ]
            }
        )
        report = evaluate_graph(with_constraint, project_domain.load_graph())
        violations = [
            finding for finding in report.findings if finding.category == "constraint_violation"
        ]
        assert len(violations) == feature["donor_violation_count"]
        assert {finding.severity for finding in violations} == {"warning"}
        # A constraint never removes a row: the same query answers identically.
        assert (
            _run(project_domain, "work_items_for_area", {"area_id": "pa-core"})
            == case("pd.work_items_for_area")["donor_rows"]
        )

    def test_select_counts_aggregate_edges_without_returning_them(
        self, agent_operation: CruxibleInstance
    ) -> None:
        """``counts:`` emits a cardinality, not a bounded item list."""

        feature = deferred_feature("select_counts")
        example = feature["donor_example"]
        result = execute_query(
            agent_operation.load_config(),
            agent_operation.load_graph(),
            example["query"],
            {},
        )
        counted = {}
        for row in result.results:
            values = dump_query_row(row)["values"]
            counted[values["note_id"]] = values[example["count_key"]]
        assert counted == feature["donor_counts"]

    def test_contains_matches_a_substring_no_comparison_operator_can(
        self, agent_operation: CruxibleInstance
    ) -> None:
        """``contains``/``icontains`` are substring tests, not orderings."""

        feature = deferred_feature("substring_operators")
        example = feature["donor_example"]
        schema = NamedQuerySchema(
            mode="collection",
            returns="WorkItem",
            result_shape="entity",
            dedupe="entity",
            where=QueryPredicateSpec({example["path"]: {example["operator"]: example["value"]}}),
            select={
                "work_item_id": "$result.entity_id",
                "title": "$result.properties.title",
            },
        )
        assert (
            self._run_inline(agent_operation, "inline_contains", schema, {})
            == (feature["donor_rows"])
        )

    def test_a_multi_relationship_step_walks_a_union_to_a_declared_depth(
        self, project_domain: CruxibleInstance
    ) -> None:
        """One donor step names FIVE relationships and repeats itself three hops."""

        feature = deferred_feature("variable_depth_union_traversal")
        example = feature["donor_example"]
        schema = project_domain.load_config().named_queries[example["query"]]
        assert isinstance(schema.traversal[0].relationship, list)
        assert len(schema.traversal[0].relationship) == example["relationship_count"]
        assert schema.traversal[0].max_depth == example["max_depth"]
        assert (
            _run(project_domain, example["query"], {"area_id": "pa-core"})
            == (feature["donor_rows"])
        )


def test_every_recorded_donor_case_names_a_query_the_donor_config_declares(
    tmp_path: Path,
) -> None:
    """No recorded case can drift onto a query its donor domain does not have."""

    for entry in ORACLE["cases"]:
        root = tmp_path / entry["case_id"]
        root.mkdir()
        instance = _instance(entry["donor_domain"], root)
        assert entry["donor_query"] in instance.load_config().named_queries


def test_every_recorded_case_has_a_live_donor_derivation_in_this_module() -> None:
    """While the donors stand, no pinned row may go un-re-derived.

    A new case added to the oracle without a donor-side test here would be a
    recording with nothing behind it. This is also F8's checklist: every case
    named below loses its live derivation when this module is deleted and keeps
    only the pin.
    """

    derived = {
        "ao.work_queue",
        "ao.work_item_scratchpad",
        "ao.state_notes_for_work_item",
        "pd.work_items_for_area",
        "sc.incident_work_items",
        "sc.open_incidents_by_severity",
        "ao.single_valued_conflict",
        "ao.evaluation_time_axis",
    }
    assert derived == {entry["case_id"] for entry in ORACLE["cases"]}
