"""CLI command registration — re-exports commands from domain submodules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    add_constraint_cmd: Any
    add_decision_policy_cmd: Any
    add_entity_cmd: Any
    add_relationship_cmd: Any
    analyze_feedback_cmd: Any
    analyze_outcomes_cmd: Any
    apply_cmd: Any
    config_views_cmd: Any
    connect_group: Any
    decision_records_cmd: Any
    evaluate: Any
    explain: Any
    export_group: Any
    feedback_batch_cmd: Any
    feedback_cmd: Any
    feedback_profile_cmd: Any
    find_candidates_cmd: Any
    fork_cmd: Any
    get_entity_cmd: Any
    get_relationship_cmd: Any
    group_group: Any
    ingest: Any
    init: Any
    inspect_group: Any
    lint_cmd: Any
    list_group: Any
    lock_cmd: Any
    outcome_cmd: Any
    outcome_profile_cmd: Any
    plan_cmd: Any
    propose_cmd: Any
    query: Any
    reload_config_cmd: Any
    render_wiki_cmd: Any
    run_cmd: Any
    sample: Any
    schema: Any
    server_group: Any
    snapshot_group: Any
    stats_cmd: Any
    test_cmd: Any
    validate: Any
    world_group: Any
else:
    from cruxible_core.cli.commands.config_views import config_views_cmd
    from cruxible_core.cli.commands.context import (
        connect_group,
    )
    from cruxible_core.cli.commands.decision_records import decision_records_cmd
    from cruxible_core.cli.commands.feedback import (
        feedback_batch_cmd,
        feedback_cmd,
        feedback_profile_cmd,
        outcome_cmd,
        outcome_profile_cmd,
    )
    from cruxible_core.cli.commands.groups import (
        group_group,
    )
    from cruxible_core.cli.commands.lists import (
        export_group,
        list_group,
    )
    from cruxible_core.cli.commands.mutations import (
        add_constraint_cmd,
        add_decision_policy_cmd,
        add_entity_cmd,
        add_relationship_cmd,
        reload_config_cmd,
    )
    from cruxible_core.cli.commands.reads import (
        analyze_feedback_cmd,
        analyze_outcomes_cmd,
        evaluate,
        explain,
        find_candidates_cmd,
        get_entity_cmd,
        get_relationship_cmd,
        inspect_group,
        lint_cmd,
        query,
        sample,
        schema,
        stats_cmd,
    )
    from cruxible_core.cli.commands.server import server_group
    from cruxible_core.cli.commands.wiki import render_wiki_cmd
    from cruxible_core.cli.commands.workflows import (
        apply_cmd,
        fork_cmd,
        ingest,
        init,
        lock_cmd,
        plan_cmd,
        propose_cmd,
        run_cmd,
        snapshot_group,
        test_cmd,
        validate,
    )
    from cruxible_core.cli.commands.world import world_group

__all__ = [
    "add_constraint_cmd",
    "add_decision_policy_cmd",
    "add_entity_cmd",
    "add_relationship_cmd",
    "analyze_feedback_cmd",
    "analyze_outcomes_cmd",
    "apply_cmd",
    "config_views_cmd",
    "connect_group",
    "decision_records_cmd",
    "evaluate",
    "explain",
    "export_group",
    "feedback_batch_cmd",
    "feedback_cmd",
    "feedback_profile_cmd",
    "find_candidates_cmd",
    "fork_cmd",
    "get_entity_cmd",
    "get_relationship_cmd",
    "group_group",
    "ingest",
    "init",
    "inspect_group",
    "lint_cmd",
    "list_group",
    "lock_cmd",
    "outcome_cmd",
    "outcome_profile_cmd",
    "plan_cmd",
    "propose_cmd",
    "query",
    "reload_config_cmd",
    "render_wiki_cmd",
    "run_cmd",
    "sample",
    "schema",
    "server_group",
    "snapshot_group",
    "stats_cmd",
    "test_cmd",
    "validate",
    "world_group",
]
