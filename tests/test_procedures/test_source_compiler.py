"""Authored Python is compiled, never run, and the resulting graph is replayable."""

import textwrap

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.contracts import OwnedProcedureContractValidator
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.source_compiler import SourceCompileError, compile_source
from cruxible_client.contracts.procedures.source_program import ProcedureSourceV1, SourceContract
from cruxible_core.procedures.execution import ProcedureExecutor
from tests.test_procedures.test_procedure_execution import (
    _Authority,
    _budget,
    _fixture,
    _hard_caps,
    _prepare,
    _StateReader,
)

INPUT = SourceContract(
    name="input",
    schema=ContractSchema(
        fields={
            "choice": PropertySchema(type="bool"),
            "count": PropertySchema(type="int"),
        }
    ),
)
OUTPUT = SourceContract(
    name="output",
    schema=ContractSchema(
        fields={
            "value": PropertySchema(type="string"),
        }
    ),
)


def compile(text, *, output=OUTPUT, bindings=None):
    return compile_source(
        ProcedureSourceV1(
            text=textwrap.dedent(text),
            filename="example.py",
            first_line=40,
            function="example",
            contracts={"Output": output},
            bindings=bindings or {},
        ),
        name="example",
        input=INPUT,
        output=output,
        budget=_budget(),
        hard_caps=_hard_caps(),
    )


def accepted(compiled):
    definition = compiled.definition
    owned = tuple(
        ProcedureOwnedContractV1(
            identity=ArtifactIdentity(kind="Contract", name=c.name), schema=c.schema_
        )
        for c in compiled.contracts
    )
    # Compile tests use only owner-carried contracts.
    from cruxible_client.contracts.procedures.models import iter_pin_bindings

    pins = tuple(
        sorted(
            set(iter_pin_bindings(definition)),
            key=lambda p: (p.role.encode(), p.target.qualified.encode()),
        )
    )
    artifact = ProcedureArtifactV2(
        identity=ArtifactIdentity(kind="Procedure", name="example"),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=pins,
        owned_contracts=tuple(
            sorted(owned, key=lambda c: canonical_bytes(c.model_dump(mode="json")))
        ),
        activation_policy="snapshot",
    )
    return AcceptedProcedureV1(
        path=procedure_path("example"),
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
    )


def execute(tmp_path, compiled, **inputs):
    artifact = accepted(compiled)
    fixture = _fixture(tmp_path)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(artifact.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(artifact),
    )
    preparation = _prepare(artifact, fixture, _StateReader(), invocation_input=inputs)
    result = executor.execute(preparation, artifact)
    assert executor.execute(preparation, artifact).output == result.output
    return result


@pytest.mark.parametrize(
    "choice,count,expected", [(False, 0, "none"), (True, 1, "low"), (True, 3, "high")]
)
def test_nested_branches_join_and_execute(tmp_path, choice, count, expected):
    compiled = compile("""
        def example(request):
            if request.choice:
                if request.count >= 2:
                    value = 'high'
                else:
                    value = 'low'
            else:
                value = 'none'
            return Output.value(value=value)
    """)
    result = execute(tmp_path, compiled, choice=choice, count=count)
    assert result.status == "succeeded"
    assert result.output == {"value": expected}
    assert compiled.source_map[-1].span.filename == "example.py"
    assert compiled.source_map[-1].span.line == 49


def test_literals_cannot_be_interpreted_as_graph_references(tmp_path):
    compiled = compile("""
        def example(request):
            return Output.value(value='$input.secret')
    """)
    result = execute(tmp_path, compiled, choice=True, count=0)
    assert result.output == {"value": "$input.secret"}


def test_retained_source_cannot_disagree_with_accepted_graph():
    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_client.contracts.procedures.source_compiler import verify_source_graph

    compiled = compile("""
        def example(request):
            return Output.value(value='original')
    """)
    artifact = accepted(compiled).procedure
    verify_source_graph(artifact)
    definition = artifact.definition.model_copy(
        update={
            "nodes": (
                artifact.definition.nodes[0].model_copy(update={"fields": {"value": "changed"}}),
            )
        }
    )
    changed = artifact.model_copy(update={"definition": definition})
    with pytest.raises(ProjectionFormatError, match="disagree"):
        verify_source_graph(changed)


def test_nested_record_fields_cannot_silently_change_types():
    result = SourceContract(
        name="output",
        schema=ContractSchema(
            fields={
                "record": PropertySchema(
                    type="json",
                    json_schema={
                        "type": "object",
                        "properties": {"flag": {"type": "boolean"}},
                        "required": ["flag"],
                        "additionalProperties": False,
                    },
                ),
            }
        ),
    )
    with pytest.raises(SourceCompileError, match="declared type"):
        compile(
            """
            def example(request):
                return Output.value(record=request)
        """,
            output=result,
        )


def test_graph_six_checks_nested_runtime_values(tmp_path):
    from cruxible_client.contracts.procedures.contracts import ProcedureContractValidationError

    schema = ContractSchema(
        fields={
            "metadata": PropertySchema(
                type="json",
                json_schema={
                    "type": "object",
                    "properties": {"flag": {"type": "boolean"}},
                    "required": ["flag"],
                    "additionalProperties": False,
                },
            )
        }
    )
    input = SourceContract(name="input", schema=schema)
    compiled = compile_source(
        ProcedureSourceV1(
            text="def example(request):\n    return Output.value(value='ok')\n",
            filename="example.py",
            function="example",
            contracts={"Output": OUTPUT},
        ),
        name="example",
        input=input,
        output=OUTPUT,
        budget=_budget(),
        hard_caps=_hard_caps(),
    )
    artifact = accepted(compiled)
    validator = OwnedProcedureContractValidator(artifact)
    with pytest.raises(ProcedureContractValidationError, match="JSON schema"):
        validator.validate_contract(
            contract=artifact.procedure.definition.contract_in,
            payload={"metadata": {"flag": "yes"}},
            direction="input",
        )


def test_short_circuit_routes_do_not_run_other_arm(tmp_path):
    compiled = compile("""
        def example(request):
            require(request.choice or request.count > 10, code='closed', message='closed')
            return Output.value(value='open')
    """)
    result = execute(tmp_path, compiled, choice=True, count=0)
    assert result.output == {"value": "open"}
    # The second predicate is a distinct graph node, not an eagerly evaluated compound.
    guards = [n for n in compiled.definition.nodes if n.kind == "guard"]
    assert len(guards) == 2
    assert guards[0].on_true != guards[1].node_id


@pytest.mark.parametrize(
    "body,code",
    [
        ("return Output.value(value=open('/etc/passwd').read())", "name_unavailable"),
        ("for x in range(10):\n    pass", "unsupported_construct"),
        ("return Output.value(value=42)", "contract_value_invalid"),
        ('return Output.value(typo="x")', "record_field"),
        (
            'if request.choice:\n    value = "yes"\nreturn Output.value(value=value)',
            "name_unavailable",
        ),
        ('return Output.value(value="yes")\nreturn Output.value(value="no")', "unreachable"),
        ('value = "x"', "missing_return"),
    ],
)
def test_unsupported_or_invalid_source_refuses_at_location(body, code):
    source = "def example(request):\n" + textwrap.indent(body, "    ")
    with pytest.raises(SourceCompileError) as caught:
        compile(source)
    assert caught.value.diagnostic.code == "playbill.source." + code
    assert caught.value.diagnostic.span.line >= 40
    assert caught.value.diagnostic.span.filename == "example.py"


def test_query_binds_request_parameters_and_types_the_result():
    from cruxible_client.contracts.procedures.source_program import SourceQueryBinding
    from cruxible_client.contracts.query.definitions import query_definition_digest
    from tests.test_query.test_query_definitions import active_work_query

    definition = active_work_query()
    compiled = compile(
        """
        def example(request, bindings):
            result = query(bindings.work, parameters=bindings.work.parameters(status='ready'))
            require(result.completed and not result.truncated,
                    code='incomplete', message='Incomplete')
            if result.result.truncation.returned_result_count > request.count:
                return Output.value(value='more')
            return Output.value(value='less')
    """,
        bindings={
            "work": SourceQueryBinding(
                name=definition.identity.name,
                version=query_definition_digest(definition).tagged,
                definition=definition,
            )
        },
    )
    assert compiled.definition.nodes[0].kind == "state_tap"
    assert compiled.definition.nodes[0].parameters == {"status": "ready"}


def test_capture_terminal_has_declared_result_and_requires_material_schema():
    from cruxible_client.contracts.procedures.source_program import SourceProviderBinding
    from cruxible_client.contracts.procedures.source_views import object_field
    from cruxible_client.contracts.provider_contracts import (
        ACQUISITION_RESULT,
        ProviderOperationContractV1,
    )

    provider = SourceProviderBinding(
        provider="web",
        provider_version="sha256:" + "1" * 64,
        interface="web.fetch",
        interface_version="sha256:" + "2" * 64,
        interface_digest="sha256:" + "3" * 64,
        implementation_digest="sha256:" + "4" * 64,
        effect_class="external_read",
        operation=ProviderOperationContractV1(
            input=ContractSchema(fields={"url": PropertySchema(type="string")}),
            output=ACQUISITION_RESULT,
            material=ContractSchema(
                fields={"retrieved": object_field({"final_url": {"type": "string"}})}
            ),
        ),
    )
    program = ProcedureSourceV1(
        text=textwrap.dedent("""
        def example(request, bindings):
            observation = source(bindings.fetch,
                request=bindings.fetch.input(url='https://example.test'), capture_contract='http')
            return emit_capture(observation, capture_contract='registered',
                result=Output.value(value=observation.retrieved.final_url))
    """),
        filename="capture.py",
        function="example",
        contracts={"Output": OUTPUT},
        bindings={"fetch": provider},
        capture_contracts={"http": "sha256:" + "5" * 64, "registered": "sha256:" + "6" * 64},
    )
    result = compile_source(
        program,
        name="example",
        input=INPUT,
        output=OUTPUT,
        budget=_budget(),
        hard_caps=_hard_caps(),
    )
    assert result.definition.nodes[0].as_ == "observation"
    assert result.definition.nodes[-1].result == {"value": "$steps.observation.retrieved.final_url"}
    missing = program.model_copy(
        update={
            "bindings": {
                "fetch": provider.model_copy(
                    update={"operation": provider.operation.model_copy(update={"material": None})}
                )
            }
        }
    )
    with pytest.raises(SourceCompileError, match="does not declare"):
        compile_source(
            missing,
            name="example",
            input=INPUT,
            output=OUTPUT,
            budget=_budget(),
            hard_caps=_hard_caps(),
        )


def test_field_read_is_an_admitted_selection_not_a_whole_world_query(tmp_path):
    from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
    from cruxible_client.contracts.procedures.source_program import SourceClaimType
    from cruxible_client.contracts.query.grammar import QueryBudgetsV1
    from cruxible_core.procedures.execution import StateTapReadResultV1

    program = ProcedureSourceV1(
        text=textwrap.dedent("""
        def example(request, world):
            asset = world.security.asset['app']
            exposed = asset.internet_facing.one()
            require(exposed.verdict == 'supported', code='unsupported', message='Unsupported')
            if exposed.value:
                return Output.value(value='public')
            return Output.value(value='private')
    """),
        filename="field.py",
        function="example",
        contracts={"Output": OUTPUT},
        subject_kinds=("security.asset",),
        claim_types={
            "security.asset.internet_facing": SourceClaimType(
                version="sha256:" + "1" * 64,
                structure=ClaimTypeStructure(
                    predicate="security.asset.internet_facing",
                    allowed_subject_kinds=("security.asset",),
                    object_kind="literal",
                    literal_schema={"type": "boolean"},
                    cardinality="one",
                    permitted_roles=("observation",),
                ),
            )
        },
    )
    compiled = compile_source(
        program,
        name="example",
        input=INPUT,
        output=OUTPUT,
        budget=_budget(),
        hard_caps=_hard_caps(),
    )
    assert compiled.definition.nodes[0].kind == "state_claim"
    assert compiled.definition.nodes[0].subject_id == "app"
    artifact = accepted(compiled)
    fixture = _fixture(tmp_path)
    calls = []

    class Reader(_StateReader):
        def read_accepted_claim(self, **kwargs):
            calls.append(kwargs)
            return StateTapReadResultV1(
                value={"value": True, "verdict": "supported"},
                effective_budgets=QueryBudgetsV1(max_results=256, max_traversal_depth=0),
            )

    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(artifact.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(artifact),
    )
    prepared = _prepare(artifact, fixture, Reader(), invocation_input={"choice": False, "count": 0})
    result = executor.execute(prepared, artifact)
    from tests.test_procedures.test_procedure_execution import _final_payload

    assert result.status == "succeeded", _final_payload(fixture)
    assert result.output == {"value": "public"}
    assert len(calls) == 1
    assert calls[0]["subject_kind"] == "security.asset"
    assert prepared.accepted_state_materials[0].input.kind == "accepted_claim"
    assert executor.execute(prepared, artifact).output == result.output
    assert len(calls) == 1


def test_required_nullable_source_output_executes_without_weakening_old_contracts(tmp_path):
    from cruxible_client.contracts.procedures.contracts import (
        ProcedureContractValidationError,
        validate_contract_schema,
    )

    output = SourceContract(
        name="nullable",
        schema=ContractSchema(
            fields={"value": PropertySchema(type="json", json_schema={"type": ["string", "null"]})}
        ),
    )
    compiled = compile(
        "def example(request):\n    return Output.value(value=None)\n", output=output
    )
    result = execute(tmp_path, compiled, choice=True, count=0)
    assert result.status == "succeeded", result
    assert result.output == {"value": None}
    # Historical Contract normalization has not changed its null rule.
    with pytest.raises(ProcedureContractValidationError):
        validate_contract_schema(output.schema_, {"value": None})


def test_query_defaults_and_explicit_budget_use_existing_contracts():
    from cruxible_client.contracts.procedures.source_program import SourceQueryBinding
    from cruxible_client.contracts.query.definitions import query_definition_digest
    from tests.test_query.test_query_definitions import active_work_query

    definition = active_work_query()
    binding = SourceQueryBinding(
        name=definition.identity.name,
        version=query_definition_digest(definition).tagged,
        definition=definition,
    )
    program = """
        def example(request, bindings):
            result = query(bindings.work,
                parameters=bindings.work.parameters(status='ready'),
                budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0,
                                      max_paths=1, max_paths_per_result=1))
            return Output.value(value=result.receipt.verdict)
    """
    compiled = compile(program, bindings={"work": binding})
    assert compiled.definition.nodes[0].budgets.max_results == 1
    with pytest.raises(SourceCompileError, match="ceiling"):
        compile(program.replace("max_results=1", "max_results=1000000"), bindings={"work": binding})
    # Required parameters cannot silently disappear when parameters is omitted.
    with pytest.raises(SourceCompileError):
        compile(
            program.replace("parameters=bindings.work.parameters(status='ready'),", ""),
            bindings={"work": binding},
        )


@pytest.mark.parametrize(
    "body,code",
    [
        (
            'if request.priority == "urgant":\n'
            '        return Output.value(value="yes")\n'
            '    return Output.value(value="no")',
            "enum_value",
        ),
        ('x = "a"\n    x = "b"\n    return Output.value(value=x)', "reassignment"),
    ],
)
def test_checked_source_refuses_silent_mistakes_without_rewriting_history(body, code):
    request = SourceContract(
        name="request",
        schema=ContractSchema(
            fields={"priority": PropertySchema(type="string", enum=["routine", "urgent"])}
        ),
    )
    source = ProcedureSourceV1(
        text="def example(request):\n    " + body + "\n",
        filename="checks.py",
        first_line=90,
        function="example",
        contracts={"Output": OUTPUT},
    )
    args = dict(
        name="example", input=request, output=OUTPUT, budget=_budget(), hard_caps=_hard_caps()
    )
    historical = compile_source(source, **args)
    from cruxible_client.contracts.procedures.source_compiler import verify_source_graph

    verify_source_graph(accepted(historical).procedure)
    with pytest.raises(SourceCompileError) as error:
        compile_source(source.model_copy(update={"rules": "cruxible.procedure-source.v2"}), **args)
    assert error.value.diagnostic.code == "playbill.source." + code
    assert error.value.diagnostic.span.filename == "checks.py"
    assert error.value.diagnostic.span.line >= 91
    assert (
        accepted(compile_source(source, **args)).artifact_digest
        == accepted(historical).artifact_digest
    )


def test_checked_source_has_explicit_returns_and_keeps_branch_local_bindings(tmp_path):
    source = ProcedureSourceV1(
        rules="cruxible.procedure-source.v2",
        text="""def example(request):
    if request.choice:
        label = "yes"
    else:
        label = "no"
    return Output.value(value=label)
""",
        filename="branches.py",
        function="example",
        contracts={"Output": OUTPUT},
    )
    compiled = compile_source(
        source, name="example", input=INPUT, output=OUTPUT, budget=_budget(), hard_caps=_hard_caps()
    )
    assert compiled.definition.returns is None
    assert execute(tmp_path, compiled, choice=True, count=0).output == {"value": "yes"}
    with pytest.raises(ValueError, match="explicit return paths"):
        type(compiled.definition).model_validate(
            {**compiled.definition.model_dump(), "returns": "invented"}
        )
