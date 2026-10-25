"""Authored Procedure refusals and kit lowering-equivalence corpus."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

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
from cruxible_client.contracts.procedures.models import ProcedureDefinition, iter_pin_bindings


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
        spec(steps=[{"call": "result", "interface": "test@1", "input": {}, "typo": 1}])
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


def test_shared_authoring_envelope():
    authored = spec()
    wrapped = ProcedureInput(kind="procedure", spec=authored)
    flat = ProcedureInput.model_validate(
        {"kind": "procedure", **authored.model_dump(mode="json", by_alias=True)}
    )
    assert flat == wrapped
    payload = lower_authoring_input(wrapped)
    assert payload.definition["procedure_spec"]["procedure"] == authored.procedure


CORPUS = json.loads(
    (Path(__file__).parents[1] / "fixtures/procedures/security-p1.json").read_text()
)


@pytest.mark.parametrize("filename", sorted(CORPUS))
def test_every_kit_procedure_lowers_to_stored_bytes(filename):
    original = ProcedureArtifact.model_validate(CORPUS[filename])
    authored = decompile_procedure(original)
    originals = {node.as_: node for node in original.definition.nodes if hasattr(node, "as_")}
    contracts = {c.identity.name: c.contract_schema for c in original.owned_contracts}

    def resolve(kind, step):
        node = originals[getattr(step, "call", None) or getattr(step, "source", None)]
        output = (
            contracts.get(node.contract_out.target.name)
            if hasattr(node, "contract_out") and node.contract_out
            else None
        )
        if node.kind == "source":
            from cruxible_client.contracts.provider_contracts import ProviderOperationContract

            operations = json.loads(
                (
                    Path(__file__).parents[1] / "fixtures/procedures/security-p1-operations.json"
                ).read_text()
            )
            operation = ProviderOperationContract.model_validate(
                operations[node.interface.target.name]
            )
            output = operation.material or (
                operation.output if isinstance(operation.output, ContractSchema) else None
            )
        return {
            "bindings": {
                key: getattr(node, key).model_dump(mode="json")
                if hasattr(getattr(node, key), "model_dump")
                else getattr(node, key)
                for key in ("provider", "interface", "interface_digest", "implementation_digest")
            },
            "output": output.fields
            if output
            else {"content": PropertySchema(type="json"), "source": PropertySchema(type="json")},
            "input": None,
        }

    lowered = lower_procedure_spec(authored, resolve=resolve)
    # The daemon's unchanged binder owns exact references and owner contracts.
    # Rebind by the original names to compare typed tree and actual v3 bytes.
    pins = {(p.role, p.target.qualified): p for p in original.pins}

    def rebind(value):
        if isinstance(value, dict):
            if value.get("kind") == "accepted":
                return pins[value["role"], value["target"]].model_dump(mode="json")
            if value.get("kind") == "carried_contract":
                return pins[value["role"], "Contract:" + value["name"]].model_dump(mode="json")
            return {k: rebind(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [rebind(v) for v in value]
        return value

    definition = ProcedureDefinition.model_validate(rebind(lowered.definition))
    # Omitted fallthrough edges have exactly the same typed graph semantics;
    # v3's renderer omits them from both sides.
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest

    actual = original.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "artifact_format": "cruxible-procedure-v3",
        }
    )
    from cruxible_client.contracts.procedures.models import CallNode

    # The corpus predates N1a: Call I/O copies are now interface-owned (N1b's
    # parallel area), so omit those mirrors from the reference v3 rendering.
    expected_definition = original.definition.model_copy(
        update={
            "nodes": tuple(
                n.model_copy(update={"contract_in": None, "contract_out": None})
                if isinstance(n, CallNode)
                else n
                for n in original.definition.nodes
            )
        }
    )
    expected = original.model_copy(
        update={
            "definition": expected_definition,
            "definition_digest": compute_procedure_definition_digest(expected_definition).tagged,
            "artifact_format": "cruxible-procedure-v3",
        }
    )
    assert render_procedure(actual) == render_procedure(expected)
    used = {pin for pin in iter_pin_bindings(definition)}
    actual = actual.model_copy(
        update={
            "pins": tuple(p for p in actual.pins if p in used),
            "owned_contracts": tuple(
                c for c in actual.owned_contracts if any(p.target == c.identity for p in used)
            ),
        }
    )
    rendered = render_procedure(actual)
    parsed = parse_procedure(rendered, path="procedures/" + original.identity.name + ".json")
    assert render_procedure(parsed) == rendered
    assert procedure_artifact_digest(parsed) == procedure_artifact_digest(actual)


def test_historical_v2_bytes_and_digest_do_not_change():
    for filename, document in CORPUS.items():
        original = ProcedureArtifact.model_validate(document)
        from cruxible_client.contracts.canonical import (
            ArtifactDigest,
            pretty_canonical_bytes,
            typed_digest,
        )

        assert render_procedure(original) == pretty_canonical_bytes(document)
        assert procedure_artifact_digest(original) == typed_digest(
            ArtifactDigest, "playbill-envelope-v1", document
        )


def test_inventory_import_worked_example_has_eleven_nodes_and_typed_tap_values():
    original = ProcedureArtifact.model_validate(CORPUS["security.inventory-import.json"])
    call = next(node for node in original.definition.nodes if node.kind == "call")
    call_schema = next(
        c.contract_schema
        for c in original.owned_contracts
        if c.identity == call.contract_out.target
    )
    authored = spec(
        procedure="security.inventory-import",
        input={"scope": "string"},
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
                "contract": "security.file-erasable-v1",
                "interface": "workspace.file@2",
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
                "interface": "security.runtime-inventory@1",
                "input": {
                    "inventory_text": "$inventory.content.text",
                    "held": "$held",
                    "runs": "$runs",
                    "patches": "$patches",
                    "scope": "$input.scope",
                },
            },
            {"guard": "parse.mine > 0", "else": "halt(other_scope)"},
            {"guard": "parse.proposes > 0", "else": "halt(no_change)"},
            {"propose": "$parse.items"},
        ],
    )

    def resolve(kind, step):
        return {
            "bindings": {},
            "output": call_schema.fields
            if hasattr(step, "call")
            else {"content": PropertySchema(type="json")},
            "input": None,
        }

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
    assert lowered.definition["nodes"][6].get("refusal_code") is None
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


def test_decompile_inventory_recognizes_return_and_halt_patterns():
    original = ProcedureArtifact.model_validate(CORPUS["security.inventory-import.json"])
    authored = decompile_procedure(original)
    assert len(authored.steps) == 8
    assert authored.returns.id == "summary"
    assert authored.steps[5].else_ == "halt(other_scope)"
    assert authored.steps[6].else_ == "halt(no_change)"


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


@pytest.mark.parametrize("population", [10, 100])
def test_authored_lowering_requests_only_the_named_interface(population):
    class Vocabulary(dict):
        def __iter__(self):
            raise AssertionError("authoring must not scan vocabulary")

        def values(self):
            raise AssertionError("authoring must not scan vocabulary")

        def items(self):
            raise AssertionError("authoring must not scan vocabulary")

    vocabulary = Vocabulary({f"unrelated.{i}": {} for i in range(population)})
    vocabulary["test@1"] = {"count": PropertySchema(type="integer")}
    requested = []

    def resolve(kind, step):
        requested.append((kind, step.interface))
        return {"bindings": {}, "input": {}, "output": vocabulary[step.interface]}

    authored = spec(
        input={},
        returns="read",
        steps=[{"call": "read", "interface": "test@1", "provider": "test", "input": {}}],
    )
    lowered = lower_procedure_spec(authored, resolve=resolve)
    assert requested == [("binding", "test@1")]
    assert lowered.definition["returns"] == "read"


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
    from tests.test_procedures.test_procedure_artifacts import _artifact, _layout_definition

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
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

    definition = _definition()
    transform = TransformNode(
        node_id="result",
        transform_kind="adapter",
        contract_in=definition.contract_in,
        contract_out=definition.contract_out,
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
