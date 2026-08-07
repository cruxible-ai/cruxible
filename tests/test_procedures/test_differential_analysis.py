"""T4 -- the differential analysis tests (§3.2, §7.2).

Batch C replaced three computations that every stored procedure already
depends on. §3.2 states the obligation as an equality rather than a promise:
under a LINEAR definition, analyses 2, 3 and 4 must produce byte-identical
results to `_prior_step_aliases_by_index`, the pre-graph `static_expansion`,
and `lint_procedure_definition_authoring` respectively.

The corpus is the population that claim is made about -- 48 frozen
0.3.2-normalized definitions, every one of them linear -- so each analysis is
run over all of them against an oracle carried here.

Two oracles are carried VERBATIM rather than imported, because importing the
implementation under test as its own oracle proves nothing. The third
(availability) has a real pre-existing oracle still in the tree, and that one
is imported.

Each assertion is paired with a BRANCHING counter-case showing the new
analysis genuinely diverges where linearity ends. Without those, a
differential suite would pass just as happily against an implementation that
never learned to branch at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.config.schema import CoreConfig, WorkflowStepSchema, workflow_step_kind
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.analysis import build_procedure_graph
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureRepeatStepSchema,
    unwrap_procedure_step,
)
from cruxible_core.service.procedures import (
    WARNING_CONTRACT_FIELD_PATH_CONDITIONAL,
    WARNING_CONTRACT_FIELD_UNCONSUMED,
    lint_procedure_definition_authoring,
    lint_procedure_definition_authoring_typed,
)
from cruxible_core.workflow.compiler import _prior_step_aliases_by_index
from cruxible_core.workflow.contracts import resolve_contract
from cruxible_core.workflow.refs import iter_step_reference_templates
from tests.test_procedures.conftest import CONFIG_YAML
from tests.test_procedures.test_definition_digest_corpus import ENTRIES, IDS

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 5}


def _corpus_definition(entry: dict[str, Any]) -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(entry["normalized_dump_v032"])


@pytest.fixture(scope="module")
def lint_config() -> CoreConfig:
    """The procedure test config, parsed once.

    Corpus entries carrying an INLINE contract resolve against any config; the
    named ones resolve only if this config declares them, and the rest fall
    through to the unknown-contract path the compiler owns.
    """
    import yaml

    return CoreConfig.model_validate(yaml.safe_load(CONFIG_YAML))


# ---------------------------------------------------------------------------
# The oracles
# ---------------------------------------------------------------------------


def _pre_graph_static_expansion(definition: ProcedureDefinition) -> dict[str, int]:
    """The pre-change `static_expansion` body, carried verbatim.

    Three SUMS over the whole body. C1 turned the two expanded counts into
    longest-path maxima; on a linear definition the one path IS the whole
    body, so the two agree -- and that agreement is what every stored
    definition's accepted budget depends on.
    """
    total_steps = 0
    expanded_steps = 0
    expanded_provider_calls = 0
    for wrapper in definition.steps:
        step = unwrap_procedure_step(wrapper)
        if isinstance(step, ProcedureRepeatStepSchema):
            nested_count = len(step.repeat.steps)
            nested_provider_count = sum(
                workflow_step_kind(nested) == "provider" for nested in step.repeat.steps
            )
            total_steps += 1 + nested_count
            expanded_steps += 1 + step.repeat.max_attempts * nested_count
            expanded_provider_calls += step.repeat.max_attempts * nested_provider_count
            continue
        total_steps += 1
        expanded_steps += 1
        if isinstance(step, WorkflowStepSchema) and workflow_step_kind(step) == "provider":
            expanded_provider_calls += 1
    return {
        "total_steps": total_steps,
        "expanded_steps": expanded_steps,
        "expanded_provider_calls": expanded_provider_calls,
    }


def _pre_graph_workflow_steps(definition: ProcedureDefinition) -> list[WorkflowStepSchema]:
    """`_procedure_workflow_steps` at b283e1d8, carried verbatim."""
    steps: list[WorkflowStepSchema] = []
    for wrapper in definition.steps:
        step = unwrap_procedure_step(wrapper)
        if isinstance(step, ProcedureRepeatStepSchema):
            steps.extend(step.repeat.steps)
        elif isinstance(step, WorkflowStepSchema):
            steps.append(step)
    return steps


def _pre_graph_input_references(definition: ProcedureDefinition) -> list[tuple[str, str]]:
    """`_procedure_step_input_references` at b283e1d8, carried verbatim.

    CARRIED, not imported. C2 widened the production scanner to read guard
    operands; importing it here would put the same code on both sides of the
    differential, so a scanner regression would corrupt the oracle and the
    subject together and the equality would hold while both were wrong.

    ``iter_step_reference_templates`` IS imported: batch C did not touch it
    (`git diff b283e1d8..HEAD -- workflow/refs.py` is empty), and copying an
    untouched shared module would pin a snapshot of something no longer under
    test.
    """
    references: list[tuple[str, str]] = []
    for step in _pre_graph_workflow_steps(definition):
        dumped = step.model_dump(mode="python", by_alias=True, exclude_none=True)
        for template in iter_step_reference_templates(dumped):
            references.extend((step.id, ref) for ref in _pre_graph_scan(template))
    return references


def _pre_graph_scan(value: Any) -> list[str]:
    """`_input_references` at b283e1d8, carried verbatim."""
    if isinstance(value, str):
        return [value] if value == "$input" or value.startswith("$input.") else []
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _pre_graph_scan(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _pre_graph_scan(item)]
    return []


def _pre_graph_reference_field(reference: str) -> str | None:
    """`_input_reference_field` at b283e1d8, carried verbatim."""
    if not reference.startswith("$input."):
        return None
    path = reference[len("$input.") :]
    return path.split(".", 1)[0].split("[", 1)[0]


def _pre_graph_unconsumed_warnings(
    definition: ProcedureDefinition,
    config: CoreConfig,
) -> list[str]:
    """The pre-change contract-field rule, over the pre-change scanner.

    Declared minus consumed, no path analysis, no guard operands -- the whole
    of what C2 replaced, reconstructed from the tree as it stood at AB's tip.
    """
    contract = resolve_contract(config, definition.contract_in)
    if contract is None:
        return []
    consumed_fields: set[str] = set()
    for _step_id, reference in _pre_graph_input_references(definition):
        if reference == "$input":
            consumed_fields.update(contract.fields)
            continue
        field_name = _pre_graph_reference_field(reference)
        if field_name is not None:
            consumed_fields.add(field_name)
    if contract.allow_extra:
        return []
    return [
        f"contract_in field '{field_name}' is declared but not consumed by any procedure step"
        for field_name in sorted(set(contract.fields) - consumed_fields)
    ]


# ---------------------------------------------------------------------------
# Analysis 2 -- per-path alias availability (§3.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_t4_availability_matches_the_compilers_prior_alias_walk(entry: dict[str, Any]) -> None:
    definition = _corpus_definition(entry)
    graph = build_procedure_graph(definition)
    assert [graph.available_aliases[node_id] for node_id in graph.node_ids] == (
        _prior_step_aliases_by_index(definition.steps)
    )


def test_availability_diverges_from_the_linear_walk_once_arms_exist() -> None:
    """The counter-case: the oracle is WRONG under branching, and must be.

    `_prior_step_aliases_by_index` accumulates every earlier step's alias in
    list order. After a join, an alias produced on one arm only is not
    available -- and the flat walk says it is.
    """
    definition = _branching_definition()
    graph = build_procedure_graph(definition)
    flat = _prior_step_aliases_by_index(definition.steps)
    per_node = [graph.available_aliases[node_id] for node_id in graph.node_ids]
    assert per_node != flat
    assert "hot_only" in flat[-1]
    assert "hot_only" not in graph.available_aliases["tail"]


# ---------------------------------------------------------------------------
# Analysis 3 -- worst-case budget (§3.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_t4_static_expansion_matches_the_pre_graph_sums(entry: dict[str, Any]) -> None:
    definition = _corpus_definition(entry)
    expansion = definition.static_expansion()
    assert {
        "total_steps": expansion.total_steps,
        "expanded_steps": expansion.expanded_steps,
        "expanded_provider_calls": expansion.expanded_provider_calls,
    } == _pre_graph_static_expansion(definition)


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_t4_the_linear_witness_path_is_the_whole_body(entry: dict[str, Any]) -> None:
    """One path, so the witness is every node in list order."""
    definition = _corpus_definition(entry)
    node_ids = tuple(str(step.id) for step in definition.steps)
    expansion = definition.static_expansion()
    assert expansion.expanded_steps_path == node_ids
    assert expansion.expanded_provider_calls_path == node_ids


def test_the_budget_diverges_from_the_sum_once_arms_exist() -> None:
    """The counter-case: three arms, one provider call each, max is one."""
    definition = _three_arm_definition()
    expansion = definition.static_expansion()
    oracle = _pre_graph_static_expansion(definition)
    assert oracle["expanded_provider_calls"] == 3
    assert expansion.expanded_provider_calls == 1
    assert expansion.total_steps == oracle["total_steps"]


# ---------------------------------------------------------------------------
# Analysis 4 -- per-path contract checking (§3.5)
# ---------------------------------------------------------------------------


R10_REFUSED_ENTRIES = frozenset(
    {
        "tests-test_lifecycle-02",
        "tests-test_lifecycle-06",
        "tests-test_validation-01",
    }
)
"""The three corpus entries this config refuses under R10, PINNED BY NAME.

Each was harvested from a test whose own config declared the field it reads;
against the procedure test config the reference is undeclared and the
pre-existing refusal fires. The refusal is unchanged by batch C.

A NAMED allow-list rather than a bare `except: skip`, because a skip is
indistinguishable from a pass in a summary line: a change that made a fourth
entry refuse would otherwise remove it from T4 silently, and T4 would report
green while covering less than it did.
"""


def _lint_or_refusal(
    definition: ProcedureDefinition,
    config: CoreConfig,
    label: str,
) -> list[Any] | None:
    """Lint one corpus entry, tolerating ONLY the pinned R10 refusals."""
    try:
        return lint_procedure_definition_authoring_typed(definition, config)
    except ConfigError as exc:
        assert label in R10_REFUSED_ENTRIES, (
            f"corpus entry '{label}' newly refuses against the test config: {exc}. "
            "A new refusal removes an entry from T4's coverage -- add it to "
            "R10_REFUSED_ENTRIES deliberately, or fix what started refusing it."
        )
        return None


@pytest.mark.parametrize("entry,label", zip(ENTRIES, IDS), ids=IDS)
def test_t4_the_contract_lint_matches_the_pre_graph_rule(
    entry: dict[str, Any],
    label: str,
    lint_config: CoreConfig,
) -> None:
    definition = _corpus_definition(entry)
    typed = _lint_or_refusal(definition, lint_config, label)
    if typed is None:
        return
    unconsumed = [
        warning.message for warning in typed if warning.code == WARNING_CONTRACT_FIELD_UNCONSUMED
    ]
    assert unconsumed == _pre_graph_unconsumed_warnings(definition, lint_config)
    assert not any(warning.code == WARNING_CONTRACT_FIELD_PATH_CONDITIONAL for warning in typed), (
        "a linear definition has one path; consumption is total or absent, never partial"
    )


@pytest.mark.parametrize("entry,label", zip(ENTRIES, IDS), ids=IDS)
def test_t4_the_string_channel_still_mirrors_the_typed_one(
    entry: dict[str, Any],
    label: str,
    lint_config: CoreConfig,
) -> None:
    """Dual-emit over the whole corpus, not just the cases C2 exercised."""
    definition = _corpus_definition(entry)
    typed = _lint_or_refusal(definition, lint_config, label)
    if typed is None:
        return
    assert lint_procedure_definition_authoring(definition, lint_config) == [
        warning.message for warning in typed
    ]


def test_every_pinned_refusal_still_refuses(lint_config: CoreConfig) -> None:
    """The allow-list is exact in BOTH directions.

    An entry that stops refusing has to leave the list, or the list slowly
    becomes a place where entries are excused for reasons that no longer exist.
    """
    still_refusing = set()
    for entry, label in zip(ENTRIES, IDS):
        try:
            lint_procedure_definition_authoring_typed(_corpus_definition(entry), lint_config)
        except ConfigError:
            still_refusing.add(label)
    assert still_refusing == R10_REFUSED_ENTRIES


def test_the_contract_lint_diverges_once_arms_exist(lint_config: CoreConfig) -> None:
    """The counter-case: the third verdict only exists under branching."""
    definition = _branching_input_definition()
    typed = lint_procedure_definition_authoring_typed(definition, lint_config)
    assert [warning.code for warning in typed if "contract_in field" in warning.message] == [
        WARNING_CONTRACT_FIELD_PATH_CONDITIONAL
    ]
    assert _pre_graph_unconsumed_warnings(definition, lint_config) == []


@pytest.mark.parametrize(
    "definition_factory,reader",
    [
        pytest.param(lambda: _repeat_body_input_definition(), "retry", id="repeat-body"),
        pytest.param(lambda: _query_params_input_definition(), "read", id="query-params"),
    ],
)
def test_the_scanner_reaches_every_reference_position_it_used_to(
    definition_factory: Any,
    reader: str,
    lint_config: CoreConfig,
) -> None:
    """Kind-specific counter-cases: a reference the scanner must still find.

    The two positions the flat step list reaches only by descending -- a
    reference inside a REPEAT BODY, and one in a query's PARAMS map. A scanner
    regression that stopped descending into either would report the field
    unconsumed; here it must instead be found, attributed to the CONTAINER
    node, and reported path-conditional because only one arm reaches it.
    """
    definition = definition_factory()
    typed = lint_procedure_definition_authoring_typed(definition, lint_config)
    conditional = [
        warning for warning in typed if warning.code == WARNING_CONTRACT_FIELD_PATH_CONDITIONAL
    ]
    assert [warning.node_ids for warning in conditional] == [[reader]]
    assert "'value'" in conditional[0].message
    # And the pre-change oracle finds the same reference, so the divergence is
    # the VERDICT rather than the scan.
    assert _pre_graph_unconsumed_warnings(definition, lint_config) == []


# ---------------------------------------------------------------------------
# The branching counter-cases
# ---------------------------------------------------------------------------


def _shape(step_id: str, alias: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
        "as": alias,
    }


def _branching_definition() -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "name": "differential_branching",
            "graph_format": 2,
            "steps": [
                {
                    "id": "gate",
                    "guard": {"left": 1, "op": "gt", "right": 0},
                    "on_true": "hot",
                    "on_false": "cold",
                    "message": "no",
                },
                {"step": _shape("hot", "hot_only"), "next": "tail"},
                _shape("cold", "cold_only"),
                _shape("tail", "final"),
            ],
            "returns": "final",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
        }
    )


def _three_arm_definition() -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "name": "differential_three_arm",
            "graph_format": 2,
            "steps": [
                {
                    "id": "triage",
                    "guard": {"left": 1, "op": "gt", "right": 0},
                    "on_true": "hot",
                    "on_false": "second",
                    "message": "no",
                },
                {
                    "id": "second",
                    "guard": {"left": 2, "op": "gt", "right": 0},
                    "on_true": "warm",
                    "on_false": "cold",
                    "message": "no",
                },
                {
                    "step": {"id": "hot", "provider": "scorer", "input": {}, "as": "hot_call"},
                    "next": "tail",
                },
                {
                    "step": {"id": "warm", "provider": "scorer", "input": {}, "as": "warm_call"},
                    "next": "tail",
                },
                {"id": "cold", "provider": "scorer", "input": {}, "as": "cold_call"},
                _shape("tail", "final"),
            ],
            "returns": "final",
            "precondition": {},
            "budget": _BUDGET,
        }
    )


def _branching_input_definition() -> ProcedureDefinition:
    """`value` read only on the true arm, against the test config's contract."""
    return ProcedureDefinition.model_validate(
        {
            "name": "differential_conditional_input",
            "contract_in": "ProcedureInput",
            "graph_format": 2,
            "steps": [
                {
                    "id": "gate",
                    "guard": {"left": 1, "op": "gt", "right": 0},
                    "on_true": "escalate",
                    "on_false": "tail",
                    "message": "no",
                },
                {
                    "step": {
                        "id": "escalate",
                        "provider": "exported_action",
                        "input": {"value": "$input.value"},
                        "as": "escalated",
                    },
                    "next": "tail",
                },
                _shape("tail", "final"),
            ],
            "returns": "final",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )


def _repeat_body_input_definition() -> ProcedureDefinition:
    """`$input.value` read from inside a REPEAT BODY, on one arm only.

    The nested step is not a graph node, so the finding is attributed to the
    repeat container -- the node paths actually run through.
    """
    return ProcedureDefinition.model_validate(
        {
            "name": "differential_repeat_body",
            "contract_in": "ProcedureInput",
            "graph_format": 2,
            "steps": [
                {
                    "id": "gate",
                    "guard": {"left": 1, "op": "gt", "right": 0},
                    "on_true": "retry",
                    "on_false": "tail",
                    "message": "no",
                },
                {
                    "id": "retry",
                    "as": "retried",
                    "repeat": {
                        "max_attempts": 2,
                        "until": {
                            "left": "$steps.attempt.value",
                            "op": "gte",
                            "right": 0,
                            "message": "settled",
                        },
                        "steps": [
                            {
                                "id": "attempt",
                                "provider": "exported_action",
                                "input": {"value": "$input.value"},
                                "as": "attempt",
                            }
                        ],
                    },
                },
                _shape("tail", "final"),
            ],
            "returns": "final",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 2},
            "declared_tier": "graph_write",
        }
    )


def _query_params_input_definition() -> ProcedureDefinition:
    """`$input.value` read from a query's PARAMS map, on one arm only."""
    return ProcedureDefinition.model_validate(
        {
            "name": "differential_query_params",
            "contract_in": "ProcedureInput",
            "graph_format": 2,
            "steps": [
                {
                    "id": "gate",
                    "guard": {"left": 1, "op": "gt", "right": 0},
                    "on_true": "read",
                    "on_false": "tail",
                    "message": "no",
                },
                {
                    "step": {
                        "id": "read",
                        "query": {
                            "mode": "collection",
                            "returns": "Task",
                            "result_shape": "entity",
                            "limit": 10,
                        },
                        "params": {"status": "$input.value"},
                        "as": "rows",
                    },
                    "next": "tail",
                },
                _shape("tail", "final"),
            ],
            "returns": "final",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
            "declared_tier": "graph_write",
        }
    )
