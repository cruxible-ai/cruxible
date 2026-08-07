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
    _input_reference_field,
    _procedure_step_input_references,
    lint_procedure_definition_authoring,
    lint_procedure_definition_authoring_typed,
)
from cruxible_core.workflow.compiler import _prior_step_aliases_by_index
from cruxible_core.workflow.contracts import resolve_contract
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


def _pre_graph_unconsumed_warnings(
    definition: ProcedureDefinition,
    config: CoreConfig,
) -> list[str]:
    """The pre-change contract-field warning slice, carried verbatim.

    The reference SCAN is shared with the implementation deliberately: C2
    widened it to read guard operands, and on a definition with no guards that
    widening is the identity. What is carried here is the RULE the scan feeds
    -- declared minus consumed, no path analysis -- which is the thing C2
    replaced.
    """
    contract = resolve_contract(config, definition.contract_in)
    if contract is None:
        return []
    consumed_fields: set[str] = set()
    for _node_id, _step_id, reference in _procedure_step_input_references(definition):
        if reference == "$input":
            consumed_fields.update(contract.fields)
            continue
        field_name = _input_reference_field(reference)
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


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_t4_the_contract_lint_matches_the_pre_graph_rule(
    entry: dict[str, Any],
    lint_config: CoreConfig,
) -> None:
    definition = _corpus_definition(entry)
    try:
        typed = lint_procedure_definition_authoring_typed(definition, lint_config)
    except ConfigError:
        # R10 on a contract this config resolves differently than the source
        # instance did. The refusal is unchanged by C2 and is not what T4 is
        # about.
        pytest.skip("definition is refused against this config")
    unconsumed = [
        warning.message for warning in typed if warning.code == WARNING_CONTRACT_FIELD_UNCONSUMED
    ]
    assert unconsumed == _pre_graph_unconsumed_warnings(definition, lint_config)
    assert not any(warning.code == WARNING_CONTRACT_FIELD_PATH_CONDITIONAL for warning in typed), (
        "a linear definition has one path; consumption is total or absent, never partial"
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_t4_the_string_channel_still_mirrors_the_typed_one(
    entry: dict[str, Any],
    lint_config: CoreConfig,
) -> None:
    """Dual-emit over the whole corpus, not just the cases C2 exercised."""
    definition = _corpus_definition(entry)
    try:
        typed = lint_procedure_definition_authoring_typed(definition, lint_config)
        strings = lint_procedure_definition_authoring(definition, lint_config)
    except ConfigError:
        pytest.skip("definition is refused against this config")
    assert strings == [warning.message for warning in typed]


def test_the_corpus_actually_exercises_the_contract_lint(lint_config: CoreConfig) -> None:
    """Guard on the guard: a suite that skips every entry proves nothing.

    Most corpus entries carry an inline or built-in contract, so the lint runs
    on them for real. If a config change ever made them all unresolvable, the
    two assertions above would go green while testing nothing.
    """
    resolved = 0
    warned = 0
    for entry in ENTRIES:
        definition = _corpus_definition(entry)
        if resolve_contract(lint_config, definition.contract_in) is None:
            continue
        resolved += 1
        try:
            if lint_procedure_definition_authoring_typed(definition, lint_config):
                warned += 1
        except ConfigError:
            continue
    assert resolved >= 30, f"only {resolved} corpus contracts resolve"
    assert warned >= 1, "no corpus entry produced a warning; the lint may be short-circuiting"


def test_the_contract_lint_diverges_once_arms_exist(lint_config: CoreConfig) -> None:
    """The counter-case: the third verdict only exists under branching."""
    definition = _branching_input_definition()
    typed = lint_procedure_definition_authoring_typed(definition, lint_config)
    assert [warning.code for warning in typed if "contract_in field" in warning.message] == [
        WARNING_CONTRACT_FIELD_PATH_CONDITIONAL
    ]
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
