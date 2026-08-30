"""PC-D static laws for semantic pins, aliases, and terminal authority."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    analyze_procedure_v3,
)
from cruxible_client.contracts.procedures.models import (
    CaptureEgressNodeV3,
    ExhaustTapNodeV3,
    GuardNodeV3,
    GuardPredicateV1,
    HaltNodeV3,
    InboxEgressNodeV3,
    MandateSettlementNodeV3,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProjectNodeV3,
    ProposeChangeSetNodeV3,
    ProviderNodeV3,
    RepeatBodyNodeV3,
    RepeatNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
)


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-static-law-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _predicate(alias: str) -> GuardPredicateV1:
    return GuardPredicateV1(
        left=PredicateOperandV1(kind="step", alias=alias),
        operator="eq",
        right=PredicateOperandV1(kind="literal", value=True),
    )


def _definition(
    nodes: Iterable[object],
    *,
    returns: str,
    terminal_capability: int = 3,
    pin_slots: tuple[ProcedurePinSlotV1, ...] = (),
    parameter_contract: ArtifactPin | None = None,
) -> ProcedureDefinitionV3:
    return ProcedureDefinitionV3(
        name="static-laws",
        contract_in=_pin("contract-in", "Contract", "definition-input"),
        contract_out=_pin("contract-out", "Contract", "definition-output"),
        parameter_contract=parameter_contract,
        nodes=tuple(nodes),  # type: ignore[arg-type]
        returns=returns,
        pin_slots=pin_slots,
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=10,
            max_capture_bytes=10_000,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=4_000_000),
            max_provider_calls=20,
            max_capture_bytes=20_000,
            max_items=200,
            max_repeat_attempts=3,
        ),
        terminal_capability=terminal_capability,  # type: ignore[arg-type]
    )


def _kitchen_sink_definition() -> ProcedureDefinitionV3:
    contract_in = _pin("contract-in", "Contract", "node-input")
    contract_out = _pin("contract-out", "Contract", "node-output")
    environment = _pin("environment", "EnvironmentManifest", "runtime")
    provider = _pin("provider", "Provider", "worker")
    return _definition(
        (
            StateTapNodeV3(
                node_id="state",
                query=_pin("query", "QueryDefinition", "accepted-state"),
                parameters={},
                as_="state_rows",
            ),
            SourceNodeV3(
                node_id="source",
                capture_contract=_pin("capture-contract", "CaptureContract", "source-capture"),
                provider=provider,
                request={"state": "$steps.state_rows"},
                as_="source_rows",
            ),
            ExhaustTapNodeV3(
                node_id="exhaust",
                reducer_or_query=_pin("reducer", "Reducer", "journal-reducer"),
                journal_identity="run-exhaust",
                as_="exhaust_rows",
            ),
            ProviderNodeV3(
                node_id="provider",
                provider=provider,
                contract_in=contract_in,
                contract_out=contract_out,
                environment=environment,
                effect_policy=_pin("effect-policy", "EffectPolicy", "provider-effects"),
                input={"source": "$steps.source_rows"},
                as_="provider_rows",
            ),
            TransformNodeV3(
                node_id="transform",
                transform_kind="shape_items",
                contract_in=contract_in,
                contract_out=contract_out,
                spec={
                    "tag": "playbill-transform-shape-items-spec-v1",
                    "items": "$steps.provider_rows",
                    "fields": {},
                    "include_input": True,
                },
                as_="transformed_rows",
            ),
            ProjectNodeV3(
                node_id="project",
                fields={"rows": "$steps.transformed_rows"},
                contract_out=contract_out,
                as_="projected_rows",
            ),
            RepeatNodeV3(
                node_id="repeat",
                max_attempts=2,
                body=(
                    RepeatBodyNodeV3(
                        node_id="body-transform",
                        operation="transform",
                        transform_kind="adapter",
                        contract_in=contract_in,
                        contract_out=contract_out,
                        spec={
                            "tag": "playbill-transform-adapter-spec-v1",
                            "value": {"rows": "$steps.projected_rows"},
                        },
                        as_="body_rows",
                    ),
                    RepeatBodyNodeV3(
                        node_id="body-provider",
                        operation="provider",
                        provider=provider,
                        contract_in=contract_in,
                        contract_out=contract_out,
                        environment=environment,
                        spec={"rows": "$steps.body_rows"},
                        as_="body_result",
                    ),
                ),
                until=_predicate("body_result"),
                as_="repeated_rows",
            ),
            CaptureEgressNodeV3(
                node_id="capture",
                capture_contract=_pin("capture-contract", "CaptureContract", "result-capture"),
                input={"rows": "$steps.repeated_rows"},
            ),
        ),
        returns="repeated_rows",
        parameter_contract=_pin("parameter-contract", "Contract", "parameters"),
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
        ("nodes", 1, "capture_contract"),
        ("nodes", 1, "provider"),
        ("nodes", 2, "reducer_or_query"),
        ("nodes", 3, "provider"),
        ("nodes", 3, "contract_in"),
        ("nodes", 3, "contract_out"),
        ("nodes", 3, "environment"),
        ("nodes", 3, "effect_policy"),
        ("nodes", 4, "contract_in"),
        ("nodes", 4, "contract_out"),
        ("nodes", 5, "contract_out"),
        ("nodes", 6, "body", 0, "contract_in"),
        ("nodes", 6, "body", 0, "contract_out"),
        ("nodes", 6, "body", 1, "provider"),
        ("nodes", 6, "body", 1, "contract_in"),
        ("nodes", 6, "body", 1, "contract_out"),
        ("nodes", 6, "body", 1, "environment"),
        ("nodes", 7, "capture_contract"),
    ),
)
def test_semantic_pin_expectations_cover_every_exact_field(
    path: tuple[object, ...],
) -> None:
    payload = _kitchen_sink_definition().model_dump(mode="json", by_alias=True)
    wrong = _pin("wrong", "WrongArtifact", "wrong").model_dump(mode="json")
    _replace_path(payload, path, wrong)

    with pytest.raises(ValidationError, match="requires role="):
        ProcedureDefinitionV3.model_validate(payload)


def test_semantic_pin_expectations_validate_slot_declarations_not_only_bindings() -> None:
    wrong_slot = ProcedurePinSlotV1(
        slot_name="query",
        pin_role="provider",
        artifact_kind="Provider",
        interface_digest=_digest("provider-interface"),
    )
    with pytest.raises(ValidationError, match="slot 'query'.*requires role='query'"):
        _definition(
            (
                StateTapNodeV3(
                    node_id="state",
                    query=ProcedurePinSlotRefV1(slot_name="query"),
                    as_="rows",
                ),
            ),
            returns="rows",
            pin_slots=(wrong_slot,),
        )


def test_mandate_terminal_pin_expectations_are_nominal() -> None:
    definition = _definition(
        (
            ProjectNodeV3(
                node_id="project",
                fields={"ready": True},
                contract_out=_pin("contract-out", "Contract", "result"),
                as_="result",
            ),
            MandateSettlementNodeV3(
                node_id="settle",
                mandate=_pin("mandate", "StandingMandate", "release"),
                target_law=_pin("target-law", "Policy", "release-law"),
                input="$steps.result",
            ),
        ),
        returns="result",
    )
    for field in ("mandate", "target_law"):
        payload = definition.model_dump(mode="json", by_alias=True)
        payload["nodes"][1][field] = _pin("wrong", "WrongArtifact", field).model_dump(mode="json")
        with pytest.raises(ValidationError, match="requires role="):
            ProcedureDefinitionV3.model_validate(payload)


def test_branch_join_tracks_must_availability_separately_from_may_reachability() -> None:
    contract_out = _pin("contract-out", "Contract", "result")
    definition = _definition(
        (
            StateTapNodeV3(
                node_id="state",
                query=_pin("query", "QueryDefinition", "state"),
                as_="rows",
            ),
            GuardNodeV3(
                node_id="branch",
                predicate=_predicate("rows"),
                on_true="hot",
                on_false="join",
                refusal_code="branch.false",
                message="Take the cold path.",
            ),
            ProjectNodeV3(
                node_id="hot",
                fields={"hot": True},
                contract_out=contract_out,
                as_="hot_rows",
                next="join",
            ),
            ProjectNodeV3(
                node_id="join",
                fields={"status": "joined"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
    )

    graph = analyze_procedure_v3(definition)
    assert graph.available_aliases["join"] == frozenset({"rows"})
    assert graph.reachable_aliases["join"] == frozenset({"rows", "hot_rows"})

    payload = definition.model_dump(mode="json", by_alias=True)
    payload["nodes"][3]["fields"] = {"hot": "$steps.hot_rows"}
    with pytest.raises(ProcedureGraphFormatError, match="not produced on every path"):
        ProcedureDefinitionV3.model_validate(payload)


def test_guard_at_a_join_cannot_read_an_alias_from_only_one_branch() -> None:
    contract_out = _pin("contract-out", "Contract", "result")
    with pytest.raises(ProcedureGraphFormatError, match="not produced on every path"):
        _definition(
            (
                StateTapNodeV3(
                    node_id="state",
                    query=_pin("query", "QueryDefinition", "state"),
                    as_="rows",
                ),
                GuardNodeV3(
                    node_id="branch",
                    predicate=_predicate("rows"),
                    on_true="hot",
                    on_false="join",
                    refusal_code="branch.false",
                    message="Take the cold path.",
                ),
                ProjectNodeV3(
                    node_id="hot",
                    fields={"hot": True},
                    contract_out=contract_out,
                    as_="hot_rows",
                    next="join",
                ),
                GuardNodeV3(
                    node_id="join",
                    predicate=_predicate("hot_rows"),
                    on_false="$abort",
                    refusal_code="hot.missing",
                    message="Hot data is required.",
                ),
                ProjectNodeV3(
                    node_id="result",
                    fields={"status": "joined"},
                    contract_out=contract_out,
                    as_="result",
                ),
            ),
            returns="result",
        )


def test_trailing_halt_is_a_terminal_graph_leaf_without_edges() -> None:
    definition = _definition(
        (
            StateTapNodeV3(
                node_id="read",
                query=_pin("query", "QueryDefinition", "halt-input"),
                as_="rows",
            ),
            HaltNodeV3(node_id="stop", reason="No work remains."),
        ),
        returns="rows",
        terminal_capability=1,
    )

    graph = analyze_procedure_v3(definition)

    assert graph.edges["read"] == {"next": "stop"}
    assert graph.edges["stop"] == {}


@pytest.mark.parametrize(
    "path",
    (
        ("nodes", 0, "parameters"),
        ("nodes", 1, "request"),
        ("nodes", 3, "input"),
        ("nodes", 4, "spec", "items"),
        ("nodes", 5, "fields"),
        ("nodes", 6, "body", 0, "spec", "value"),
        ("nodes", 7, "input"),
    ),
)
def test_structured_step_references_are_checked_in_every_runtime_template(
    path: tuple[object, ...],
) -> None:
    payload = _kitchen_sink_definition().model_dump(mode="json", by_alias=True)
    _replace_path(payload, path, {"missing": "$steps.missing"})
    with pytest.raises(ProcedureGraphFormatError, match="missing"):
        ProcedureDefinitionV3.model_validate(payload)


def test_transform_specs_are_tagged_closed_and_kind_matched() -> None:
    definition = _kitchen_sink_definition()
    transform = definition.nodes[4]
    assert isinstance(transform, TransformNodeV3)
    raw = transform.model_dump(mode="json", by_alias=True)

    untagged = {**raw, "spec": {"items": [], "fields": {}}}
    with pytest.raises(ValueError, match="tag"):
        TransformNodeV3.model_validate(untagged)

    mismatched = {
        **raw,
        "spec": {"tag": "playbill-transform-aggregate-items-spec-v1", "items": []},
    }
    with pytest.raises(ValueError, match="does not match"):
        TransformNodeV3.model_validate(mismatched)

    extra = raw.copy()
    extra["spec"] = {**raw["spec"], "undeclared": True}
    with pytest.raises(ValueError, match="Extra inputs"):
        TransformNodeV3.model_validate(extra)


@pytest.mark.parametrize(
    ("terminal", "capability", "accepted"),
    (
        (InboxEgressNodeV3(node_id="inbox", input="$steps.result"), 1, True),
        (
            ProposeChangeSetNodeV3(
                node_id="propose",
                candidate_templates=({"input": "$steps.result"},),
            ),
            1,
            False,
        ),
        (
            ProposeChangeSetNodeV3(
                node_id="propose",
                candidate_templates=({"input": "$steps.result"},),
            ),
            2,
            True,
        ),
        (
            MandateSettlementNodeV3(
                node_id="settle",
                mandate=_pin("mandate", "StandingMandate", "release"),
                target_law=_pin("target-law", "Policy", "release-law"),
                input="$steps.result",
            ),
            2,
            False,
        ),
        (
            MandateSettlementNodeV3(
                node_id="settle",
                mandate=_pin("mandate", "StandingMandate", "release"),
                target_law=_pin("target-law", "Policy", "release-law"),
                input="$steps.result",
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
        ProjectNodeV3(
            node_id="project",
            fields={"ready": True},
            contract_out=_pin("contract-out", "Contract", "result"),
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
    contract_in = _pin("contract-in", "Contract", "provider-input")
    contract_out = _pin("contract-out", "Contract", "provider-output")
    definition = _definition(
        (
            ProviderNodeV3(
                node_id="provider",
                provider=_pin("provider", "Provider", "external"),
                contract_in=contract_in,
                contract_out=contract_out,
                environment=_pin("environment", "EnvironmentManifest", "runtime"),
                effect_policy=_pin("effect-policy", "EffectPolicy", "external-effects"),
                input={},
                as_="provider_result",
            ),
            ProjectNodeV3(
                node_id="project",
                fields={"result": "$steps.provider_result"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        terminal_capability=1,
    )
    assert definition.terminal_capability == 1


@pytest.mark.parametrize(
    "terminal",
    (
        InboxEgressNodeV3(node_id="inbox", input="$steps.missing"),
        ProposeChangeSetNodeV3(
            node_id="propose",
            candidate_templates=({"input": "$steps.missing"},),
        ),
        MandateSettlementNodeV3(
            node_id="settle",
            mandate=_pin("mandate", "StandingMandate", "release"),
            target_law=_pin("target-law", "Policy", "release-law"),
            input="$steps.missing",
        ),
    ),
)
def test_all_terminal_runtime_templates_receive_structured_reference_checks(
    terminal: object,
) -> None:
    with pytest.raises(ProcedureGraphFormatError, match="missing"):
        _definition(
            (
                ProjectNodeV3(
                    node_id="project",
                    fields={"ready": True},
                    contract_out=_pin("contract-out", "Contract", "result"),
                    as_="result",
                ),
                terminal,
            ),
            returns="result",
        )
