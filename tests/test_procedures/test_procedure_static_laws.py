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
    ClaimsNode,
    GuardNode,
    HaltNode,
    ProcedureDefinition,
    ProjectNode,
    ReadNode,
    RepeatBodyNode,
    RepeatNode,
    TransformNode,
)
from cruxible_client.contracts.refs import step_ref
from cruxible_client.contracts.shapes import Comparison, LiteralOperand, Predicate
from tests.support.inbox import inbox_node
from tests.support.procedures import named, procedure_definition, procedure_hard_caps


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-static-law-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _predicate(alias: str) -> Predicate:
    return Comparison(op="eq", left=step_ref(alias), right=LiteralOperand(literal=True))


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
    return procedure_definition(
        name="static-laws",
        contract_in=named(_pin("contract-in", "Contract", "definition-input/v1")),
        contract_out=named(_pin("contract-out", "Contract", "definition-output/v1")),
        parameter_contract=named(parameter_contract),
        nodes=tuple(nodes),  # type: ignore[arg-type]
        returns=returns,
        slots=slots or {},
        hard_caps=procedure_hard_caps(
            max_wall_clock=CanonicalDuration(microseconds=4_000_000),
            max_provider_calls=20,
            max_capture_bytes=20_000,
            max_repeat_attempts=3,
        ),
        terminal_capability=terminal_capability,  # type: ignore[arg-type]
    )


def _kitchen_sink_definition() -> ProcedureDefinition:
    _pin("contract-out", "Contract", "node-output/v1")
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
                transform_kind="shape",
                spec={
                    "tag": "cruxible-transform-shape-spec-v1",
                    "input": "$steps.provider_rows",
                    "fields": {"rows": {"from": "$item.fields.rows"}},
                },
                as_="transformed_rows",
            ),
            ProjectNode(
                node_id="project",
                fields={"rows": "$steps.transformed_rows"},
                as_="projected_rows",
            ),
            RepeatNode(
                node_id="repeat",
                max_attempts=2,
                body=(
                    RepeatBodyNode(
                        node_id="body-transform",
                        operation="transform",
                        transform_kind="shape",
                        spec={
                            "tag": "cruxible-transform-shape-spec-v1",
                            "input": "$steps.projected_rows",
                            "fields": {"rows": {"from": "$item.fields.rows"}},
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
        parameter_contract=_pin("parameter-contract", "Contract", "parameters/v1"),
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
    _pin("contract-out", "Contract", "result/v1")
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
            ),
            ProjectNode(
                node_id="hot",
                fields={"hot": True},
                as_="hot_rows",
                next="join",
            ),
            ProjectNode(
                node_id="join",
                fields={"status": "joined"},
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
    _pin("contract-out", "Contract", "result/v1")
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
                ),
                ProjectNode(
                    node_id="hot",
                    fields={"hot": True},
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
    _pin("contract-out", "Contract", "guard-layout-result/v1")
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
    )
    result = ProjectNode(
        node_id="result",
        fields={"rows": "$steps.rows"},
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
        as_="result",
    )
    gate = GuardNode(
        node_id="gate",
        predicate=Comparison(
            op="eq", left=LiteralOperand(literal=True), right=LiteralOperand(literal=True)
        ),
        on_false="stop" if explicit_halt else "$abort",
        refusal_code=None if explicit_halt else "guard.false",
        message=None if explicit_halt else "The guard refused.",
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
    _pin("contract-out", "Contract", "guard-fallthrough/v1")
    definition = _definition(
        (
            GuardNode(
                node_id="gate",
                predicate=Comparison(
                    op="eq", left=LiteralOperand(literal=True), right=LiteralOperand(literal=True)
                ),
                on_false="$abort",
                refusal_code="guard.false",
                message="The guard refused.",
            ),
            ProjectNode(
                node_id="result",
                fields={"ok": True},
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
    _pin("contract-out", "Contract", "guard-tail-result/v1")
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
                ),
                TransformNode(
                    node_id="result",
                    transform_kind="aggregate",
                    spec={
                        "tag": "cruxible-transform-aggregate-spec-v1",
                        "input": "$steps.rows",
                        "op": "count",
                    },
                    as_="result",
                ),
                ProjectNode(
                    node_id="tail",
                    fields={"extra": True},
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
        ("nodes", 3, "spec", "input"),
        ("nodes", 4, "fields"),
        ("nodes", 5, "body", 0, "spec", "input"),
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


@pytest.mark.parametrize(
    ("retired", "replacement"),
    [
        ("shape_items", "'shape'"),
        ("filter_items", "'filter'"),
        ("join_items", "'join'"),
        ("aggregate_items", "'aggregate'"),
        ("dedupe_items", "'dedupe'"),
    ],
)
def test_retired_item_transform_kinds_are_refused_naming_their_closed_replacement(
    retired: str, replacement: str
) -> None:
    """F12: one transform vocabulary; the old item kinds refuse, in nodes and repeat bodies."""

    from cruxible_client.contracts.procedures.nodes import RetiredNodeKind

    payload = _kitchen_sink_definition().model_dump(mode="json", by_alias=True)
    node = payload["nodes"][3]
    node["transform_kind"] = retired
    with pytest.raises(ValidationError, match="procedure_node_kind_retired") as caught:
        ProcedureDefinition.model_validate(payload)
    assert replacement in str(caught.value)
    with pytest.raises((RetiredNodeKind, ValidationError), match=replacement):
        RepeatBodyNode.model_validate(
            {
                "node_id": "b",
                "operation": "transform",
                "transform_kind": retired,
                "spec": {},
                "as": "b",
            }
        )


def test_transform_specs_are_tagged_closed_and_kind_matched() -> None:
    definition = _kitchen_sink_definition()
    transform = definition.nodes[3]
    assert isinstance(transform, TransformNode)
    raw = transform.model_dump(mode="json", by_alias=True)

    untagged = {**raw, "spec": {"input": [], "fields": {}}}
    with pytest.raises(ValueError, match="tag"):
        TransformNode.model_validate(untagged)

    mismatched = {
        **raw,
        "spec": {"tag": "cruxible-transform-aggregate-spec-v1", "input": [], "op": "count"},
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
        (inbox_node(fields={"note": "$steps.result"}), 1, True),
        (
            ClaimsNode(
                node_id="propose",
                for_each="$steps.result.records",
                subject={"kind": "project.work_item", "key": {"slug": "one"}},
                claims=[
                    {
                        "type": {
                            "target": {"kind": "ClaimType", "name": "project.work_item.status"}
                        },
                        "value": "ready",
                    }
                ],
            ),
            1,
            False,
        ),
        (
            ClaimsNode(
                node_id="propose",
                for_each="$steps.result.records",
                subject={"kind": "project.work_item", "key": {"slug": "one"}},
                claims=[
                    {
                        "type": {
                            "target": {"kind": "ClaimType", "name": "project.work_item.status"}
                        },
                        "value": "ready",
                    }
                ],
            ),
            3,
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
    _pin("contract-out", "Contract", "provider-output/v1")
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
        inbox_node(fields={"note": "$steps.missing"}),
        ClaimsNode(
            node_id="propose",
            for_each="$steps.missing.records",
            subject={"kind": "project.work_item", "key": {"slug": "one"}},
            claims=[
                {
                    "type": {"target": {"kind": "ClaimType", "name": "project.work_item.status"}},
                    "value": "ready",
                }
            ],
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
                    as_="result",
                ),
                terminal,
            ),
            returns="result",
        )


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        ({"fields": {"note": "x"}, "for_each": [1, 2]}, "names one list"),
        ({"fields": {"note": "$item.note"}}, "only under for_each"),
        ({"fields": {"note": "x"}, "dedupe_key": ["missing"]}, "dedupe_key"),
        ({"fields": {}}, "at least 1"),
        ({"input": {"items": []}, "fields": {"note": "x"}}, "Extra inputs"),
    ],
    ids=["for-each-literal", "item-without-for-each", "unknown-key", "no-fields", "untyped-input"],
)
def test_a_post_inbox_declares_its_typed_items(declared: dict[str, object], message: str) -> None:
    """F13: {contract, for_each, fields, dedupe_key, max_items}; nothing untyped."""

    from cruxible_client.contracts.procedures.models import InboxEgressNode

    with pytest.raises(ValidationError, match=message):
        InboxEgressNode.model_validate(
            {"node_id": "inbox", "contract": "test.inbox-note@1", **declared}
        )
    typed = InboxEgressNode.model_validate(
        {
            "node_id": "inbox",
            "contract": "test.inbox-note@1",
            "for_each": "$steps.rows.records",
            "fields": {"note": "$item.fields.note"},
            "dedupe_key": ["note"],
        }
    )
    assert typed.contract.spelling == "test.inbox-note@1"


def test_a_post_inbox_contract_is_pinned_to_its_accepted_version() -> None:
    from cruxible_client.contracts.procedures.artifacts import extract_row_problem

    node = inbox_node(fields={"note": "$steps.result"})
    definition = _definition(
        (
            ProjectNode(
                node_id="result",
                literal={"note": "x"},
                as_="result",
                next="inbox",
            ),
            node,
        ),
        returns="result",
        terminal_capability=1,
    )
    assert "is not accepted" in str(extract_row_problem(definition, {}))
    pinned = {(node.contract.name, node.contract.version): node.contract.digest}
    assert extract_row_problem(definition, pinned) is None  # type: ignore[arg-type]
    other = {(node.contract.name, node.contract.version): "sha256:" + "0" * 64}
    assert "must pin" in str(extract_row_problem(definition, other))
