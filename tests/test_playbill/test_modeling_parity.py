"""PC-F semantic modeling parity: Claim-native meaning against the donor pin.

PC-F item 5 asks for parity fixtures from project-domain, agent-operation, and
one business domain, comparing query MEANING and results rather than old wire
receipts. This module is the surviving half of that comparison.

The pinned oracle at ``tests/data/playbill_parity/modeling-parity-oracle-v1.json``
records, per case, what the legacy surface answered and what the Claim-native
surface answers over the same world. ``test_modeling_parity_donors.py`` re-derives
every ``donor_rows`` entry from the live legacy engine while the donor island
stands, so the pin is a recording rather than a guess. That module is deleted by
PC-F slice F8; this one is not, and it keeps asserting the Claim-native side
against the same pinned rows afterwards.

Three claims are made, and the suite distinguishes them:

* ``parity`` -- the two surfaces return identical rows in identical order.
* ``supersession`` -- they deliberately differ, and the divergence is typed.
* ``divergence`` -- they differ because of a donor feature with no Claim-native
  equivalent yet. The gap is named and pinned, never papered over.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.query.definitions import QueryDefinitionV1
from cruxible_core.playbill.query.engine import (
    CLAIM_CONFLICT,
    ClaimQueryResultV1,
    evaluate_claim_query,
)
from cruxible_core.playbill.query.grammar import (
    QueryComparisonOperatorV1,
    QueryFilterV1,
    QueryTraversalStepV1,
    QueryValueTypeV1,
)
from tests.test_playbill import _modeling_parity_worlds as worlds
from tests.test_playbill._modeling_parity_support import (
    EVALUATION_TIME,
    accepted,
    coordinate,
    projected_rows,
)

ORACLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "playbill_parity"
    / "modeling-parity-oracle-v1.json"
)
ORACLE: dict[str, Any] = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))

PARITY_DOMAINS = {"agent-operation", "project-domain", "supply-chain-blast-radius"}


def case(case_id: str) -> dict[str, Any]:
    """Return one recorded parity case, or fail loudly if the pin lost it."""

    for entry in ORACLE["cases"]:
        if entry["case_id"] == case_id:
            return entry
    raise AssertionError(f"parity oracle has no case {case_id!r}")


def deferred_feature(name: str) -> dict[str, Any]:
    """Return one recorded deferred donor feature, or fail loudly."""

    for entry in ORACLE["deferred_donor_features"]:
        if entry["feature"] == name:
            return entry
    raise AssertionError(f"parity oracle has no deferred feature {name!r}")


def _evaluate(
    domain: str,
    query: QueryDefinitionV1,
    facts: Any,
    parameters: dict[str, object] | None = None,
    *,
    when: datetime = EVALUATION_TIME,
) -> ClaimQueryResultV1:
    return evaluate_claim_query(
        accepted(query),
        facts=facts,
        coordinate=coordinate(domain),
        evaluation_time=when,
        parameters=parameters,
    )


# -- the pin itself -------------------------------------------------------


def test_the_parity_oracle_is_well_formed_and_covers_the_three_named_domains() -> None:
    """PC-F names the domains the fixtures must come from; the pin must have them."""

    assert ORACLE["format"] == "playbill-modeling-parity-oracle-v1"
    assert datetime.fromisoformat(ORACLE["evaluation_time"].replace("Z", "+00:00")) == (
        EVALUATION_TIME
    )
    case_ids = [entry["case_id"] for entry in ORACLE["cases"]]
    assert len(case_ids) == len(set(case_ids))
    assert {entry["donor_domain"] for entry in ORACLE["cases"]} == PARITY_DOMAINS
    assert {entry["claim"] for entry in ORACLE["cases"]} == {
        "parity",
        "supersession",
        "divergence",
    }
    for entry in ORACLE["cases"]:
        assert entry["donor_rows"], entry["case_id"]
        assert entry["semantics"].strip()
        if entry["claim"] == "parity":
            assert "divergence" not in entry
        else:
            assert entry["divergence"]["kind"]
            assert entry["divergence"]["donor"].strip()
            assert entry["divergence"]["playbill"].strip()


def test_claimed_parity_cases_record_identical_donor_and_playbill_rows() -> None:
    """The tie between the two surfaces, asserted on the pin alone.

    This is the assertion that outlives F8: it needs neither engine, only the
    recording. A future edit that quietly relaxes a parity claim into a
    divergence has to change this file to do it.
    """

    parity = [entry for entry in ORACLE["cases"] if entry["claim"] == "parity"]
    assert len(parity) == 5
    for entry in parity:
        assert entry["playbill_rows"] == entry["donor_rows"], entry["case_id"]


def test_non_parity_cases_record_rows_that_genuinely_differ() -> None:
    """A typed divergence must actually diverge, or the claim is noise."""

    for entry in ORACLE["cases"]:
        if entry["claim"] == "parity":
            continue
        assert entry["playbill_rows"] != entry["donor_rows"], entry["case_id"]
        assert entry["divergence"]["deliberate"] == (entry["claim"] == "supersession")


# -- agent-operation ------------------------------------------------------


class TestAgentOperationParity:
    """The operating-layer domain: work queues and the note thread."""

    def test_work_queue_reproduces_the_donor_rows(self) -> None:
        entry = case("ao.work_queue")
        result = _evaluate(
            "agent-operation", worlds.work_queue_query(), worlds.agent_operation_facts()
        )
        assert result.verdict == "completed"
        assert projected_rows(result) == entry["playbill_rows"]

    def test_work_item_scratchpad_reproduces_the_donor_rows(self) -> None:
        entry = case("ao.work_item_scratchpad")
        result = _evaluate(
            "agent-operation",
            worlds.work_item_scratchpad_query(),
            worlds.agent_operation_facts(),
            {"work_item_id": "wi-1"},
        )
        assert projected_rows(result) == entry["playbill_rows"]

    def test_state_notes_for_work_item_reproduces_the_donor_rows(self) -> None:
        entry = case("ao.state_notes_for_work_item")
        result = _evaluate(
            "agent-operation",
            worlds.state_notes_for_work_item_query(),
            worlds.agent_operation_facts(),
            {"work_item_id": "wi-1"},
        )
        assert projected_rows(result) == entry["playbill_rows"]

    def test_the_curated_and_scratchpad_reads_partition_the_note_thread(self) -> None:
        """Meaning, not rows: the two filters are exact complements."""

        facts = worlds.agent_operation_facts()
        curated = _evaluate(
            "agent-operation",
            worlds.state_notes_for_work_item_query(),
            facts,
            {"work_item_id": "wi-1"},
        )
        scratchpad = _evaluate(
            "agent-operation",
            worlds.work_item_scratchpad_query(),
            facts,
            {"work_item_id": "wi-1"},
        )
        curated_ids = {row["id"] for row in projected_rows(curated)}
        scratchpad_ids = {row["id"] for row in projected_rows(scratchpad)}
        assert curated_ids.isdisjoint(scratchpad_ids)
        assert curated_ids | scratchpad_ids == {"sn-1", "sn-2", "sn-3", "sn-4"}


class TestAgentOperationSupersession:
    """The two behaviors PC-F deliberately does not reproduce."""

    def test_competing_single_valued_claims_refuse_rather_than_pick_a_winner(self) -> None:
        entry = case("ao.single_valued_conflict")
        result = _evaluate(
            "agent-operation",
            worlds.work_item_status_query(),
            worlds.agent_operation_facts(competing_status_on="wi-1"),
            {"work_item_id": "wi-1"},
        )
        assert result.verdict == "refused"
        assert result.refusal is not None
        assert result.refusal.code == CLAIM_CONFLICT == entry["playbill_refusal_code"]
        assert result.rows == ()

    def test_a_surfacing_policy_names_both_competing_statements(self) -> None:
        entry = case("ao.single_valued_conflict")
        result = _evaluate(
            "agent-operation",
            worlds.work_item_status_surfacing_query(),
            worlds.agent_operation_facts(competing_status_on="wi-1"),
            {"work_item_id": "wi-1"},
        )
        assert result.verdict == "completed"
        assert projected_rows(result) == entry["playbill_rows"]
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict.kind == "claim_object"
        assert conflict.predicate == worlds.WI_STATUS
        assert len(conflict.statement_digests) == 2
        assert conflict.subject_identity == "Subject:project.work_item/wi-1"

    def test_one_declaration_answers_differently_at_two_declared_instants(self) -> None:
        entry = case("ao.evaluation_time_axis")
        facts = worlds.agent_operation_expired_status_facts()
        earlier = datetime.fromisoformat(
            entry["playbill_earlier_evaluation_time"].replace("Z", "+00:00")
        )
        assert earlier < EVALUATION_TIME
        assert earlier.tzinfo == UTC

        before = _evaluate("agent-operation", worlds.work_queue_query(), facts, when=earlier)
        after = _evaluate("agent-operation", worlds.work_queue_query(), facts)
        assert projected_rows(before) == entry["playbill_rows_before_expiry"]
        assert projected_rows(after) == entry["playbill_rows"]
        assert before.evaluated_at == earlier
        assert after.evaluated_at == EVALUATION_TIME
        # Nothing was rewritten between the two reads.
        assert before.definition_digest == after.definition_digest
        assert before.coordinate == after.coordinate


# -- project-domain -------------------------------------------------------


class TestProjectDomainParity:
    """The project/product domain: areas and the work that targets them."""

    def test_work_items_for_area_reproduces_the_donor_rows(self) -> None:
        entry = case("pd.work_items_for_area")
        result = _evaluate(
            "project-domain",
            worlds.work_items_for_area_query(),
            worlds.project_domain_facts(),
            {"area_id": "pa-core"},
        )
        assert projected_rows(result) == entry["playbill_rows"]

    def test_an_area_with_no_attached_work_returns_nothing_rather_than_everything(self) -> None:
        """Meaning, not rows: the traversal is a real join, not a type scan."""

        result = _evaluate(
            "project-domain",
            worlds.work_items_for_area_query(),
            worlds.project_domain_facts(),
            {"area_id": "pa-ui"},
        )
        assert result.verdict == "completed"
        assert projected_rows(result) == []


# -- supply-chain blast radius (the business domain) ----------------------


class TestSupplyChainParity:
    """The business domain: incidents and the response work attached to them."""

    def test_incident_work_items_reproduces_the_donor_rows(self) -> None:
        entry = case("sc.incident_work_items")
        result = _evaluate(
            "supply-chain",
            worlds.incident_work_items_query(),
            worlds.supply_chain_facts(),
            {"incident_id": "inc-1"},
        )
        assert projected_rows(result) == entry["playbill_rows"]

    def test_open_incident_ordering_diverges_exactly_as_recorded(self) -> None:
        """Same incidents, different sequence -- the enum-ordinal gap, pinned.

        The donor ordered by the declared ``incident_severity`` ordinal. Nothing
        in ``QueryValueTypeV1`` can say that, so the nearest declarable key is
        the severity string, and lexicographic order puts ``medium`` above
        ``critical``. The suite asserts the divergence rather than hiding it
        behind a re-sorted expectation.
        """

        entry = case("sc.open_incidents_by_severity")
        result = _evaluate(
            "supply-chain",
            worlds.open_incidents_by_severity_query(),
            worlds.supply_chain_facts(),
        )
        rows = projected_rows(result)
        assert rows == entry["playbill_rows"]
        assert rows != entry["donor_rows"]
        assert {row["incident_id"] for row in rows} == {
            row["incident_id"] for row in entry["donor_rows"]
        }
        assert entry["divergence"]["same_result_set"] is True
        assert entry["divergence"]["deferred_feature"] == "enum_ordinal_ordering"


# -- deferred donor features: what the Claim-native grammar can say -------


class TestDeferredDonorFeatureCoverage:
    """The Claim-native half of the deferred-feature evidence.

    Each test states exactly what the grammar can and cannot declare today. The
    donor half -- what the legacy surface produced -- lives in the donor module
    and is pinned in the oracle. Nothing here ports a feature; PC-F stops at the
    evidence and the maintainer decides.
    """

    def test_every_deferred_feature_carries_a_decidable_recommendation(self) -> None:
        features = ORACLE["deferred_donor_features"]
        assert len(features) == 7
        names = [entry["feature"] for entry in features]
        assert len(names) == len(set(names))
        for entry in features:
            assert entry["recommendation"] in {
                "keep-by-porting",
                "drop-with-evidence",
                "defer-to-PC-G",
            }
            assert entry["playbill_status"] in {
                "already expressible",
                "partially expressible",
                "not expressible",
                "not a query concept",
            }
            assert entry["expresses"].strip()
            assert entry["loss_if_dropped"].strip()
            assert entry["donor_symbol"].startswith("cruxible_core.")

    def test_the_semi_join_half_of_where_related_is_already_expressible(self) -> None:
        feature = deferred_feature("where_related")
        result = _evaluate(
            "agent-operation",
            worlds.notes_on_open_review_query(),
            worlds.agent_operation_facts(),
            {"work_item_id": "wi-1", "review_status": "requested"},
        )
        assert projected_rows(result) == feature["donor_semi_join_rows"]

    def test_the_bare_anti_join_half_is_a_negated_claim_presence_filter(self) -> None:
        feature = deferred_feature("where_related")
        result = _evaluate(
            "agent-operation",
            worlds.notes_without_review_query(),
            worlds.agent_operation_facts(),
            {"work_item_id": "wi-1"},
        )
        assert projected_rows(result) == feature["donor_anti_join_rows"]

    def test_no_traversal_step_can_be_negated_or_carry_a_related_predicate(self) -> None:
        """The residual ``where_related`` gap, stated against the grammar."""

        assert deferred_feature("where_related")["playbill_status"] == "partially expressible"
        fields = set(QueryTraversalStepV1.model_fields)
        assert fields == {
            "tag",
            "binding",
            "from_binding",
            "predicate",
            "direction",
            "required",
            "target_subject_kinds",
            "where",
        }
        assert "negated" not in fields
        assert "where_related" not in fields
        assert "where_not_related" not in fields

    def test_the_filter_union_is_closed_and_carries_no_related_or_aggregate_form(self) -> None:
        kinds = {
            member.model_fields["kind"].default for member in QueryFilterV1.__origin__.__args__
        }
        assert kinds == {"comparison", "membership", "claim_presence", "all_of", "any_of", "not"}
        for absent in ("related", "not_related", "count", "aggregate", "exists_related"):
            assert absent not in kinds

    def test_no_ordering_can_name_an_enum_ordinal(self) -> None:
        """The ``enum_ordinal_ordering`` gap: there is no enum value type."""

        feature = deferred_feature("enum_ordinal_ordering")
        assert feature["playbill_status"] == "not expressible"
        assert feature["recommendation"] == "keep-by-porting"
        assert set(QueryValueTypeV1.__args__) == {
            "string",
            "integer",
            "boolean",
            "decimal",
            "timestamp",
            "subject_reference",
        }

    def test_no_comparison_operator_matches_a_substring(self) -> None:
        """The ``substring_operators`` gap: the operator set is ordered-only."""

        feature = deferred_feature("substring_operators")
        assert feature["playbill_status"] == "not expressible"
        assert set(QueryComparisonOperatorV1.__args__) == {"eq", "ne", "gt", "gte", "lt", "lte"}
        for absent in ("contains", "icontains", "matches", "startswith"):
            assert absent not in set(QueryComparisonOperatorV1.__args__)

    def test_includes_bound_their_items_and_report_no_cardinality(self) -> None:
        """The ``select_counts`` gap: a bounded item list is not a count."""

        feature = deferred_feature("select_counts")
        assert feature["playbill_status"] == "not expressible"
        from cruxible_core.playbill.query.engine import QueryIncludeResultV1

        fields = set(QueryIncludeResultV1.model_fields)
        assert {"items", "truncated"} <= fields
        for absent in ("count", "total", "total_matches", "cardinality"):
            assert absent not in fields

    def test_a_traversal_step_declares_one_predicate_and_one_hop(self) -> None:
        """The ``variable_depth_union_traversal`` gap, stated against the grammar."""

        feature = deferred_feature("variable_depth_union_traversal")
        assert feature["playbill_status"] == "not expressible"
        assert feature["recommendation"] == "keep-by-porting"
        annotation = QueryTraversalStepV1.model_fields["predicate"].annotation
        assert annotation is str
        assert "max_depth" not in QueryTraversalStepV1.model_fields
        with pytest.raises(ValidationError):
            QueryTraversalStepV1(
                binding="work",
                from_binding="area",
                predicate=[worlds.WI_TARGETS_AREA, worlds.SN_ABOUT_WORK_ITEM],  # type: ignore[arg-type]
                direction="reverse",
            )

    def test_a_step_constraint_is_already_a_typed_parameterized_comparison(self) -> None:
        """``constraint_dsl_step``: ported, and stricter than the string form."""

        feature = deferred_feature("constraint_dsl_step")
        assert feature["playbill_status"] == "already expressible"
        assert feature["recommendation"] == "drop-with-evidence"
        assert feature["playbill_query"] == "parity.agent_operation.notes_of_kind"
        facts = worlds.agent_operation_facts()
        result = _evaluate(
            "agent-operation",
            worlds.notes_of_kind_query(),
            facts,
            {"work_item_id": "wi-1", "kind": "scratchpad"},
        )
        assert projected_rows(result) == feature["donor_rows"]
        # The same declaration under a different bound parameter selects a
        # different set: the comparison is against the parameter, never against
        # a literal baked into the accepted declaration.
        curated = _evaluate(
            "agent-operation",
            worlds.notes_of_kind_query(),
            facts,
            {"work_item_id": "wi-1", "kind": "review_note"},
        )
        assert projected_rows(curated) == [{"id": "sn-2"}]

    def test_a_query_definition_declares_no_standing_invariant_or_severity(self) -> None:
        """``constraint_dsl_graph``: a canonical read is not an invariant."""

        feature = deferred_feature("constraint_dsl_graph")
        assert feature["playbill_status"] == "not a query concept"
        fields = set(QueryDefinitionV1.model_fields)
        for absent in ("constraints", "severity", "invariants", "rules"):
            assert absent not in fields
