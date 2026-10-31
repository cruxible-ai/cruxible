"""Static laws for semantic pins, aliases, and terminal authority."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    analyze_procedure,
    compute_procedure_definition_digest,
    compute_procedure_node_digests,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    CaptureEgressNode,
    GuardNode,
    GuardPredicate,
    HaltNode,
    InboxEgressNode,
    PredicateOperand,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    ProposeChangeSetNode,
    ReadNode,
    RepeatBodyNode,
    RepeatNode,
    TransformNode,
)
from tests.support.procedures import named


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-static-law-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _predicate(alias: str) -> GuardPredicate:
    return GuardPredicate(
        left=PredicateOperand(kind="step", alias=alias),
        operator="eq",
        right=PredicateOperand(kind="literal", value=True),
    )


def _slot_signature(*, effect: str = "external_mutation") -> dict[str, object]:
    return {"in": "test.worker-input@1", "out": "test.worker-output@1", "effect": effect}


def _definition(
    nodes: Iterable[object],
    *,
    returns: str,
    terminal_capability: int = 3,
    slots: dict[str, object] | None = None,
    parameter_contract: ArtifactPin | None = None,
) -> ProcedureDefinition:
    return ProcedureDefinition(
        name="static-laws",
        contract_in=named(_pin("contract-in", "Contract", "definition-input")),
        contract_out=named(_pin("contract-out", "Contract", "definition-output")),
        parameter_contract=named(parameter_contract),
        nodes=tuple(nodes),  # type: ignore[arg-type]
        returns=returns,
        slots=slots or {},
        budget=ProcedureBudget(
            wall_clock=CanonicalDuration(microseconds=2_000_000),
            max_provider_calls=10,
            max_capture_bytes=10_000,
            max_items=100,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=CanonicalDuration(microseconds=4_000_000),
            max_provider_calls=20,
            max_capture_bytes=20_000,
            max_items=200,
            max_repeat_attempts=3,
        ),
        terminal_capability=terminal_capability,  # type: ignore[arg-type]
    )


def _kitchen_sink_definition() -> ProcedureDefinition:
    contract_in = _pin("contract-in", "Contract", "node-input")
    contract_out = _pin("contract-out", "Contract", "node-output")
    return _definition(
        (
            ReadNode(
                node_id="state",
                query=named(_pin("query", "QueryDefinition", "accepted-state")),
                parameters={},
                as_="state_rows",
            ),
            CallNode(
                node_id="source",
                captures={
                    "capture": named(_pin("capture-contract", "CaptureContract", "source-capture"))
                },
                slot="fetch",
                input={"state": "$steps.state_rows"},
                as_="source_rows",
            ),
            CallNode(
                node_id="provider",
                slot="worker",
                effect_policy=named(_pin("effect-policy", "EffectPolicy", "provider-effects")),
                input={"source": "$steps.source_rows"},
                as_="provider_rows",
            ),
            TransformNode(
                node_id="transform",
                transform_kind="shape_items",
                contract_in=named(contract_in),
                contract_out=named(contract_out),
                spec={
                    "tag": "playbill-transform-shape-items-spec-v1",
                    "items": "$steps.provider_rows",
                    "fields": {},
                    "include_input": True,
                },
                as_="transformed_rows",
            ),
            ProjectNode(
                node_id="project",
                fields={"rows": "$steps.transformed_rows"},
                contract_out=named(contract_out),
                as_="projected_rows",
            ),
            RepeatNode(
                node_id="repeat",
                max_attempts=2,
                body=(
                    RepeatBodyNode(
                        node_id="body-transform",
                        operation="transform",
                        transform_kind="shape_items",
                        contract_in=named(contract_in),
                        contract_out=named(contract_out),
                        spec={
                            "tag": "playbill-transform-shape-items-spec-v1",
                            "items": "$steps.projected_rows",
                            "fields": {},
                        },
                        as_="body_rows",
                    ),
                    RepeatBodyNode(
                        node_id="body-provider",
                        operation="call",
                        slot="worker",
                        effect_policy=named(
                            _pin("effect-policy", "EffectPolicy", "repeat-effects")
                        ),
                        spec={"rows": "$steps.body_rows"},
                        as_="body_result",
                    ),
                ),
                until=_predicate("body_result"),
                as_="repeated_rows",
            ),
            CaptureEgressNode(
                node_id="capture",
                capture_contract=named(
                    _pin("capture-contract", "CaptureContract", "result-capture")
                ),
                input={"rows": "$steps.repeated_rows"},
                result="$steps.repeated_rows",
            ),
        ),
        returns="repeated_rows",
        parameter_contract=_pin("parameter-contract", "Contract", "parameters"),
        slots={
            "worker": _slot_signature(),
            "fetch": _slot_signature(effect="external_read"),
        },
    )


def _replace_path(root: object, path: tuple[object, ...], value: object) -> None:
    current = root
    for member in path[:-1]:
        current = current[member]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]


@pytest.mark.parametrize(
    "path",
    (
        ("contract_in",),
        ("contract_out",),
        ("parameter_contract",),
        ("nodes", 0, "query"),
        ("nodes", 1, "captures", "capture"),
        ("nodes", 2, "effect_policy"),
        ("nodes", 3, "contract_in"),
        ("nodes", 3, "contract_out"),
        ("nodes", 4, "contract_out"),
        ("nodes", 5, "body", 0, "contract_in"),
        ("nodes", 5, "body", 0, "contract_out"),
        ("nodes", 6, "capture_contract"),
    ),
)
def test_semantic_reference_expectations_cover_every_dependency_field(
    path: tuple[object, ...],
) -> None:
    payload = _kitchen_sink_definition().model_dump(mode="json", by_alias=True)
    wrong = named(_pin("wrong", "WrongArtifact", "wrong")).model_dump(mode="json")
    _replace_path(payload, path, wrong)

    with pytest.raises(ValidationError, match="requires role="):
        ProcedureDefinition.model_validate(payload)


# NOTE: test_semantic_pin_expectations_validate_slot_declarations_not_only_bindings
# tested the old generic pin_slots mechanism, where any node field could be
# declared as a cross-checked slot reference (ProcedurePinSlotRef) with its
# own independent role declaration (ProcedurePinSlot) that could disagree
# with how it was actually used. Slots are now type-fixed to provider-calling
# nodes only (CallNode/CallNode/the call arm of RepeatBodyNode), named
# directly as a plain `slot: str`, with no indirection and so no possible
# role mismatch to validate -- the test had no surviving behavior to migrate
# to and is deleted.


def test_branch_join_tracks_must_availability_separately_from_may_reachability() -> None:
    contract_out = _pin("contract-out", "Contract", "result")
    definition = _definition(
        (
            ReadNode(
                node_id="state",
                query=named(_pin("query", "QueryDefinition", "state")),
                as_="rows",
            ),
            GuardNode(
                node_id="branch",
                predicate=_predicate("rows"),
                on_true="hot",
                on_false="join",
                refusal_code="branch.false",
                message="Take the cold path.",
            ),
            ProjectNode(
                node_id="hot",
                fields={"hot": True},
                contract_out=named(contract_out),
                as_="hot_rows",
                next="join",
            ),
            ProjectNode(
                node_id="join",
                fields={"status": "joined"},
                contract_out=named(contract_out),
                as_="result",
            ),
        ),
        returns="result",
    )

    graph = analyze_procedure(definition)
    assert graph.available_aliases["join"] == frozenset({"rows"})
    assert graph.reachable_aliases["join"] == frozenset({"rows", "hot_rows"})

    payload = definition.model_dump(mode="json", by_alias=True)
    payload["nodes"][3]["fields"] = {"hot": "$steps.hot_rows"}
    with pytest.raises(ProcedureGraphFormatError, match="not produced on every path"):
        ProcedureDefinition.model_validate(payload)


def test_guard_at_a_join_cannot_read_an_alias_from_only_one_branch() -> None:
    contract_out = _pin("contract-out", "Contract", "result")
    with pytest.raises(ProcedureGraphFormatError, match="not produced on every path"):
        _definition(
            (
                ReadNode(
                    node_id="state",
                    query=named(_pin("query", "QueryDefinition", "state")),
                    as_="rows",
                ),
                GuardNode(
                    node_id="branch",
                    predicate=_predicate("rows"),
                    on_true="hot",
                    on_false="join",
                    refusal_code="branch.false",
                    message="Take the cold path.",
                ),
                ProjectNode(
                    node_id="hot",
                    fields={"hot": True},
                    contract_out=named(contract_out),
                    as_="hot_rows",
                    next="join",
                ),
                GuardNode(
                    node_id="join",
                    predicate=_predicate("hot_rows"),
                    on_false="$abort",
                    refusal_code="hot.missing",
                    message="Hot data is required.",
                ),
                ProjectNode(
                    node_id="result",
                    fields={"status": "joined"},
                    contract_out=named(contract_out),
                    as_="result",
                ),
            ),
            returns="result",
        )


def test_trailing_halt_is_a_terminal_graph_leaf_without_edges() -> None:
    definition = _definition(
        (
            ReadNode(
                node_id="read",
                query=named(_pin("query", "QueryDefinition", "halt-input")),
                as_="rows",
            ),
            HaltNode(node_id="stop", reason="No work remains."),
        ),
        returns="rows",
        terminal_capability=1,
    )

    graph = analyze_procedure(definition)

    assert graph.edges["read"] == {"next": "stop"}
    assert graph.edges["stop"] == {}


def _guard_halt_layout(*, halt_before_return: bool) -> ProcedureDefinition:
    contract_out = _pin("contract-out", "Contract", "guard-layout-result")
    read = ReadNode(
        node_id="read",
        query=named(_pin("query", "QueryDefinition", "guard-layout-input")),
        as_="rows",
        next="gate",
    )
    gate = GuardNode(
        node_id="gate",
        predicate=_predicate("rows"),
        on_true="result",
        on_false="stop",
        refusal_code="guard.empty",
        message="No rows are available.",
    )
    result = ProjectNode(
        node_id="result",
        fields={"rows": "$steps.rows"},
        contract_out=named(contract_out),
        as_="result",
    )
    stop = HaltNode(node_id="stop", reason="No rows are available.")
    tail = (stop, result) if halt_before_return else (result, stop)
    return _definition((read, gate, *tail), returns="result", terminal_capability=1)


def test_guard_arm_halt_layout_order_has_identical_graph_identity() -> None:
    halt_first = _guard_halt_layout(halt_before_return=True)
    halt_last = _guard_halt_layout(halt_before_return=False)

    first_graph = analyze_procedure(halt_first)
    last_graph = analyze_procedure(halt_last)
    assert first_graph.edges == last_graph.edges
    assert last_graph.edges["result"] == {}
    assert last_graph.predecessors["stop"] == ("gate",)
    assert compute_procedure_node_digests(halt_first) == (compute_procedure_node_digests(halt_last))
    assert compute_procedure_definition_digest(halt_first) == (
        compute_procedure_definition_digest(halt_last)
    )


@pytest.mark.parametrize("explicit_halt", (False, True))
def test_terminal_position_guard_requires_authored_true_target(explicit_halt: bool) -> None:
    result = ProjectNode(
        node_id="result",
        fields={"ok": True},
        contract_out=named(_pin("contract-out", "Contract", "terminal-guard-result")),
        as_="result",
    )
    gate = GuardNode(
        node_id="gate",
        predicate=GuardPredicate(
            left=PredicateOperand(kind="literal", value=True),
            operator="eq",
            right=PredicateOperand(kind="literal", value=True),
        ),
        on_false="stop" if explicit_halt else "$abort",
        refusal_code="guard.false",
        message="The guard refused.",
    )
    nodes: tuple[object, ...] = (
        (result, HaltNode(node_id="stop", reason="The guard refused."), gate)
        if explicit_halt
        else (result, gate)
    )

    with pytest.raises(ProcedureGraphFormatError) as raised:
        _definition(nodes, returns="result", terminal_capability=1)

    assert str(raised.value) == (
        "Procedure guard 'gate' with omitted on_true must name a forward true target "
        "or have its intended successor placed after the guard"
    )


def test_nonterminal_guard_keeps_implicit_true_fallthrough() -> None:
    contract_out = _pin("contract-out", "Contract", "guard-fallthrough")
    definition = _definition(
        (
            GuardNode(
                node_id="gate",
                predicate=GuardPredicate(
                    left=PredicateOperand(kind="literal", value=True),
                    operator="eq",
                    right=PredicateOperand(kind="literal", value=True),
                ),
                on_false="$abort",
                refusal_code="guard.false",
                message="The guard refused.",
            ),
            ProjectNode(
                node_id="result",
                fields={"ok": True},
                contract_out=named(contract_out),
                as_="result",
            ),
        ),
        returns="result",
    )

    assert analyze_procedure(definition).edges["gate"] == {
        "on_false": "$abort",
        "on_true": "result",
    }


def test_guard_arm_halt_exposes_an_intervening_nonreturn_leaf() -> None:
    contract_out = _pin("contract-out", "Contract", "guard-tail-result")
    with pytest.raises(ProcedureGraphFormatError) as raised:
        _definition(
            (
                ReadNode(
                    node_id="read",
                    query=named(_pin("query", "QueryDefinition", "guard-tail-input")),
                    as_="rows",
                    next="gate",
                ),
                GuardNode(
                    node_id="gate",
                    predicate=_predicate("rows"),
                    on_true="result",
                    on_false="stop",
                    refusal_code="guard.empty",
                    message="No rows are available.",
                ),
                TransformNode(
                    node_id="result",
                    transform_kind="aggregate_items",
                    contract_in=named(_pin("contract-in", "Contract", "guard-tail-items")),
                    contract_out=named(contract_out),
                    spec={
                        "tag": "playbill-transform-aggregate-items-spec-v1",
                        "items": "$steps.rows",
                    },
                    as_="result",
                ),
                ProjectNode(
                    node_id="tail",
                    fields={"extra": True},
                    contract_out=named(contract_out),
                    as_="extra",
                ),
                HaltNode(node_id="stop", reason="No rows are available."),
            ),
            returns="result",
            terminal_capability=1,
        )

    assert str(raised.value) == (
        "Procedure leaf 'tail' neither halts, emits typed egress, nor returns "
        "the declared output alias 'result'"
    )


@pytest.mark.parametrize(
    "path",
    (
        ("nodes", 0, "parameters"),
        ("nodes", 1, "input"),
        ("nodes", 2, "input"),
        ("nodes", 3, "spec", "items"),
        ("nodes", 4, "fields"),
        ("nodes", 5, "body", 0, "spec", "items"),
        ("nodes", 6, "input"),
        ("nodes", 6, "result"),
    ),
)
def test_structured_step_references_are_checked_in_every_runtime_template(
    path: tuple[object, ...],
) -> None:
    payload = _kitchen_sink_definition().model_dump(mode="json", by_alias=True)
    _replace_path(payload, path, {"missing": "$steps.missing"})
    with pytest.raises(ProcedureGraphFormatError, match="missing"):
        ProcedureDefinition.model_validate(payload)


def test_transform_specs_are_tagged_closed_and_kind_matched() -> None:
    definition = _kitchen_sink_definition()
    transform = definition.nodes[3]
    assert isinstance(transform, TransformNode)
    raw = transform.model_dump(mode="json", by_alias=True)

    untagged = {**raw, "spec": {"items": [], "fields": {}}}
    with pytest.raises(ValueError, match="tag"):
        TransformNode.model_validate(untagged)

    mismatched = {
        **raw,
        "spec": {"tag": "playbill-transform-aggregate-items-spec-v1", "items": []},
    }
    with pytest.raises(ValueError, match="does not match"):
        TransformNode.model_validate(mismatched)

    extra = raw.copy()
    extra["spec"] = {**raw["spec"], "undeclared": True}
    with pytest.raises(ValueError, match="Extra inputs"):
        TransformNode.model_validate(extra)


@pytest.mark.parametrize(
    ("terminal", "capability", "accepted"),
    (
        (InboxEgressNode(node_id="inbox", input="$steps.result"), 1, True),
        (
            ProposeChangeSetNode(
                node_id="propose",
                candidate_templates=({"input": "$steps.result"},),
                result="$steps.result",
            ),
            1,
            False,
        ),
        (
            ProposeChangeSetNode(
                node_id="propose",
                candidate_templates=({"input": "$steps.result"},),
                result="$steps.result",
            ),
            2,
            True,
        ),
    ),
)
def test_terminal_kinds_cannot_exceed_the_declared_capability(
    terminal: object,
    capability: int,
    accepted: bool,
) -> None:
    nodes = (
        ProjectNode(
            node_id="project",
            fields={"ready": True},
            contract_out=named(_pin("contract-out", "Contract", "result")),
            as_="result",
        ),
        terminal,
    )
    if accepted:
        assert (
            _definition(
                nodes,
                returns="result",
                terminal_capability=capability,
            ).terminal_capability
            == capability
        )
    else:
        with pytest.raises(ProcedureGraphFormatError, match="requires rung"):
            _definition(nodes, returns="result", terminal_capability=capability)


def test_external_provider_effects_do_not_raise_the_terminal_rung() -> None:
    contract_out = _pin("contract-out", "Contract", "provider-output")
    definition = _definition(
        (
            CallNode(
                node_id="provider",
                slot="external",
                effect_policy=named(_pin("effect-policy", "EffectPolicy", "external-effects")),
                input={},
                as_="provider_result",
            ),
            ProjectNode(
                node_id="project",
                fields={"result": "$steps.provider_result"},
                contract_out=named(contract_out),
                as_="result",
            ),
        ),
        returns="result",
        terminal_capability=1,
        slots={"external": _slot_signature()},
    )
    assert definition.terminal_capability == 1


@pytest.mark.parametrize(
    "terminal",
    (
        InboxEgressNode(node_id="inbox", input="$steps.missing"),
        ProposeChangeSetNode(
            node_id="propose",
            candidate_templates=({"input": "$steps.missing"},),
            result="$steps.result",
        ),
    ),
)
def test_all_terminal_runtime_templates_receive_structured_reference_checks(
    terminal: object,
) -> None:
    with pytest.raises(ProcedureGraphFormatError, match="missing"):
        _definition(
            (
                ProjectNode(
                    node_id="project",
                    fields={"ready": True},
                    contract_out=named(_pin("contract-out", "Contract", "result")),
                    as_="result",
                ),
                terminal,
            ),
            returns="result",
        )
