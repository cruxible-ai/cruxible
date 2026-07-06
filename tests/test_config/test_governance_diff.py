"""Exhaustive tests for the governance-diff classifier.

Every weakening rule in dd-config-by-reference-one-source gets a test, plus
the fail-closed default (unclassifiable changes and unknown surfaces are
weakening, never neutral) and the mirror tightening cases.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from cruxible_core.config import governance_diff
from cruxible_core.config.governance_diff import GovernanceDiff, diff_governance
from cruxible_core.config.schema import CoreConfig, RuntimeConfigSchema

Mutator = Callable[[dict[str, Any]], None]

BASE_CONFIG: dict[str, Any] = {
    "name": "governance-diff-base",
    "entity_types": {
        "WorkItem": {
            "properties": {
                "work_item_id": {"type": "string", "primary_key": True},
                "status": {"type": "string"},
            },
        },
        "Actor": {
            "properties": {"actor_id": {"type": "string", "primary_key": True}},
        },
    },
    "relationships": [
        {
            "name": "work_item_owned_by_actor",
            "from": "WorkItem",
            "to": "Actor",
            "proposal_policy": {
                "signals": {
                    "ownership_signal": {
                        "role": "required",
                        "always_review_on_unsure": True,
                        "require_evidence_on_support": True,
                    },
                },
                "auto_resolve_when": "all_support",
                "auto_resolve_requires_prior_trust": "trusted_only",
                "max_group_size": 100,
            },
        },
        {"name": "work_item_blocked_by", "from": "WorkItem", "to": "WorkItem"},
    ],
    "mutation_guards": [
        {
            "name": "guarded_close",
            "entity_type": "WorkItem",
            "property": "status",
            "new_value": "closed",
            "condition": {"type": "actor", "allowed_actor_ids": ["reviewer"]},
            "message": "closes need the reviewer",
            "where_related": [
                {"relationship": "work_item_owned_by_actor", "direction": "outgoing"},
            ],
        },
    ],
    "quality_checks": [
        {
            "kind": "property",
            "name": "status_required",
            "severity": "error",
            "target": "entity",
            "entity_type": "WorkItem",
            "property": "status",
            "rule": "required",
        },
        {
            "kind": "property",
            "name": "status_advice",
            "severity": "warning",
            "target": "entity",
            "entity_type": "WorkItem",
            "property": "status",
            "rule": "non_empty",
        },
    ],
    "constraints": [
        {
            "name": "status_bounded",
            "rule": "WorkItem.status in ['open', 'closed']",
            "severity": "error",
        },
    ],
    "decision_policies": [
        {
            "name": "review_ownership",
            "applies_to": "workflow",
            "workflow_name": "wf",
            "relationship_type": "work_item_owned_by_actor",
            "effect": "require_review",
        },
        {
            "name": "suppress_noise",
            "applies_to": "query",
            "query_name": "q",
            "relationship_type": "work_item_owned_by_actor",
            "effect": "suppress",
        },
    ],
}


def build(mutate: Mutator | None = None) -> CoreConfig:
    data = copy.deepcopy(BASE_CONFIG)
    if mutate is not None:
        mutate(data)
    return CoreConfig.model_validate(data)


def classify(mutate: Mutator) -> GovernanceDiff:
    return diff_governance(build(), build(mutate))


def weakening_lines(diff: GovernanceDiff) -> list[str]:
    return [c.summary for c in diff.changes if c.direction == "weakening"]


def tightening_lines(diff: GovernanceDiff) -> list[str]:
    return [c.summary for c in diff.changes if c.direction == "tightening"]


# ---------------------------------------------------------------------------
# Baseline / neutral surfaces
# ---------------------------------------------------------------------------


def test_identical_configs_are_neutral() -> None:
    diff = diff_governance(build(), build())
    assert diff.classification == "neutral"
    assert diff.changes == ()
    assert diff.summary_lines == []


def test_neutral_surfaces_do_not_move_the_classification() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["description"] = "totally new description"
        data["entity_types"]["WorkItem"]["description"] = "annotated"
        # Schema additions are neutral.
        data["entity_types"]["NewThing"] = {
            "properties": {"thing_id": {"type": "string", "primary_key": True}},
        }
        data["relationships"].append(
            {"name": "work_item_mentions_thing", "from": "WorkItem", "to": "NewThing"}
        )
        data["entity_types"]["WorkItem"]["properties"]["new_field"] = {"type": "string"}
        data["enums"] = {"Status": {"values": ["open", "closed"]}}
        data["named_queries"] = {
            "open_items": {
                "mode": "collection",
                "returns": "WorkItem",
                "result_shape": "entity",
            }
        }

    assert classify(mutate).classification == "neutral"


def test_warning_severity_check_churn_is_neutral() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["quality_checks"] = [
            check for check in data["quality_checks"] if check["name"] != "status_advice"
        ]

    assert classify(mutate).classification == "neutral"


# ---------------------------------------------------------------------------
# Write policies (effective, per type)
# ---------------------------------------------------------------------------


def test_entity_write_policy_removed_is_weakening() -> None:
    old = build(lambda d: d["entity_types"]["WorkItem"].update(write_policy="proposal_only"))
    diff = diff_governance(old, build())
    assert diff.classification == "weakened"
    assert any("WorkItem" in line for line in weakening_lines(diff))


def test_entity_write_policy_added_is_tightening() -> None:
    new = build(lambda d: d["entity_types"]["WorkItem"].update(write_policy="proposal_only"))
    diff = diff_governance(build(), new)
    assert diff.classification == "tightened"


def test_entity_write_policy_downgraded_from_mint_only_is_weakening() -> None:
    old = build(lambda d: d["entity_types"]["Actor"].update(write_policy="mint_only"))
    new = build(lambda d: d["entity_types"]["Actor"].update(write_policy="proposal_only"))
    diff = diff_governance(old, new)
    assert diff.classification == "weakened"


def test_entity_write_policy_upgraded_to_mint_only_is_tightening() -> None:
    old = build(lambda d: d["entity_types"]["Actor"].update(write_policy="proposal_only"))
    new = build(lambda d: d["entity_types"]["Actor"].update(write_policy="mint_only"))
    assert diff_governance(old, new).classification == "tightened"


def test_relationship_write_policy_downgraded_is_weakening() -> None:
    old = build(lambda d: d["relationships"][1].update(write_policy="proposal_only"))
    diff = diff_governance(old, build())
    assert diff.classification == "weakened"
    assert any("work_item_blocked_by" in line for line in weakening_lines(diff))


def test_effective_policy_is_compared_not_raw_fields() -> None:
    # Raw removal of the per-type field is NOT weakening when the new default
    # keeps the effective policy identical.
    def explicit_direct(data: dict[str, Any]) -> None:
        data["entity_types"]["Actor"]["write_policy"] = "direct"
        for rel in data["relationships"]:
            rel["write_policy"] = "direct"

    def old_mutate(data: dict[str, Any]) -> None:
        explicit_direct(data)
        data["entity_types"]["WorkItem"]["write_policy"] = "proposal_only"

    def new_mutate(data: dict[str, Any]) -> None:
        explicit_direct(data)
        data["runtime"] = {"default_write_policy": "proposal_only"}

    diff = diff_governance(build(old_mutate), build(new_mutate))
    # The default upgrade itself is a tightening, but nothing about WorkItem
    # may register as weakening: its EFFECTIVE policy is unchanged.
    assert diff.classification == "tightened"
    assert not weakening_lines(diff)


def test_effective_downgrade_via_explicit_direct_opt_out_is_weakening() -> None:
    old = build(lambda d: d.update(runtime={"default_write_policy": "proposal_only"}))

    def new_mutate(data: dict[str, Any]) -> None:
        data["runtime"] = {"default_write_policy": "proposal_only"}
        data["entity_types"]["WorkItem"]["write_policy"] = "direct"

    diff = diff_governance(old, build(new_mutate))
    assert diff.classification == "weakened"


def test_default_write_policy_downgraded_is_weakening() -> None:
    old = build(lambda d: d.update(runtime={"default_write_policy": "proposal_only"}))
    diff = diff_governance(old, build())
    assert diff.classification == "weakened"
    assert any("default_write_policy" in line for line in weakening_lines(diff))


def test_default_write_policy_upgraded_is_tightening() -> None:
    new = build(lambda d: d.update(runtime={"default_write_policy": "proposal_only"}))
    assert diff_governance(build(), new).classification == "tightened"


def test_entity_type_removed_is_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        del data["entity_types"]["Actor"]
        data["relationships"] = [
            rel for rel in data["relationships"] if rel["name"] != "work_item_owned_by_actor"
        ]
        data["decision_policies"] = []
        # The guard scopes through the removed relationship; drop it too so the
        # config stays valid (its removal is a second weakening finding).
        data["mutation_guards"] = []

    diff = classify(mutate)
    assert diff.classification == "weakened"
    assert any("entity type removed" in line for line in weakening_lines(diff))


def test_relationship_removed_is_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["relationships"] = [
            rel for rel in data["relationships"] if rel["name"] != "work_item_blocked_by"
        ]

    diff = classify(mutate)
    assert diff.classification == "weakened"


# ---------------------------------------------------------------------------
# Mutation guards
# ---------------------------------------------------------------------------


def test_mutation_guard_removed_is_weakening() -> None:
    diff = classify(lambda d: d.update(mutation_guards=[]))
    assert diff.classification == "weakened"
    assert any("guarded_close" in line for line in weakening_lines(diff))


def test_mutation_guard_added_is_tightening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["mutation_guards"].append(
            {
                "name": "second_guard",
                "entity_type": "WorkItem",
                "property": "status",
                "new_value": "open",
                "condition": {"type": "actor", "allowed_actor_ids": ["reviewer"]},
            }
        )

    assert classify(mutate).classification == "tightened"


def test_mutation_guard_scope_narrowed_by_where_is_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["mutation_guards"][0]["where"] = {"candidate.priority": {"eq": "high"}}

    diff = classify(mutate)
    assert diff.classification == "weakened"
    assert any("scope narrowed" in line for line in weakening_lines(diff))


def test_mutation_guard_scope_narrowed_by_where_related_is_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["mutation_guards"][0]["where_related"].append(
            {"relationship": "work_item_blocked_by", "direction": "outgoing"}
        )

    assert classify(mutate).classification == "weakened"


def test_mutation_guard_scope_broadened_is_tightening() -> None:
    diff = classify(lambda d: d["mutation_guards"][0].update(where_related=[]))
    assert diff.classification == "tightened"
    assert any("scope broadened" in line for line in tightening_lines(diff))


def test_mutation_guard_condition_change_fails_closed_to_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["mutation_guards"][0]["condition"] = {
            "type": "actor",
            "allowed_actor_ids": ["reviewer", "someone-else"],
        }

    diff = classify(mutate)
    assert diff.classification == "weakened"
    assert any("cannot be verified as tightening" in line for line in weakening_lines(diff))


def test_mutation_guard_mixed_scope_change_fails_closed_to_weakening() -> None:
    # One scope dimension broadens while another narrows: not positively
    # classifiable, so it must be weakening.
    def mutate(data: dict[str, Any]) -> None:
        data["mutation_guards"][0]["where_related"] = []
        data["mutation_guards"][0]["where"] = {"candidate.priority": {"eq": "high"}}

    assert classify(mutate).classification == "weakened"


def test_mutation_guard_message_edit_is_neutral() -> None:
    diff = classify(lambda d: d["mutation_guards"][0].update(message="new wording"))
    assert diff.classification == "neutral"


# ---------------------------------------------------------------------------
# Proposal policies
# ---------------------------------------------------------------------------


def _proposal_policy(data: dict[str, Any]) -> dict[str, Any]:
    return data["relationships"][0]["proposal_policy"]


def test_proposal_policy_removed_is_weakening() -> None:
    diff = classify(lambda d: d["relationships"][0].pop("proposal_policy"))
    assert diff.classification == "weakened"


def test_proposal_policy_added_is_tightening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["relationships"][1]["proposal_policy"] = {
            "signals": {"blocking_signal": {"role": "blocking"}},
        }

    assert classify(mutate).classification == "tightened"


def test_proposal_signal_removed_is_weakening() -> None:
    diff = classify(lambda d: _proposal_policy(d)["signals"].pop("ownership_signal"))
    assert diff.classification == "weakened"
    assert any("signal removed" in line for line in weakening_lines(diff))


def test_proposal_signal_added_is_tightening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        _proposal_policy(data)["signals"]["extra_signal"] = {"role": "advisory"}

    assert classify(mutate).classification == "tightened"


def test_signal_role_downgraded_is_weakening() -> None:
    diff = classify(
        lambda d: _proposal_policy(d)["signals"]["ownership_signal"].update(role="advisory")
    )
    assert diff.classification == "weakened"


def test_signal_role_downgraded_from_blocking_is_weakening() -> None:
    old = build(
        lambda d: _proposal_policy(d)["signals"]["ownership_signal"].update(role="blocking")
    )
    assert diff_governance(old, build()).classification == "weakened"


def test_signal_role_upgraded_is_tightening() -> None:
    diff = classify(
        lambda d: _proposal_policy(d)["signals"]["ownership_signal"].update(role="blocking")
    )
    assert diff.classification == "tightened"


def test_always_review_on_unsure_dropped_is_weakening() -> None:
    diff = classify(
        lambda d: _proposal_policy(d)["signals"]["ownership_signal"].update(
            always_review_on_unsure=False
        )
    )
    assert diff.classification == "weakened"


def test_always_review_on_unsure_added_is_tightening() -> None:
    old = build(
        lambda d: _proposal_policy(d)["signals"]["ownership_signal"].update(
            always_review_on_unsure=False
        )
    )
    assert diff_governance(old, build()).classification == "tightened"


def test_require_evidence_on_support_dropped_is_weakening() -> None:
    diff = classify(
        lambda d: _proposal_policy(d)["signals"]["ownership_signal"].update(
            require_evidence_on_support=False
        )
    )
    assert diff.classification == "weakened"


def test_auto_resolve_when_broadened_is_weakening() -> None:
    diff = classify(lambda d: _proposal_policy(d).update(auto_resolve_when="no_contradict"))
    assert diff.classification == "weakened"
    assert any("auto_resolve_when" in line for line in weakening_lines(diff))


def test_auto_resolve_when_narrowed_is_tightening() -> None:
    old = build(lambda d: _proposal_policy(d).update(auto_resolve_when="no_contradict"))
    assert diff_governance(old, build()).classification == "tightened"


def test_prior_trust_requirement_dropped_is_weakening() -> None:
    diff = classify(
        lambda d: _proposal_policy(d).update(auto_resolve_requires_prior_trust="trusted_or_watch")
    )
    assert diff.classification == "weakened"


def test_prior_trust_requirement_raised_is_tightening() -> None:
    old = build(
        lambda d: _proposal_policy(d).update(auto_resolve_requires_prior_trust="trusted_or_watch")
    )
    assert diff_governance(old, build()).classification == "tightened"


def test_max_group_size_raised_is_weakening() -> None:
    diff = classify(lambda d: _proposal_policy(d).update(max_group_size=5000))
    assert diff.classification == "weakened"


def test_max_group_size_lowered_is_tightening() -> None:
    diff = classify(lambda d: _proposal_policy(d).update(max_group_size=10))
    assert diff.classification == "tightened"


def test_proposal_identity_change_fails_closed_to_weakening() -> None:
    diff = classify(lambda d: d["relationships"][0].update(proposal_identity="relationship_tuple"))
    assert diff.classification == "weakened"


# ---------------------------------------------------------------------------
# Quality checks and constraints (error floor)
# ---------------------------------------------------------------------------


def test_error_quality_check_removed_is_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["quality_checks"] = [
            check for check in data["quality_checks"] if check["name"] != "status_required"
        ]

    diff = classify(mutate)
    assert diff.classification == "weakened"
    assert any("status_required" in line for line in weakening_lines(diff))


def test_error_quality_check_demoted_is_weakening() -> None:
    diff = classify(lambda d: d["quality_checks"][0].update(severity="warning"))
    assert diff.classification == "weakened"


def test_error_quality_check_added_is_tightening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["quality_checks"].append(
            {
                "kind": "property",
                "name": "actor_id_required",
                "severity": "error",
                "target": "entity",
                "entity_type": "Actor",
                "property": "actor_id",
                "rule": "required",
            }
        )

    assert classify(mutate).classification == "tightened"


def test_quality_check_promoted_to_error_is_tightening() -> None:
    diff = classify(lambda d: d["quality_checks"][1].update(severity="error"))
    assert diff.classification == "tightened"


def test_error_quality_check_content_change_fails_closed_to_weakening() -> None:
    diff = classify(lambda d: d["quality_checks"][0].update(rule="non_empty"))
    assert diff.classification == "weakened"


def test_error_constraint_removed_is_weakening() -> None:
    diff = classify(lambda d: d.update(constraints=[]))
    assert diff.classification == "weakened"
    assert any("status_bounded" in line for line in weakening_lines(diff))


def test_error_constraint_demoted_is_weakening() -> None:
    diff = classify(lambda d: d["constraints"][0].update(severity="warning"))
    assert diff.classification == "weakened"


def test_entity_constraint_reference_removed_is_weakening() -> None:
    old = build(lambda d: d["entity_types"]["WorkItem"].update(constraints=["status_bounded"]))
    diff = diff_governance(old, build())
    assert diff.classification == "weakened"


# ---------------------------------------------------------------------------
# Decision policies
# ---------------------------------------------------------------------------


def test_require_review_policy_removed_is_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["decision_policies"] = [
            policy for policy in data["decision_policies"] if policy["effect"] != "require_review"
        ]

    assert classify(mutate).classification == "weakened"


def test_require_review_policy_added_is_tightening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["decision_policies"].append(
            {
                "name": "review_blockers",
                "applies_to": "workflow",
                "workflow_name": "wf2",
                "relationship_type": "work_item_blocked_by",
                "effect": "require_review",
            }
        )

    assert classify(mutate).classification == "tightened"


def test_suppress_policy_added_fails_closed_to_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["decision_policies"].append(
            {
                "name": "suppress_more",
                "applies_to": "query",
                "query_name": "q2",
                "relationship_type": "work_item_blocked_by",
                "effect": "suppress",
            }
        )

    assert classify(mutate).classification == "weakened"


def test_suppress_policy_removed_is_tightening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["decision_policies"] = [
            policy for policy in data["decision_policies"] if policy["effect"] != "suppress"
        ]

    assert classify(mutate).classification == "tightened"


def test_decision_policy_rationale_edit_is_neutral() -> None:
    diff = classify(lambda d: d["decision_policies"][0].update(rationale="because"))
    assert diff.classification == "neutral"


# ---------------------------------------------------------------------------
# Fail-closed defaults
# ---------------------------------------------------------------------------


def test_property_removed_fails_closed_to_weakening() -> None:
    diff = classify(lambda d: d["entity_types"]["WorkItem"]["properties"].pop("status"))
    assert diff.classification == "weakened"


def test_property_definition_change_fails_closed_to_weakening() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["entity_types"]["WorkItem"]["properties"]["status"] = {"type": "integer"}

    assert classify(mutate).classification == "weakened"


def test_relationship_structural_change_fails_closed_to_weakening() -> None:
    diff = classify(lambda d: d["relationships"][1].update(cardinality="one_to_one"))
    assert diff.classification == "weakened"


def test_auth_managed_dropped_is_weakening() -> None:
    old = build(
        lambda d: d["entity_types"]["Actor"].update(auth_managed=True, write_policy="mint_only")
    )
    new = build(lambda d: d["entity_types"]["Actor"].update(write_policy="mint_only"))
    assert diff_governance(old, new).classification == "weakened"


def test_audit_retention_change_fails_closed_to_weakening() -> None:
    diff = classify(lambda d: d.update(runtime={"trace_payloads": "metadata"}))
    assert diff.classification == "weakened"

    diff = classify(lambda d: d.update(runtime={"mutation_payloads": "full"}))
    assert diff.classification == "weakened"


def test_unrecognized_top_level_surface_fails_closed_to_weakening(
    monkeypatch: Any,
) -> None:
    # Simulate a config surface the classifier does not model by removing
    # 'enums' from the neutral allow-list: a change there must be weakening.
    monkeypatch.setattr(
        governance_diff,
        "_NEUTRAL_TOP_LEVEL_FIELDS",
        frozenset(governance_diff._NEUTRAL_TOP_LEVEL_FIELDS - {"enums"}),
    )
    diff = classify(lambda d: d.update(enums={"Status": {"values": ["open"]}}))
    assert diff.classification == "weakened"
    assert any("unrecognized config surface" in line for line in weakening_lines(diff))


def test_every_top_level_config_surface_is_categorized() -> None:
    # New CoreConfig fields must be explicitly categorized as governed or
    # neutral; until then they change-detect as weakening at runtime, and this
    # test forces the classifier decision at schema-change time.
    categorized = governance_diff._GOVERNED_TOP_LEVEL_FIELDS | (
        governance_diff._NEUTRAL_TOP_LEVEL_FIELDS
    )
    assert set(CoreConfig.model_fields) == set(categorized)


def test_every_runtime_surface_is_modeled() -> None:
    assert set(RuntimeConfigSchema.model_fields) == set(governance_diff._KNOWN_RUNTIME_FIELDS)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_weakening_dominates_mixed_change_sets() -> None:
    def mutate(data: dict[str, Any]) -> None:
        # One tightening (guard added) plus one weakening (error check removed).
        data["mutation_guards"].append(
            {
                "name": "second_guard",
                "entity_type": "WorkItem",
                "property": "status",
                "new_value": "open",
                "condition": {"type": "actor", "allowed_actor_ids": ["reviewer"]},
            }
        )
        data["quality_checks"] = [
            check for check in data["quality_checks"] if check["name"] != "status_required"
        ]

    diff = classify(mutate)
    assert diff.classification == "weakened"
    assert tightening_lines(diff) and weakening_lines(diff)


def test_summary_lines_carry_direction_surface_and_subject() -> None:
    diff = classify(lambda d: d.update(mutation_guards=[]))
    assert diff.summary_lines == [
        "[weakening] mutation_guard 'guarded_close': mutation guard removed"
    ]
