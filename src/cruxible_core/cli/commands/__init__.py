"""Lazy re-exports for CLI commands grouped by domain module."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    attest_group: Any
    add_constraint_cmd: Any
    add_decision_policy_cmd: Any
    add_entity_cmd: Any
    add_relationship_cmd: Any
    analyze_feedback_cmd: Any
    analyze_outcomes_cmd: Any
    apply_cmd: Any
    batch_direct_write_cmd: Any
    config_expand_cmd: Any
    config_status_cmd: Any
    config_views_cmd: Any
    connect_group: Any
    decision_records_cmd: Any
    credential_group: Any
    evaluate: Any
    explain: Any
    export_group: Any
    feedback_batch_cmd: Any
    feedback_cmd: Any
    feedback_from_query_cmd: Any
    feedback_group: Any
    feedback_profile_cmd: Any
    clone_cmd: Any
    gate_group: Any
    get_entity_cmd: Any
    get_relationship_cmd: Any
    group_group: Any
    init: Any
    instance_group: Any
    inspect_entity_cmd: Any
    inspect_entity_history_cmd: Any
    inspect_group: Any
    inspect_relationship_lineage_cmd: Any
    lint_cmd: Any
    list_group: Any
    lock_cmd: Any
    outcome_cmd: Any
    outcome_group: Any
    outcome_profile_cmd: Any
    plan_cmd: Any
    propose_cmd: Any
    procedure_group: Any
    query: Any
    reload_config_cmd: Any
    run_cmd: Any
    sample: Any
    schema: Any
    server_group: Any
    snapshot_group: Any
    source_group: Any
    stats_cmd: Any
    test_cmd: Any
    update_entity_cmd: Any
    update_relationship_cmd: Any
    validate: Any
    state_group: Any
    ws_group: Any

_COMMAND_MODULES = {
    "attest_group": "attestations",
    "add_constraint_cmd": "mutations",
    "add_decision_policy_cmd": "mutations",
    "add_entity_cmd": "mutations",
    "add_relationship_cmd": "mutations",
    "analyze_feedback_cmd": "reads",
    "analyze_outcomes_cmd": "reads",
    "apply_cmd": "workflows",
    "batch_direct_write_cmd": "mutations",
    "clone_cmd": "workflows",
    "config_expand_cmd": "config_views",
    "config_status_cmd": "mutations",
    "config_views_cmd": "config_views",
    "connect_group": "context",
    "credential_group": "credentials",
    "decision_records_cmd": "decision_records",
    "evaluate": "reads",
    "explain": "reads",
    "export_group": "lists",
    "feedback_batch_cmd": "feedback",
    "feedback_cmd": "feedback",
    "feedback_from_query_cmd": "feedback",
    "feedback_group": "feedback",
    "feedback_profile_cmd": "feedback",
    "gate_group": "gates",
    "get_entity_cmd": "reads",
    "get_relationship_cmd": "reads",
    "group_group": "groups",
    "init": "workflows",
    "instance_group": "instances",
    "inspect_entity_cmd": "reads",
    "inspect_entity_history_cmd": "reads",
    "inspect_group": "reads",
    "inspect_relationship_lineage_cmd": "reads",
    "lint_cmd": "reads",
    "list_group": "lists",
    "lock_cmd": "workflows",
    "outcome_cmd": "feedback",
    "outcome_group": "feedback",
    "outcome_profile_cmd": "feedback",
    "plan_cmd": "workflows",
    "propose_cmd": "workflows",
    "procedure_group": "procedures",
    "query": "reads",
    "reload_config_cmd": "mutations",
    "run_cmd": "workflows",
    "sample": "reads",
    "schema": "reads",
    "server_group": "server",
    "snapshot_group": "workflows",
    "source_group": "source_artifacts",
    "state_group": "state",
    "stats_cmd": "read_stats",
    "test_cmd": "workflows",
    "update_entity_cmd": "mutations",
    "update_relationship_cmd": "mutations",
    "validate": "workflows",
    "ws_group": "working_set",
}


def __getattr__(name: str) -> Any:
    """Import only the domain module that owns the requested command."""
    module_name = _COMMAND_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{module_name}")
    command = getattr(module, name)
    globals()[name] = command
    return command


def __dir__() -> list[str]:
    return sorted({*globals(), *_COMMAND_MODULES})


__all__ = [
    "attest_group",
    "add_constraint_cmd",
    "add_decision_policy_cmd",
    "add_entity_cmd",
    "add_relationship_cmd",
    "analyze_feedback_cmd",
    "analyze_outcomes_cmd",
    "apply_cmd",
    "batch_direct_write_cmd",
    "config_expand_cmd",
    "config_status_cmd",
    "config_views_cmd",
    "connect_group",
    "decision_records_cmd",
    "credential_group",
    "evaluate",
    "explain",
    "export_group",
    "feedback_batch_cmd",
    "feedback_cmd",
    "feedback_from_query_cmd",
    "feedback_group",
    "feedback_profile_cmd",
    "clone_cmd",
    "gate_group",
    "get_entity_cmd",
    "get_relationship_cmd",
    "group_group",
    "init",
    "instance_group",
    "inspect_entity_cmd",
    "inspect_entity_history_cmd",
    "inspect_group",
    "inspect_relationship_lineage_cmd",
    "lint_cmd",
    "list_group",
    "lock_cmd",
    "outcome_cmd",
    "outcome_group",
    "outcome_profile_cmd",
    "plan_cmd",
    "propose_cmd",
    "procedure_group",
    "query",
    "reload_config_cmd",
    "run_cmd",
    "sample",
    "schema",
    "server_group",
    "snapshot_group",
    "source_group",
    "stats_cmd",
    "test_cmd",
    "update_entity_cmd",
    "update_relationship_cmd",
    "validate",
    "state_group",
    "ws_group",
]
