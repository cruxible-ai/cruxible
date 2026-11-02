"""The one typed Procedure authoring form: refusals, rendering and the kit corpus."""

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
from cruxible_client.contracts.procedures.decompile import decompile_procedure
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import ProcedureDefinition
from cruxible_client.contracts.shapes import ContractField, IntType, validate_contract
from tests.support.procedures import procedure_members, ref


def _artifact(definition):
    from tests.support.procedures import procedure_artifact

    return procedure_artifact(definition, activation_policy="drain")


def _stored(lowered, *, name=None, activation="snapshot"):
    definition = ProcedureDefinition.model_validate(lowered.definition)
    return ProcedureArtifact(
        identity={"kind": "Procedure", "name": name or definition.name},
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        activation_policy=activation,
    )


def spec(**changes):
    data = dict(
        procedure="test.authored",
        input={"value": "int", "flag": "bool"},
        returns="result",
        steps=[{"project": "result", "fields": {"value": ref("$input.value")}}],
    )
    data.update(changes)
    return ProcedureSpec.model_validate(data)


def _gt(left, right):
    return {"op": "gt", "left": left, "right": right}


def test_typed_extra_forbidden_and_no_numeric_coercion():
    with pytest.raises(ValidationError):
        spec(
            slots={"compute": {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}},
            steps=[{"call": "result", "slot": "compute", "input": {}, "typo": 1}],
        )
    with pytest.raises(ValidationError):
        spec(hard_caps={"provider_calls": True})


@pytest.mark.parametrize("retired", ["budget", "caps"])
def test_a_procedure_declares_hard_caps_only(retired):
    # The operating budget is the Line's (a direct run's ephemeral Line spec),
    # within the Procedure's hard caps.
    with pytest.raises(ValidationError, match="procedure_budget_retired"):
        spec(**{retired: {"provider_calls": 1}})
    lowered = lower_procedure_spec(spec(hard_caps={"provider_calls": 1})).definition
    assert "budget" not in lowered
    assert lowered["hard_caps"]["max_provider_calls"] == 1
    with pytest.raises(ValidationError):
        spec(steps=[{"project": "result", "fields": {}, "read": "other", "query": "q"}])


@pytest.mark.parametrize(
    ("predicate", "code"),
    [
        (_gt(ref("$input.vlaue"), {"literal": 0}), "procedure_ref_unknown"),
        (_gt(ref("$steps.missing.value"), {"literal": 0}), "procedure_ref_unknown"),
        (
            {"op": "eq", "left": ref("$input.value"), "right": {"literal": "0"}},
            "procedure_operand_type_mismatch",
        ),
        (_gt(ref("$input.flag"), {"literal": False}), "procedure_operand_type_mismatch"),
        (_gt(ref("$params.missing"), {"literal": 0}), "procedure_ref_unknown"),
    ],
)
def test_refusal_table(predicate, code):
    authored = spec(
        steps=[
            {"guard": predicate, "else": {"halt": "stop"}},
            {"project": "result", "fields": {"value": ref("$input.value")}},
        ]
    )
    with pytest.raises(ProcedureSpecError) as caught:
        lower_procedure_spec(authored)
    assert caught.value.code == "cruxible.authoring." + code
    assert caught.value.field_path == "$.steps[0].guard"
    if "vlaue" in json.dumps(predicate):
        assert "value" in str(caught.value) and "Did you mean" in str(caught.value)


@pytest.mark.parametrize("verb", ["guard", "repeat"])
def test_guard_and_until_take_the_typed_predicate_never_an_expression_string(verb):
    from cruxible_client.contracts.procedures.authored import GuardStep, RepeatStep

    model = GuardStep if verb == "guard" else RepeatStep
    field = "guard" if verb == "guard" else "until"
    data = (
        {}
        if verb == "guard"
        else {
            "repeat": "loop",
            "max_attempts": 1,
            "body": [{"transform": "rows", "kind": "dedupe", "spec": {"input": [], "keys": ["a"]}}],
        }
    )
    with pytest.raises(ValidationError, match="expression strings are not accepted"):
        model.model_validate({**data, field: "input.value > 0"})
    with pytest.raises(ValidationError):
        model.model_validate({**data, field: {"op": "gt", "left": "input.value", "right": 0}})
    # A guard reads run values: never the current item.
    with pytest.raises(ValidationError, match="current item"):
        model.model_validate(
            {**data, field: {"op": "present", "of": {"$ref": "item", "path": ["fields", "x"]}}}
        )
    typed = model.model_validate({**data, field: _gt(ref("$input.value"), {"literal": 0})})
    assert getattr(typed, field).op == "gt"


def test_branch_actions_are_a_typed_union():
    from cruxible_client.contracts.procedures.authored import (
        AbortAction,
        GotoAction,
        GuardStep,
        HaltAction,
    )

    predicate = _gt(ref("$input.value"), {"literal": 0})
    default = GuardStep.model_validate({"guard": predicate})
    assert isinstance(default.else_, AbortAction)
    assert default.else_.abort.code == "guard_refused"
    for arm, expected in (
        ({"abort": {"code": "empty", "message": "Nothing"}}, AbortAction),
        ({"halt": "empty", "message": "Nothing remains"}, HaltAction),
        ({"goto": "result"}, GotoAction),
    ):
        step = GuardStep.model_validate({"guard": predicate, "then": arm, "else": arm})
        assert isinstance(step.then, expected) and isinstance(step.else_, expected)
    with pytest.raises(ValidationError):
        GuardStep.model_validate({"guard": predicate, "else": 'halt(empty, "Nothing")'})
    with pytest.raises(ValidationError):
        GuardStep.model_validate({"guard": predicate, "else": {"stop": "x"}})


def test_abort_arms_lower_to_one_guard_diagnostic():
    abort = {"abort": {"code": "empty", "message": "Nothing"}}
    lowered = lower_procedure_spec(
        spec(
            steps=[
                {"guard": _gt(ref("$input.value"), {"literal": 0}), "else": abort},
                {"project": "result", "fields": {"value": ref("$input.value")}},
            ]
        )
    )
    guard = lowered.definition["nodes"][0]
    assert (guard["on_false"], guard["refusal_code"], guard["message"]) == (
        "$abort",
        "empty",
        "Nothing",
    )
    other = {"abort": {"code": "other", "message": "Else"}}
    with pytest.raises(ProcedureSpecError, match="one diagnostic"):
        lower_procedure_spec(
            spec(
                steps=[
                    {
                        "guard": _gt(ref("$input.value"), {"literal": 0}),
                        "then": abort,
                        "else": other,
                    },
                    {"project": "result", "fields": {"value": ref("$input.value")}},
                ]
            )
        )


def test_the_explicit_escape_hatch_and_string_references_are_gone():
    with pytest.raises(ValidationError, match="retired step 'explicit'"):
        spec(steps=[{"explicit": {"kind": "halt", "node_id": "stop"}}])
    # A literal never names a reference: a reference is a typed Ref.
    with pytest.raises(ProcedureSpecError) as caught:
        lower_procedure_spec(spec(steps=[{"project": "result", "fields": {"value": "$input"}}]))
    assert caught.value.code == "cruxible.authoring.procedure_literal_reserved"
    assert caught.value.field_path == "$.steps[0].fields.value"
    with pytest.raises(ValidationError, match="retained source"):
        spec(source={"text": "def f(): ..."})


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


@pytest.mark.parametrize(
    ("definition", "form"),
    [
        ({"name": "x", "source_request": {"text": "def f(): ..."}}, "source_request"),
        ({"name": "x", "nodes": []}, "a graph definition"),
    ],
)
def test_retired_authoring_forms_refuse_typed_naming_the_spec(definition, form):
    with pytest.raises(ValidationError) as caught:
        ProcedureInput.model_validate({"kind": "procedure", "definition": definition})
    (error,) = caught.value.errors()
    assert error["type"] == "procedure_authoring_form_retired"
    assert "ProcedureSpec" in error["msg"] and form in error["msg"]


def test_authored_spec_keeps_a_typed_schema_in_both_wire_modes():
    request = ProcedureSpec.model_json_schema(mode="validation")
    response = ProcedureSpec.model_json_schema(mode="serialization")
    assert response == request
    assert request["additionalProperties"] is False
    assert {"procedure", "input", "steps"} <= set(request["properties"])
    assert request["properties"]["steps"]["type"] == "array"
    assert "Ref" in request["$defs"]
    assert request["$defs"]["Ref"]["required"] == ["$ref"]


CORPUS = json.loads(
    (Path(__file__).parents[1] / "fixtures/procedures/security-p1.json").read_text()
)


def _control_flow_artifact(raw):
    """Keep the corpus's control flow as a native halt example.

    Command terminals themselves are tested as retired below; this independent
    expected graph still pins decompilation of the surrounding calls and guards.
    """
    data = json.loads(json.dumps(raw))
    for i, node in enumerate(data["definition"]["nodes"]):
        if node["kind"] in {"propose_change_set", "settle_change_set"}:
            data["definition"]["nodes"][i] = {
                "kind": "halt",
                "node_id": node.get("node_id", node.get("id")),
            }
    from cruxible_client.contracts.procedures.wire_v3 import expand_procedure

    data = expand_procedure(data)
    from cruxible_client.contracts.procedures.models import derived_terminal_capability

    data["definition"]["terminal_capability"] = derived_terminal_capability(
        data["definition"]["nodes"]
    )
    definition = ProcedureDefinition.model_validate(data["definition"])
    data["definition_digest"] = compute_procedure_definition_digest(definition).tagged
    return ProcedureArtifact.model_validate(data)


OPERATIONS = json.loads(
    (Path(__file__).parents[1] / "fixtures/procedures/security-p1-operations.json").read_text()
)
#: The governed boundary Contracts the corpus Procedures name (``name@1``).
CORPUS_CONTRACTS = {
    name: validate_contract(contract)
    for name, contract in json.loads(
        (Path(__file__).parents[1] / "fixtures/procedures/security-p1-contracts.json").read_text()
    ).items()
}


def _corpus_resolver(original):
    """Independent schema metadata for unbound operation signatures, not providers."""
    sources = {
        node.as_: node
        for node in original.definition.nodes
        if node.kind == "call" and node.acquires
    }

    def resolve(kind, value):
        if kind == "slot":
            return value
        if kind == "query":
            return None
        if kind == "contract":
            return CORPUS_CONTRACTS.get(value.name)
        signature = original.definition.slots[value.slot]
        entry = next(
            entry
            for entry in OPERATIONS.values()
            if entry["signature"]["in"]["name"] == signature.in_.name
        )
        acquiring = bool(getattr(value, "captures", None))
        names = (entry.get("source_view") or entry["output"]) if acquiring else entry["output"]
        return {
            "output": {name: None for name in names["fields"]},
            "input": None,
            "request_defaults": sources[value.call].input if acquiring else {},
        }

    return resolve


@pytest.mark.parametrize("filename", sorted(CORPUS))
def test_every_kit_procedure_renders_typed_and_lowers_to_stored_bytes(filename):
    with pytest.raises(ValueError, match="procedure_node_kind_retired.*claims"):
        ProcedureArtifact.model_validate(CORPUS[filename])
    original = _control_flow_artifact(CORPUS[filename])
    authored = decompile_procedure(original)
    rendered = json.dumps(authored.model_dump(mode="json", by_alias=True))
    assert '"$steps.' not in rendered and '"$input' not in rendered  # every reference is typed
    lowered = lower_procedure_spec(authored, resolve=_corpus_resolver(original))
    actual = _stored(lowered, activation=lowered.activation_policy)
    # The stored expectation was migrated structurally from independent fields,
    # never generated by the decompiler or the authored lowerer under test.
    assert actual.definition_digest == original.definition_digest
    assert render_procedure(actual) == render_procedure(original)
    rendered_bytes = render_procedure(actual)
    parsed = parse_procedure(rendered_bytes, path="procedures/" + original.identity.name + ".json")
    assert render_procedure(parsed) == rendered_bytes
    assert ProcedureArtifact.model_validate(json.loads(rendered_bytes)) == parsed
    assert procedure_artifact_digest(parsed) == procedure_artifact_digest(actual)


def _inventory_import_worked_example():
    original = _control_flow_artifact(CORPUS["security.inventory-import.json"])
    scope = {"scope": ref("$input.scope")}
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
        input={},
        input_contract="security-inventory-import-input@1",
        output_contract="security-runtime-inventory-summary@1",
        returns={"from": "parse", "omit": ["items"]},
        activation="drain",
        hard_caps={
            "wall_clock": "PT5M",
            "provider_calls": 2,
            "capture_bytes": "1MiB",
            "items": 5000,
            "repeat_attempts": 1,
        },
        steps=[
            {
                "call": "inventory",
                "captures": {"capture": "security.runtime-inventory-v1"},
                "slot": "inventory",
                "input": {
                    "logical_source": "security.inventory",
                    "relative_path": "inventory.json",
                },
            },
            {"read": "held", "query": "security.inventory_index", "params": scope},
            {"read": "runs", "query": "security.inventory_runs", "params": scope},
            {"read": "patches", "query": "security.inventory_patches", "params": scope},
            {
                "call": "parse",
                "slot": "parse",
                "input": {
                    "inventory_text": ref("$steps.inventory.content.text"),
                    "held": ref("$steps.held.result.rows"),
                    "runs": ref("$steps.runs.result.rows"),
                    "patches": ref("$steps.patches.result.rows"),
                    "truncated": {
                        "held": ref("$steps.held.truncated"),
                        "runs": ref("$steps.runs.truncated"),
                        "patches": ref("$steps.patches.truncated"),
                    },
                    "scope": ref("$input.scope"),
                },
            },
            {
                "guard": _gt(ref("$steps.parse.mine"), {"literal": 0}),
                "id": "mine",
                "else": {"halt": "other_scope", "id": "other"},
            },
            {
                "guard": _gt(ref("$steps.parse.proposes"), {"literal": 0}),
                "id": "changed",
                "else": {"halt": "no_change", "id": "nochange"},
            },
            {"halt": None, "id": "propose"},
        ],
    )

    return original, authored, _corpus_resolver(original)


def test_handwritten_inventory_import_matches_the_stored_definition_digest():
    original, authored, resolve = _inventory_import_worked_example()
    lowered = lower_procedure_spec(authored, resolve=resolve)
    # Its boundaries name governed Contracts: nothing is co-proposed.
    assert lowered.generated == {}
    actual = ProcedureDefinition.model_validate(lowered.definition)
    # Independent handwritten steps and literal signatures. Neither decompile
    # nor a graph normalizer constructs the authored side.
    assert compute_procedure_definition_digest(actual).tagged == original.definition_digest


def test_inventory_import_worked_example_has_eleven_nodes():
    _original, authored, resolve = _inventory_import_worked_example()
    payload = lower_authoring_input(ProcedureInput(kind="procedure", spec=authored))
    assert payload.definition["procedure_spec"]["procedure"] == "security.inventory-import"
    lowered = lower_procedure_spec(authored, resolve=resolve)
    assert [node["kind"] for node in lowered.definition["nodes"]] == [
        "call",
        "read",
        "read",
        "read",
        "call",
        "project",
        "guard",
        "guard",
        "halt",
        "halt",
        "halt",
    ]
    lowered_call = lowered.definition["nodes"][4]
    assert lowered_call["input"]["held"] == "$steps.held.result.rows"
    assert "items" not in lowered.definition["nodes"][5]["fields"]
    assert lowered.definition["nodes"][8]["kind"] == "halt"
    assert lowered.definition["nodes"][6]["on_false"] == "other"
    assert "refusal_code" not in lowered.definition["nodes"][6]
    assert lowered.definition["hard_caps"]["max_capture_bytes"] == 1048576


def test_stored_v3_keeps_template_data():
    original = ProcedureArtifact.model_validate(CORPUS["security.credential-acquire.json"])
    from cruxible_client.contracts.procedures.models import GuardNode, HaltNode
    from cruxible_client.contracts.procedures.wire_v3 import compact_procedure
    from cruxible_client.contracts.shapes import Comparison, LiteralOperand

    literal = Comparison(
        op="eq", left=LiteralOperand(literal=True), right=LiteralOperand(literal=True)
    )
    with pytest.raises(ValidationError, match="abort requires"):
        GuardNode(node_id="gate", predicate=literal)
    with pytest.raises(ValidationError, match="belong only to an abort arm"):
        GuardNode(node_id="gate", predicate=literal, on_false="stop", refusal_code="x", message="y")
    halt = HaltNode(node_id="stop", reason="done", message="Nothing remains")
    assert compact_procedure(halt) == {
        "kind": "halt",
        "id": "stop",
        "reason": "done",
        "message": "Nothing remains",
    }
    assert compact_procedure(literal) == {
        "op": "eq",
        "left": {"literal": True},
        "right": {"literal": True},
    }
    project = next(node for node in original.definition.nodes if node.kind == "project")
    data = {"tag": "my-data", "node_id": "my-id", "value": None, "default": False}
    compact = compact_procedure(project.model_copy(update={"fields": data}))
    assert compact["fields"] == data
    assert "node_id" not in compact


def test_decompile_shows_cap_durations_compactly_without_moving_stored_bytes():
    from cruxible_client.contracts.temporal import (
        format_iso_duration_microseconds,
        parse_iso_duration_microseconds,
    )

    original = _control_flow_artifact(CORPUS["security.inventory-import.json"])
    authored = decompile_procedure(original)
    assert authored.hard_caps.wall_clock == "PT300S"
    # Display only: each text parses back to the stored microseconds (the corpus
    # round trip above keeps every Procedure's stored bytes and digest).
    assert parse_iso_duration_microseconds(authored.hard_caps.wall_clock) == (
        original.definition.hard_caps.max_wall_clock.microseconds
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
    from cruxible_client.contracts.procedures.authored import HaltAction

    original = _control_flow_artifact(CORPUS["security.inventory-import.json"])
    authored = decompile_procedure(original)
    assert len(authored.steps) == 8
    assert authored.returns.id == "summary"
    assert authored.steps[5].else_ == HaltAction(halt="other_scope", id="other")
    assert authored.steps[6].else_ == HaltAction(halt="no_change", id="nochange")


@pytest.mark.parametrize("node_kind", ["guard", "repeat"])
@pytest.mark.parametrize("predicate_shape", ["empty_path", "single_all", "single_any"])
def test_every_predicate_shape_renders_typed_and_lowers_back(node_kind, predicate_shape):
    from cruxible_client.contracts.refs import step_ref
    from cruxible_client.contracts.shapes import AllOf, AnyOf, Presence
    from tests.test_procedures.test_procedure_artifacts import _definition
    from tests.test_procedures.test_procedure_execution import _repeat_transform_procedure

    alias, member = ("rows", "completed") if node_kind == "guard" else ("shaped", "flag")
    leaf = Presence(
        op="present",
        of=step_ref(alias) if predicate_shape == "empty_path" else step_ref(alias, member),
    )
    predicate = (
        leaf
        if predicate_shape == "empty_path"
        else (AllOf if predicate_shape == "single_all" else AnyOf)(of=(leaf,))
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
    typed = next(
        step for step in authored.steps if hasattr(step, "guard") or hasattr(step, "until")
    )
    assert (typed.guard if node_kind == "guard" else typed.until) == predicate
    if node_kind == "guard":
        assert typed.else_.halt == "no"

    lowered = lower_procedure_spec(authored)
    actual = ProcedureDefinition.model_validate(lowered.definition)
    assert compute_procedure_definition_digest(actual) == compute_procedure_definition_digest(
        definition
    )


def test_field_descriptors_forbid_unknown_fields_and_coercion():
    with pytest.raises(ValidationError):
        spec(input={"value": {"type": "int", "optinal": True}})
    with pytest.raises(ValidationError):
        spec(input={"value": {"type": "int", "optional": "true"}})
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
                "guard": _gt(ref("$input.value"), {"literal": 0}),
                "else": {"halt": "empty", "message": "Nothing remains"},
            },
            {"project": "result", "fields": {"value": ref("$input.value")}},
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
        steps=[{"project": "result", "fields": {"value": ref("$input.value")}, "next": "result"}]
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
            {"read": "read", "claim_type": "semantic.alais", "subject": "test"},
            "claim_type",
            "ClaimType:semantic.alias",
        ),
        (
            {"invoke": "stored.procc", "input": {}, "params": {"status": "open"}},
            "invoke",
            "Procedure:stored.proc",
        ),
        (
            {
                "call": "read",
                "captures": {"capture": "security.invenotry"},
                "slot": "read",
                "input": {},
            },
            "captures.capture",
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
    tree.update(procedure_members(procedure))
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="named-candidates")

    authored = spec(
        slots={
            "read": {
                "in": "workspace.file.request@1",
                "out": "workspace.file.result@1",
                "effect": "external_read",
            }
        }
        if "captures" in step
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


def test_reference_roots_are_typed_so_step_names_never_shadow_the_input():
    authored = spec(
        steps=[
            {"project": "input", "fields": {"value": ref("$input.value")}},
            {"project": "result", "fields": {"value": ref("$steps.input.value")}},
        ]
    )
    lowered = lower_procedure_spec(authored)
    assert lowered.definition["nodes"][0]["fields"]["value"] == "$input.value"
    assert lowered.definition["nodes"][1]["fields"]["value"] == "$steps.input.value"


def test_spec_dump_preserves_actions_with_and_without_aliases():
    authored = spec(
        steps=[
            {"guard": _gt(ref("$input.value"), {"literal": 0}), "else": {"halt": "empty"}},
            {"project": "result", "fields": {"value": ref("$input.value")}},
        ]
    )
    # The wire spells a reference's root ``$ref`` and the false arm ``else``.
    wire = authored.model_dump(mode="json", by_alias=True)
    assert wire["steps"][0]["else"] == {"halt": "empty"}
    assert ProcedureSpec.model_validate(wire) == authored
    assert ProcedureSpec.model_validate(authored.model_dump(mode="json")) == authored


def test_decompile_does_not_fold_a_halt_with_a_shared_explicit_edge():
    from cruxible_client.contracts.procedures.authored import GotoAction
    from cruxible_client.contracts.procedures.graph import analyze_procedure
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
    # Guard halts are excluded from implicit fallthrough. This explicit edge
    # deliberately shares the halt with the guard's false arm.
    assert any(getattr(step, "halt", None) is not None for step in authored.steps)
    assert any(getattr(step, "else_", None) == GotoAction(goto="stop") for step in authored.steps)
    assert analyze_procedure(original.definition).edges["result"] == {"next": "stop"}
    content = render_procedure(original)
    assert json.loads(content)["definition"]["nodes"][2]["next"] == "stop"
    restored = parse_procedure(content, path="procedures/" + original.identity.name + ".json")
    assert (
        analyze_procedure(restored.definition).edges == analyze_procedure(original.definition).edges
    )
    relowered = ProcedureDefinition.model_validate(lower_procedure_spec(authored).definition)
    assert compute_procedure_definition_digest(relowered) == compute_procedure_definition_digest(
        original.definition
    )


def test_lowered_halt_ids_do_not_collide_or_exceed_graph_limits():
    long_id = "a" * 128
    authored = spec(
        steps=[
            {
                "id": long_id,
                "guard": _gt(ref("$input.value"), {"literal": 0}),
                "else": {"halt": "empty"},
            },
            {"project": "result", "fields": {"value": ref("$input.value")}},
            {"halt": "other", "id": long_id[:123] + "_halt"},
        ]
    )
    lowered = lower_procedure_spec(authored)
    ids = [node["node_id"] for node in lowered.definition["nodes"]]
    assert len(ids) == len(set(ids))
    assert all(len(id_) <= 128 for id_ in ids)


def test_v3_retains_the_transform_spec_union_discriminator():
    from cruxible_client.contracts.procedures.models import TransformNode
    from cruxible_client.contracts.procedures.nodes import TransformShapeSpec
    from tests.test_procedures.test_procedure_artifacts import _definition

    definition = _definition()
    transform = TransformNode(
        node_id="result",
        transform_kind="shape",
        spec=TransformShapeSpec(input=[], fields={"id": {"from": "$item.fields.id"}}),
        **{"as": "result"},
    )
    artifact = _artifact(definition.model_copy(update={"nodes": (transform,), "returns": "result"}))
    content = render_procedure(artifact)
    assert (
        json.loads(content)["definition"]["nodes"][0]["spec"]["tag"]
        == "cruxible-transform-shape-spec-v1"
    )
    assert (
        parse_procedure(content, path="procedures/" + artifact.identity.name + ".json") == artifact
    )


def test_every_transform_kind_renders_typed_and_lowers_back():
    record = {"$ref": "item", "path": ["fields", "keep"]}
    steps = [
        {
            "project": "rows",
            "literal": {"items": [{"id": "a", "keep": True}]},
        },
        {
            "transform": "shaped",
            "kind": "shape",
            "spec": {
                "input": ref("$steps.rows.items"),
                "fields": {
                    "id": {"from": {"$ref": "item", "path": ["fields", "id"]}},
                    "keep": {"from": record, "as": "bool"},
                    "label": {"value": "fixed"},
                },
            },
        },
        {
            "transform": "kept",
            "kind": "filter",
            "spec": {
                "input": ref("$steps.shaped"),
                "where": {"op": "eq", "left": record, "right": {"literal": True}},
            },
        },
        {
            "transform": "deduped",
            "kind": "dedupe",
            "spec": {"input": ref("$steps.kept"), "keys": ["id"]},
        },
        {
            "transform": "joined",
            "kind": "join",
            "spec": {
                "left": ref("$steps.deduped"),
                "right": [{"id": "a", "rank": 1}],
                "left_field": "id",
                "right_field": "id",
                "mode": "anti",
            },
        },
        {
            "transform": "both",
            "kind": "union",
            "spec": {"inputs": [ref("$steps.kept"), ref("$steps.joined")]},
        },
        {
            "transform": "any_kept",
            "kind": "aggregate",
            "spec": {
                "input": ref("$steps.both"),
                "op": "any",
                "where": {"op": "present", "of": record},
            },
        },
        {
            "transform": "label",
            "kind": "derive",
            "spec": {
                "input": {"keep": ref("$steps.any_kept.result")},
                "cases": [
                    {
                        "when": {"op": "eq", "left": record, "right": {"literal": True}},
                        "value": "kept",
                    }
                ],
                "else": "dropped",
            },
        },
        {"project": "result", "fields": {"value": ref("$steps.label.result")}},
    ]
    authored = spec(input={}, output={"value": "string"}, steps=steps)
    lowered = lower_procedure_spec(authored)
    stored = _stored(lowered)
    rendered = decompile_procedure(stored)
    assert '"$steps.' not in json.dumps(rendered.model_dump(mode="json", by_alias=True))
    relowered = lower_procedure_spec(rendered)
    assert relowered.definition == lowered.definition
    with pytest.raises(ValidationError, match="record predicate"):
        spec(
            steps=[
                {
                    "transform": "kept",
                    "kind": "filter",
                    "spec": {"input": [], "where": {"op": "present", "of": ref("$steps.rows")}},
                }
            ]
        )


def test_repeat_bodies_are_typed_calls_and_transforms():
    authored = spec(
        input={},
        output={"value": "string"},
        slots={
            "compute": {
                "in": "test.compute-input@1",
                "out": "test.compute-output@1",
                "effect": "pure",
            }
        },
        hard_caps={"repeat_attempts": 3, "provider_calls": 3},
        steps=[
            {
                "repeat": "loop",
                "max_attempts": 3,
                "body": [
                    {"call": "fetched", "id": "fetch", "slot": "compute", "input": {"n": 1}},
                    {
                        "transform": "unique",
                        "kind": "dedupe",
                        "spec": {"input": ref("$steps.fetched.rows"), "keys": ["id"]},
                    },
                ],
                "until": {
                    "op": "gt",
                    "left": {"count": ref("$steps.unique")},
                    "right": {"literal": 0},
                },
            },
            {"project": "result", "fields": {"value": "done"}},
        ],
    )
    lowered = lower_procedure_spec(authored)
    repeat = lowered.definition["nodes"][0]
    assert [item["operation"] for item in repeat["body"]] == ["call", "transform"]
    assert repeat["body"][0]["node_id"] == "fetch" and repeat["body"][0]["as"] == "fetched"
    assert repeat["until"]["left"] == {"count": {"$ref": "step", "step": "unique"}}
    relowered = lower_procedure_spec(decompile_procedure(_stored(lowered)))
    assert relowered.definition == lowered.definition


def test_leaf_projects_and_literal_records_are_typed_steps():
    authored = spec(
        input={"positive": "bool"},
        output={"label": "string"},
        returns=None,
        steps=[
            {
                "guard": {"op": "eq", "left": ref("$input.positive"), "right": {"literal": True}},
                "else": {"goto": "no"},
            },
            {
                "project": "yes",
                "literal": {"label": "ready"},
                "output": {"label": "string"},
                "leaf": True,
            },
            {"project": "no", "fields": {"label": "halted"}, "leaf": True},
        ],
    )
    lowered = lower_procedure_spec(authored)
    assert lowered.definition["returns"] is None
    stored = _stored(lowered)
    assert [node.leaf for node in stored.definition.nodes if node.kind == "project"] == [True, True]
    relowered = lower_procedure_spec(decompile_procedure(stored))
    assert relowered.definition == lowered.definition


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


def test_parameter_and_step_references_have_distinct_typed_roots():
    authored = spec(
        params={"limit": "int"},
        steps=[
            {"project": "params", "fields": {"value": ref("$input.value")}},
            {
                "guard": {
                    "op": "ge",
                    "left": ref("$steps.params.value"),
                    "right": ref("$params.limit"),
                },
                "else": {"halt": "below_limit"},
            },
            {"project": "result", "fields": {"value": ref("$steps.params.value")}},
        ],
    )
    lowered = lower_procedure_spec(authored)
    assert lowered.definition["nodes"][2]["fields"]["value"] == "$steps.params.value"
    predicate = lowered.definition["nodes"][1]["predicate"]
    assert predicate["left"] == {"$ref": "step", "step": "params", "path": ["value"]}
    assert predicate["right"] == {"$ref": "param", "path": ["limit"]}
    # A value template reads a parameter too (G1), typed by the params Contract.
    valued = lower_procedure_spec(
        spec(
            params={"limit": "int"},
            output={"value": "int"},
            steps=[{"project": "result", "fields": {"value": ref("$params.limit")}}],
        )
    )
    assert valued.definition["nodes"][0]["fields"] == {"value": "$params.limit"}
    with pytest.raises(ProcedureSpecError) as caught:
        lower_procedure_spec(
            spec(
                params={"limit": "int"},
                output={"value": "string"},
                steps=[{"project": "result", "fields": {"value": ref("$params.limit")}}],
            )
        )
    assert "mistyped" in str(caught.value) and "value" in str(caught.value)


def test_parameter_name_can_match_a_collection_step_alias():
    authored = spec(
        input={"subject": "string"},
        params={"limit": "int"},
        steps=[
            {
                "read": "limit",
                "claim_type": "catalog.entry.value",
                "subject": ref("$input.subject"),
                "all": 10,
            },
            {"guard": _gt(ref("$params.limit"), {"literal": 0}), "else": {"halt": "empty"}},
            {"project": "result", "fields": {"ok": True}},
        ],
    )
    lowered = lower_procedure_spec(
        authored,
        resolve=lambda kind, step: {
            "output": {"value": ContractField(type=IntType())},
            "subject_kind": "catalog.entry",
        },
    )
    assert lowered.definition["nodes"][1]["predicate"]["left"] == {
        "$ref": "param",
        "path": ["limit"],
    }
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
    read = next(node for node in definition.nodes if node.kind == "read")
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
            "input": {"value": "int"},
            "output": {"value": "int"},
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
                    "input": {"value": ref("$input.value")},
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
    original = _stored(lowered)
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
