"""Authored Procedure refusals and kit lowering-equivalence corpus."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.authoring.inputs import ProcedureInput, lower_authoring_input
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    parse_procedure,
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.authored import (
    ProcedureSpec,
    ProcedureSpecError,
    lower_procedure_spec,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.decompile import decompile_procedure
from cruxible_client.contracts.procedures.models import ProcedureDefinition
from tests.support.procedures import named


def _artifact(definition):
    from tests.support.procedures import procedure_artifact

    return procedure_artifact(definition, activation_policy="drain")


def spec(**changes):
    data = dict(
        procedure="test.authored",
        input={"value": "integer", "flag": "bool"},
        returns="result",
        steps=[{"project": "result", "fields": {"value": "$input.value"}}],
    )
    data.update(changes)
    return ProcedureSpec.model_validate(data)


def test_typed_extra_forbidden_and_no_numeric_coercion():
    with pytest.raises(ValidationError):
        spec(
            slots={"compute": {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}},
            steps=[{"call": "result", "slot": "compute", "input": {}, "typo": 1}],
        )
    with pytest.raises(ValidationError):
        spec(budget={"provider_calls": True})
    with pytest.raises(ValidationError):
        spec(steps=[{"project": "result", "fields": {}, "tap": "other", "query": "q"}])


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        ("input.value >> 0", "procedure_expression_invalid"),
        ("input.vlaue > 0", "procedure_ref_unknown"),
        ("missing.value > 0", "procedure_ref_unknown"),
        ('input.value == "0"', "procedure_operand_type_mismatch"),
        ("input.flag > false", "procedure_operand_type_mismatch"),
        ("input.value", "procedure_operand_type_mismatch"),
        ("param.missing > 0", "procedure_ref_unknown"),
    ],
)
def test_refusal_table(expression, code):
    authored = spec(
        steps=[
            {"guard": expression, "else": "halt(stop)"},
            {"project": "result", "fields": {"value": "$input.value"}},
        ]
    )
    with pytest.raises(ProcedureSpecError) as caught:
        lower_procedure_spec(authored)
    assert caught.value.code == "cruxible.authoring." + code
    assert caught.value.field_path == "$.steps[0].guard"
    if code == "procedure_expression_invalid":
        assert "column" in str(caught.value)
    if "vlaue" in expression:
        assert "value" in str(caught.value) and "Did you mean" in str(caught.value)


@pytest.mark.parametrize("verb", ["guard", "repeat"])
def test_guard_and_until_limit_length_and_report_nested_expression_paths(verb):
    from cruxible_client.contracts.procedures.authored import GuardStep, RepeatStep
    from cruxible_client.contracts.procedures.expressions import MAX_EXPRESSION_LENGTH

    model = GuardStep if verb == "guard" else RepeatStep
    field = "guard" if verb == "guard" else "until"
    data = {} if verb == "guard" else {"repeat": "loop", "max_attempts": 1, "body": []}
    with pytest.raises(ValidationError) as caught:
        model.model_validate({**data, field: " " * (MAX_EXPRESSION_LENGTH + 1)})
    assert caught.value.errors()[0]["type"] == "string_too_long"
    assert caught.value.errors()[0]["loc"] == (field,)

    authored = spec(steps=[{**data, field: "not " * 2000 + "true"}])
    with pytest.raises(ProcedureSpecError) as refusal:
        lower_procedure_spec(authored)
    assert refusal.value.code == "cruxible.authoring.procedure_expression_invalid"
    assert refusal.value.field_path == "$.steps[0]." + field
    assert "column" in str(refusal.value)


def test_shared_authoring_envelope():
    authored = spec()
    wrapped = ProcedureInput(kind="procedure", spec=authored)
    flat = ProcedureInput.model_validate(
        {"kind": "procedure", **authored.model_dump(mode="json", by_alias=True)}
    )
    assert flat == wrapped
    from pydantic import TypeAdapter

    from cruxible_client.contracts.authoring.inputs import AuthoringInput, ChangeSetInput

    bare = authored.model_dump(mode="json", by_alias=True)
    assert TypeAdapter(AuthoringInput).validate_python(bare) == wrapped
    assert ChangeSetInput(kind="change_set", members=(bare,)).members == (wrapped,)
    payload = lower_authoring_input(wrapped)
    assert payload.definition["procedure_spec"]["procedure"] == authored.procedure


def test_authored_spec_keeps_a_typed_schema_in_both_wire_modes():
    request = ProcedureSpec.model_json_schema(mode="validation")
    response = ProcedureSpec.model_json_schema(mode="serialization")
    assert response == request
    assert request["additionalProperties"] is False
    assert {"procedure", "input", "steps"} <= set(request["properties"])
    assert request["properties"]["steps"]["type"] == "array"


CORPUS = json.loads(
    (Path(__file__).parents[1] / "fixtures/procedures/security-p1.json").read_text()
)


OPERATIONS = json.loads(
    (Path(__file__).parents[1] / "fixtures/procedures/security-p1-operations.json").read_text()
)


def _corpus_resolver(original):
    """Independent schema metadata for unbound operation signatures, not providers."""
    by_request = {entry["signature"]["in"]["name"]: entry for entry in OPERATIONS.values()}
    sources = {node.as_: node for node in original.definition.nodes if node.kind == "source"}

    def resolve(kind, value):
        if kind == "slot":
            return value
        signature = original.definition.slots[value.slot]
        entry = by_request[signature.in_.name]
        schema = (
            (entry.get("source_view") or entry["output"])
            if hasattr(value, "source")
            else entry["output"]
        )
        return {
            "output": ContractSchema.model_validate(schema).fields,
            "input": None,
            "request_defaults": sources[value.source].request if hasattr(value, "source") else {},
        }

    return resolve


@pytest.mark.parametrize("filename", sorted(CORPUS))
def test_every_kit_procedure_lowers_to_stored_bytes(filename):
    original = ProcedureArtifact.model_validate(CORPUS[filename])
    authored = decompile_procedure(original)
    lowered = lower_procedure_spec(authored, resolve=_corpus_resolver(original))
    definition = ProcedureDefinition.model_validate(lowered.definition)
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest

    actual = ProcedureArtifact(
        identity=original.identity,
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=lower_authoring_input(lowered).owned_contracts,
        activation_policy=lowered.activation_policy,
    )
    # The stored expectation was migrated structurally from independent fields,
    # never generated by the decompiler or the authored lowerer under test.
    assert actual.definition_digest == original.definition_digest
    assert render_procedure(actual) == render_procedure(original)
    rendered = render_procedure(actual)
    parsed = parse_procedure(rendered, path="procedures/" + original.identity.name + ".json")
    assert render_procedure(parsed) == rendered
    assert ProcedureArtifact.model_validate(json.loads(rendered)) == parsed
    assert procedure_artifact_digest(parsed) == procedure_artifact_digest(actual)


def _inventory_import_worked_example():
    original = ProcedureArtifact.model_validate(CORPUS["security.inventory-import.json"])
    authored = spec(
        procedure="security.inventory-import",
        description=original.definition.description,
        slots={
            "inventory": {
                "in": "fixture.workspace.file.request@1",
                "out": "fixture.workspace.file.result@1",
                "effect": "external_read",
            },
            "parse": {
                "in": "fixture.security.runtime-inventory.request@1",
                "out": "fixture.security.runtime-inventory.result@1",
                "effect": "pure",
            },
        },
        input={"scope": "string"},
        input_contract="security-inventory-import-input",
        output_contract="security-runtime-inventory-summary",
        returns={"from": "parse", "omit": ["items"]},
        activation="drain",
        budget={"wall_clock": "PT2M", "provider_calls": 2, "capture_bytes": "1MiB"},
        caps={
            "wall_clock": "PT5M",
            "provider_calls": 2,
            "capture_bytes": "1MiB",
            "items": 5000,
            "repeat_attempts": 1,
        },
        steps=[
            {
                "source": "inventory",
                "contract": "security.runtime-inventory-v1",
                "slot": "inventory",
                "request": {
                    "logical_source": "security.inventory",
                    "relative_path": "inventory.json",
                },
            },
            {
                "tap": "held",
                "query": "security.inventory_index",
                "params": {"scope": "$input.scope"},
            },
            {
                "tap": "runs",
                "query": "security.inventory_runs",
                "params": {"scope": "$input.scope"},
            },
            {
                "tap": "patches",
                "query": "security.inventory_patches",
                "params": {"scope": "$input.scope"},
            },
            {
                "call": "parse",
                "slot": "parse",
                "input": {
                    "inventory_text": "$inventory.content.text",
                    "held": "$held.result.rows",
                    "runs": "$runs.result.rows",
                    "patches": "$patches.result.rows",
                    "truncated": {
                        "held": "$held.truncated",
                        "runs": "$runs.truncated",
                        "patches": "$patches.truncated",
                    },
                    "scope": "$input.scope",
                },
            },
            {
                "guard": "parse.mine > 0",
                "id": "mine",
                "else": "halt(other_scope)",
                "else_id": "other",
                "refusal_code": "other_scope",
                "message": "The landed inventory is another scope's.",
            },
            {
                "guard": "parse.proposes > 0",
                "id": "changed",
                "else": "halt(no_change)",
                "else_id": "nochange",
                "refusal_code": "no_change",
                "message": "The scan changes nothing inventory holds.",
            },
            {"propose": "$parse.items"},
        ],
    )

    return original, authored, _corpus_resolver(original)


def test_handwritten_inventory_import_matches_the_stored_definition_digest():
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest

    original, authored, resolve = _inventory_import_worked_example()
    lowered = lower_procedure_spec(authored, resolve=resolve)
    contracts = {
        contract.identity.name: contract.contract_schema for contract in original.owned_contracts
    }
    for contract in lowered.contracts:
        assert (
            ContractSchema(
                fields=contract.fields,
                description=contract.description,
                allow_extra=contract.allow_extra,
            )
            == contracts[contract.name]
        )
    actual = ProcedureDefinition.model_validate(lowered.definition)
    # Independent handwritten steps, literal signatures and diagnostics. Neither
    # decompile nor a graph normalizer constructs the authored side.
    assert compute_procedure_definition_digest(actual).tagged == original.definition_digest


def test_inventory_import_worked_example_has_eleven_nodes_and_typed_tap_values():
    _original, authored, resolve = _inventory_import_worked_example()
    # K1's provider build will consume the new tap view. Keep its lowering check
    # separate from equality with the stored provider's row/truncation arguments.
    call_index = next(i for i, step in enumerate(authored.steps) if hasattr(step, "call"))
    authored = authored.model_copy(
        update={
            "steps": tuple(
                step.model_copy(
                    update={
                        "input": {
                            "inventory_text": "$inventory.content.text",
                            "scope": "$input.scope",
                            "held": "$held",
                            "runs": "$runs",
                            "patches": "$patches",
                        }
                    }
                )
                if i == call_index
                else step
                for i, step in enumerate(authored.steps)
            )
        }
    )
    payload = lower_authoring_input(ProcedureInput(kind="procedure", spec=authored))
    assert payload.definition["procedure_spec"]["procedure"] == "security.inventory-import"
    lowered = lower_procedure_spec(authored, resolve=resolve)
    assert len(lowered.definition["nodes"]) == 11
    assert [node["kind"] for node in lowered.definition["nodes"]] == [
        "source",
        "state_tap",
        "state_tap",
        "state_tap",
        "call",
        "project",
        "guard",
        "guard",
        "propose_change_set",
        "halt",
        "halt",
    ]
    lowered_call = lowered.definition["nodes"][4]
    assert lowered_call["input"]["held"] == {
        "rows": "$steps.held.result.rows",
        "truncated": "$steps.held.truncated",
    }
    assert "items" not in lowered.definition["nodes"][5]["fields"]
    assert lowered.definition["nodes"][8]["result"] == "$steps.summary"
    assert lowered.definition["nodes"][6]["refusal_code"] == "other_scope"
    assert lowered.definition["budget"]["max_capture_bytes"] == 1048576


def test_stored_v3_keeps_template_data_and_literal_null():
    original = ProcedureArtifact.model_validate(CORPUS["security.credential-acquire.json"])
    from cruxible_client.contracts.procedures.models import (
        GuardNode,
        GuardPredicate,
        HaltNode,
        PredicateOperand,
    )
    from cruxible_client.contracts.procedures.wire_v3 import compact_procedure

    with pytest.raises(ValidationError, match="abort requires"):
        GuardNode(
            node_id="gate",
            predicate=GuardPredicate(
                left=PredicateOperand(kind="literal", value=True),
                operator="eq",
                right=PredicateOperand(kind="literal", value=True),
            ),
        )
    halt = HaltNode(node_id="stop", reason="done", message="Nothing remains")
    assert compact_procedure(halt) == {
        "kind": "halt",
        "id": "stop",
        "reason": "done",
        "message": "Nothing remains",
    }
    scalar = compact_procedure(
        GuardPredicate(
            left=PredicateOperand(kind="literal", value=None),
            operator="eq",
            right=PredicateOperand(kind="literal", value=None),
        )
    )
    assert scalar == {"left": None, "operator": "eq", "right": None}
    project = next(node for node in original.definition.nodes if node.kind == "project")
    data = {"tag": "my-data", "node_id": "my-id", "value": None, "default": False}
    compact = compact_procedure(project.model_copy(update={"fields": data}))
    assert compact["fields"] == data
    assert "node_id" not in compact


def test_decompile_shows_budget_durations_compactly_without_moving_stored_bytes():
    from cruxible_client.contracts.temporal import (
        format_iso_duration_microseconds,
        parse_iso_duration_microseconds,
    )

    original = ProcedureArtifact.model_validate(CORPUS["security.inventory-import.json"])
    authored = decompile_procedure(original)
    assert authored.budget.wall_clock == "PT120S"
    assert authored.caps is not None and authored.caps.wall_clock == "PT300S"
    # Display only: each text parses back to the stored microseconds (the corpus
    # round trip above keeps every Procedure's stored bytes and digest).
    assert parse_iso_duration_microseconds(authored.budget.wall_clock) == (
        original.definition.budget.wall_clock.microseconds
    )
    for value, text in [
        (0, "PT0S"),
        (1, "PT0.000001S"),
        (1_500_000, "PT1.5S"),
        (120_000_000, "PT120S"),
        (86_400_123_400, "PT86400.1234S"),
    ]:
        assert format_iso_duration_microseconds(value) == text
        assert parse_iso_duration_microseconds(text) == value
    with pytest.raises(ValueError, match="nonnegative"):
        format_iso_duration_microseconds(-1)


def test_decompile_inventory_recognizes_return_and_halt_patterns():
    original = ProcedureArtifact.model_validate(CORPUS["security.inventory-import.json"])
    authored = decompile_procedure(original)
    assert len(authored.steps) == 8
    assert authored.returns.id == "summary"
    assert authored.steps[5].else_ == "halt(other_scope)"
    assert authored.steps[6].else_ == "halt(no_change)"


@pytest.mark.parametrize("node_kind", ["guard", "repeat"])
@pytest.mark.parametrize("predicate_shape", ["empty_path", "single_all", "single_any"])
def test_decompile_preserves_predicates_outside_lossless_expr(node_kind, predicate_shape):
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import GuardPredicate, PredicateOperand
    from tests.test_procedures.test_procedure_artifacts import _definition
    from tests.test_procedures.test_procedure_execution import _repeat_transform_procedure

    leaf = GuardPredicate(
        left=PredicateOperand(
            kind="step",
            alias="rows" if node_kind == "guard" else "shaped",
            path=() if predicate_shape == "empty_path" else ("flag",),
        ),
        operator="eq",
        right=PredicateOperand(kind="literal", value=True),
    )
    predicate = (
        leaf
        if predicate_shape == "empty_path"
        else GuardPredicate(**{"all_of" if predicate_shape == "single_all" else "any_of": (leaf,)})
    )
    if node_kind == "repeat":
        from cruxible_core.procedures.reference_binding import authored_procedure

        original = authored_procedure(_repeat_transform_procedure())
        repeat = original.definition.nodes[0].model_copy(update={"until": predicate})
        definition = original.definition.model_copy(update={"nodes": (repeat,)})
    else:
        from cruxible_client.contracts.procedures.models import GuardNode, HaltNode

        base = _definition()
        guard = GuardNode(node_id="gate", predicate=predicate, on_false="stop")
        definition = base.model_copy(
            update={
                "nodes": (
                    base.nodes[0],
                    guard,
                    base.nodes[1],
                    HaltNode(node_id="stop", reason="no"),
                )
            }
        )
        original = _artifact(definition)
    original = original.model_copy(update={"definition": definition})
    authored = decompile_procedure(original)
    explicit = next(step.explicit for step in authored.steps if hasattr(step, "explicit"))
    assert explicit == next(node for node in definition.nodes if node.kind == node_kind)
    if node_kind == "guard":
        assert any(getattr(step, "halt", None) == "no" for step in authored.steps)

    lowered = lower_procedure_spec(authored)
    actual = ProcedureDefinition.model_validate(lowered.definition)
    assert compute_procedure_definition_digest(actual) == compute_procedure_definition_digest(
        definition
    )


def test_decompile_recognizes_the_generated_tap_view_and_lowers_it_back():
    original = ProcedureArtifact.model_validate(CORPUS["security.inventory-import.json"])
    call = next(node for node in original.definition.nodes if node.kind == "call")
    view = {"rows": "$steps.held.result.rows", "truncated": "$steps.held.truncated"}
    definition = original.definition.model_copy(
        update={
            "nodes": tuple(
                node.model_copy(update={"input": {**call.input, "held": view}})
                if node is call
                else node
                for node in original.definition.nodes
            )
        }
    )
    authored = decompile_procedure(original.model_copy(update={"definition": definition}))
    authored_call = next(step for step in authored.steps if hasattr(step, "call"))
    assert authored_call.input["held"] == "$held"
    lowered = lower_procedure_spec(authored)
    lowered_call = next(node for node in lowered.definition["nodes"] if node["kind"] == "call")
    assert lowered_call["input"]["held"] == view


def test_field_descriptors_forbid_unknown_fields_and_coercion():
    with pytest.raises(ValidationError):
        spec(input={"value": {"type": "integer", "optinal": True}})
    with pytest.raises(ValidationError):
        spec(input={"value": {"type": "integer", "optional": "true"}})
    with pytest.raises(ValidationError):
        spec(
            input={
                "value": {"type": "list", "item_fields": {"nested": {"type": "integer", "typo": 1}}}
            }
        )


def test_authored_spec_prepares_through_existing_graph_laws(tmp_path):
    from tests.test_authoring.test_authoring_procedures import TIMESTAMP, _coordinator

    coordinator, actor = _coordinator(tmp_path)
    authored = spec(
        steps=[
            {
                "id": "positive",
                "guard": "input.value > 0",
                "else": 'halt(empty, "Nothing remains")',
            },
            {"project": "result", "fields": {"value": "$input.value"}},
        ]
    )
    result = coordinator.compile(
        actor=actor,
        payload=lower_authoring_input(ProcedureInput(kind="procedure", spec=authored)),
        canonical_timestamp=TIMESTAMP,
    )
    assert result.verdict == "passed", result.frontier
    pending = coordinator.list_pending(actor=actor).intents[-1]
    assert pending.semantic_identity == "Procedure:test.authored"
    assert pending.payload.definition["procedure_spec"]["steps"][0]["id"] == "positive"


def test_authored_graph_refusal_names_step_id(tmp_path):
    from tests.test_authoring.test_authoring_procedures import TIMESTAMP, _coordinator

    coordinator, actor = _coordinator(tmp_path)
    authored = spec(
        steps=[{"project": "result", "fields": {"value": "$input.value"}, "next": "result"}]
    )
    result = coordinator.compile(
        actor=actor,
        payload=lower_authoring_input(ProcedureInput(kind="procedure", spec=authored)),
        canonical_timestamp=TIMESTAMP,
    )
    assert result.verdict == "refused"
    assert any("result" in diagnostic.message for diagnostic in result.frontier.diagnostics)


@pytest.mark.parametrize(
    ("step", "field", "candidate"),
    [
        (
            {"claim": "read", "claim_type": "semantic.alais", "subject": "test"},
            "claim_type",
            "ClaimType:semantic.alias",
        ),
        (
            {"invoke": "stored.procc", "input": {"status": "open"}},
            "invoke",
            "Procedure:stored.proc",
        ),
        (
            {
                "source": "read",
                "contract": "security.invenotry",
                "slot": "read",
                "request": {},
            },
            "contract",
            "CaptureContract:security.inventory",
        ),
    ],
)
def test_named_schema_refusals_keep_authored_path_and_nearest_candidate(
    tmp_path, step, field, candidate
):
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.captures import (
        capture_contract_path,
        foreign_source_capture_contract,
        render_capture_contract,
    )
    from cruxible_client.contracts.procedures.artifacts import procedure_path
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._support import initialize_local
    from tests.test_authoring.test_authoring_procedures import TIMESTAMP
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_procedure_run_surface import _slotless_procedure

    instance, owner = initialize_local(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    capture = foreign_source_capture_contract("security.inventory").model_copy(
        update={"identity": ArtifactIdentity(kind="CaptureContract", name="security.inventory")}
    )
    tree[capture_contract_path(capture.identity.name)] = render_capture_contract(capture)
    procedure = _slotless_procedure("stored.proc").procedure
    tree[procedure_path(procedure.identity.name)] = render_procedure(procedure)
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="named-candidates")

    authored = spec(
        slots={
            "read": {
                "in": "workspace.file.request@1",
                "out": "workspace.file.result@1",
                "effect": "external_read",
            }
        }
        if "source" in step
        else {},
        steps=[step, {"project": "result", "fields": {"value": 1}}],
    )
    result = AuthoringIntentCoordinator.for_instance(instance).compile(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=lower_authoring_input(ProcedureInput(kind="procedure", spec=authored)),
        canonical_timestamp=TIMESTAMP,
    )
    assert result.verdict == "refused"
    refusal = result.frontier.diagnostics[0]
    assert refusal.code == "cruxible.authoring.artifact_reference_unresolved"
    assert refusal.offending_element == "$.steps[0]." + field
    assert candidate in refusal.message


def test_template_prefixes_are_step_names_and_explicit_input_alias_survives():
    authored = spec(
        steps=[
            {"project": "inputly", "fields": {"value": "$input.value"}},
            {"project": "result", "fields": {"value": "$inputly.value"}},
        ]
    )
    lowered = lower_procedure_spec(authored)
    assert lowered.definition["nodes"][1]["fields"]["value"] == "$steps.inputly.value"
    authored = spec(
        steps=[
            {"project": "input", "fields": {"value": "$input.value"}},
            {"project": "result", "fields": {"value": "$steps.input.value"}},
        ]
    )
    lowered = lower_procedure_spec(authored)
    assert lowered.definition["nodes"][1]["fields"]["value"] == "$steps.input.value"


def test_spec_dump_preserves_else_with_and_without_aliases():
    authored = spec(
        steps=[
            {"guard": "input.value > 0", "else": "halt(empty)"},
            {"project": "result", "fields": {"value": "$input.value"}},
        ]
    )
    for by_alias in (True, False):
        assert (
            ProcedureSpec.model_validate(authored.model_dump(mode="json", by_alias=by_alias))
            == authored
        )


def test_decompile_does_not_fold_a_halt_with_a_shared_explicit_edge():
    from tests.test_procedures.test_procedure_artifacts import _layout_definition

    definition = _layout_definition(halt_before_return=False)
    definition = definition.model_copy(
        update={
            "nodes": tuple(
                node.model_copy(update={"reason": "empty"})
                if node.kind == "halt"
                else node.model_copy(update={"next": "stop"})
                if node.kind == "project"
                else node
                for node in definition.nodes
            )
        }
    )
    original = _artifact(definition)
    authored = decompile_procedure(original)
    # Format 6 excludes guard halts from implicit fallthrough. This explicit
    # edge deliberately shares the halt with the guard's false arm.
    assert any(getattr(step, "halt", None) is not None for step in authored.steps)
    assert any(getattr(step, "else_", None) == "stop" for step in authored.steps)
    from cruxible_client.contracts.procedures.graph import analyze_procedure

    assert analyze_procedure(original.definition).edges["result"] == {"next": "stop"}
    content = render_procedure(original)
    assert json.loads(content)["definition"]["nodes"][2]["next"] == "stop"
    restored = parse_procedure(content, path="procedures/" + original.identity.name + ".json")
    assert (
        analyze_procedure(restored.definition).edges == analyze_procedure(original.definition).edges
    )


def test_lowered_halt_ids_do_not_collide_or_exceed_graph_limits():
    long_id = "a" * 128
    authored = spec(
        steps=[
            {"id": long_id, "guard": "input.value > 0", "else": "halt(empty)"},
            {"project": "result", "fields": {"value": "$input.value"}},
            {"halt": "other", "id": long_id[:123] + "_halt"},
        ]
    )
    lowered = lower_procedure_spec(authored)
    ids = [node["node_id"] for node in lowered.definition["nodes"]]
    assert len(ids) == len(set(ids))
    assert all(len(id_) <= 128 for id_ in ids)


def test_v3_retains_the_transform_spec_union_discriminator():
    from cruxible_client.contracts.procedures.models import TransformAdapterSpec, TransformNode
    from tests.test_procedures.test_procedure_artifacts import _definition

    definition = _definition()
    transform = TransformNode(
        node_id="result",
        transform_kind="adapter",
        contract_in=named(definition.contract_in),
        contract_out=named(definition.contract_out),
        spec=TransformAdapterSpec(value={}),
        **{"as": "result"},
    )
    artifact = _artifact(definition.model_copy(update={"nodes": (transform,), "returns": "result"}))
    content = render_procedure(artifact)
    assert (
        json.loads(content)["definition"]["nodes"][0]["spec"]["tag"]
        == "playbill-transform-adapter-spec-v1"
    )
    assert (
        parse_procedure(content, path="procedures/" + artifact.identity.name + ".json") == artifact
    )


def test_literal_null_operands_round_trip():
    from cruxible_client.contracts.procedures.models import GuardPredicate, PredicateOperand
    from tests.support.procedures import procedure_artifact
    from tests.test_procedures.test_procedure_artifacts import _layout_definition

    definition = _layout_definition(halt_before_return=False)
    predicate = GuardPredicate(
        left=PredicateOperand(kind="literal", value=None),
        operator="eq",
        right=PredicateOperand(kind="literal", value=None),
    )
    definition = definition.model_copy(
        update={
            "nodes": tuple(
                node.model_copy(update={"predicate": predicate}) if node.kind == "guard" else node
                for node in definition.nodes
            )
        }
    )
    current = procedure_artifact(definition)
    assert (
        parse_procedure(
            render_procedure(current), path="procedures/" + current.identity.name + ".json"
        )
        == current
    )


@pytest.mark.parametrize("change", ["guard_diagnostics", "halt_message"])
def test_omitted_guard_diagnostics_and_halt_messages_round_trip(change):
    from tests.support.procedures import procedure_artifact
    from tests.test_procedures.test_procedure_artifacts import _layout_definition

    definition = _layout_definition(halt_before_return=False)
    definition = definition.model_copy(
        update={
            "nodes": tuple(
                node.model_copy(update={"refusal_code": None, "message": None})
                if change == "guard_diagnostics" and node.kind == "guard"
                else node.model_copy(update={"message": "Nothing remains"})
                if change == "halt_message" and node.kind == "halt"
                else node
                for node in definition.nodes
            )
        }
    )
    current = procedure_artifact(definition)
    assert (
        parse_procedure(
            render_procedure(current), path="procedures/" + current.identity.name + ".json"
        )
        == current
    )


def test_parameter_operands_and_reserved_looking_step_ids_have_distinct_scopes():
    authored = spec(
        params={"limit": "integer"},
        steps=[
            {"project": "params", "fields": {"value": "$input.value"}},
            {"guard": "params.value >= param.limit", "else": "halt(below_limit)"},
            {"project": "result", "fields": {"value": "$params.value"}},
        ],
    )
    lowered = lower_procedure_spec(authored)
    assert lowered.definition["nodes"][2]["fields"]["value"] == "$steps.params.value"
    assert lowered.definition["nodes"][1]["predicate"]["left"]["alias"] == "params"
    assert lowered.definition["nodes"][1]["predicate"]["right"]["parameter_name"] == "limit"


def test_parameter_name_can_match_a_collection_step_alias():
    authored = spec(
        input={"subject": "string"},
        params={"limit": "integer"},
        steps=[
            {
                "claim": "limit",
                "claim_type": "catalog.entry.value",
                "subject": "$input.subject",
                "all": 10,
            },
            {"guard": "param.limit > 0", "else": "halt(empty)"},
            {"project": "result", "fields": {"ok": True}},
        ],
    )
    lowered = lower_procedure_spec(
        authored,
        resolve=lambda kind, step: {
            "output": {"value": PropertySchema(type="integer")},
            "subject_kind": "catalog.entry",
        },
    )
    assert lowered.definition["nodes"][1]["predicate"]["left"]["parameter_name"] == "limit"
    assert lowered.definition["nodes"][0]["cardinality"] == "all"


def test_measurement_node_coordinates_are_not_graph_node_ids():
    from cruxible_client.contracts.captures import CanonicalDuration
    from cruxible_client.contracts.procedures.measurements import (
        AcceptedQueryProcedureMeasurement,
        ProcedureMeasurementDeclaration,
        ProcedureMeasurementExpectation,
    )
    from tests.test_procedures.test_procedure_artifacts import _definition

    definition = _definition()
    read = next(node for node in definition.nodes if node.kind == "state_tap")
    measurement = ProcedureMeasurementDeclaration(
        name="credit",
        subject_grain="node",
        node_id=read.node_id,
        measurement=AcceptedQueryProcedureMeasurement(
            query=ArtifactPin(
                role="query", target=read.query.target, artifact_digest="sha256:" + "1" * 64
            ),
            expect=ProcedureMeasurementExpectation(min_count=0),
        ),
        check_after=CanonicalDuration(microseconds=1),
        expires_after=CanonicalDuration(microseconds=2),
    )
    artifact = _artifact(definition.model_copy(update={"measurements": (measurement,)}))
    content = render_procedure(artifact)
    assert json.loads(content)["definition"]["measurements"][0]["node_id"] == read.node_id
    assert (
        parse_procedure(content, path="procedures/" + artifact.identity.name + ".json") == artifact
    )


def test_handwritten_slotted_procedure_round_trips_without_provider_selection():
    authored = ProcedureSpec.model_validate(
        {
            "procedure": "test.slotted",
            "input": {"value": "integer"},
            "output": {"value": "integer"},
            "slots": {
                "compute": {
                    "in": "test.compute-input@1",
                    "out": "test.compute-output@1",
                    "effect": "pure",
                }
            },
            "steps": [
                {
                    "call": "result",
                    "slot": "compute",
                    "input": {"value": "$input.value"},
                }
            ],
            "returns": "result",
        }
    )
    lowered = lower_procedure_spec(authored)
    definition = ProcedureDefinition.model_validate(lowered.definition)
    assert tuple(definition.slots) == ("compute",)
    assert definition.slots["compute"].in_.spelling == "test.compute-input@1"
    assert definition.slots["compute"].out.spelling == "test.compute-output@1"
    assert definition.slots["compute"].effect == "pure"
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest

    original = ProcedureArtifact(
        identity={"kind": "Procedure", "name": definition.name},
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=lower_authoring_input(lowered).owned_contracts,
        activation_policy="snapshot",
    )
    restored = decompile_procedure(original)
    assert restored.slots == authored.slots
    assert restored.steps[0].slot == "compute"
    assert (
        ProcedureDefinition.model_validate(lower_procedure_spec(restored).definition) == definition
    )
    assert (
        parse_procedure(render_procedure(original), path="procedures/test.slotted.json") == original
    )


def test_slots_cannot_stand_in_non_provider_positions():
    with pytest.raises(ValidationError) as refused:
        ProcedureDefinition.model_validate(
            {
                **lower_procedure_spec(spec()).definition,
                "contract_in": {"slot_name": "compute"},
                "slots": {
                    "compute": {
                        "in": "test.compute-input@1",
                        "out": "test.compute-output@1",
                        "effect": "pure",
                    }
                },
            }
        )
    locations = {item["loc"] for item in refused.value.errors()}
    assert ("contract_in", "target") in locations
    assert ("contract_in", "slot_name") in locations
