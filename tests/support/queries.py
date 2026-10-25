"""Closed native queries for graph fixtures whose subject is not query evaluation."""

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.query.definitions import (
    NamedQueryDefinition,
    QueryEvaluationPolicy,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryEntry,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
)


def subject_query_members(name, *other_names):
    if other_names:
        return {
            path: value
            for item in (name, *other_names)
            for path, value in subject_query_members(item).items()
        }
    query = NamedQueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name=name),
        entry=QueryEntry(binding="item", subject_kinds=("project.work_item",)),
        result_binding="item",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="id", value=QuerySubjectFieldRef(binding="item", field="subject_id")
                ),
            )
        ),
        evaluation_policy=QueryEvaluationPolicy(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        ),
        default_budgets=QueryBudgets(max_results=100, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=100, max_traversal_depth=0),
    )
    return {query_definition_path(name): render_query_definition(query)}
