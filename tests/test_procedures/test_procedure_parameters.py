"""P5/G1: Procedure parameters are typed by the params Contract, defaulted on the
Procedure, supplied by a Line (or a direct run), and read by ``$params`` refs."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.procedures.authored import (
    ProcedureSpec,
    ProcedureSpecError,
    ProjectStep,
    lower_procedure_spec,
)
from cruxible_client.contracts.procedures.models import ProcedureDefinition
from cruxible_core.authoring.lowering import AuthoringLoweringError
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequest,
    service_run_playbill_procedure,
)
from tests.support.operation_world import NOW, actor, author_line, author_procedure, world
from tests.support.procedures import procedure_limits, procedure_spec, ref

# Executor-only fixtures: these tests author Procedure step kinds the template
# closes at authoring (D-R37 h1) to cover their executor handlers (h3).
pytestmark = pytest.mark.executor_only

LIMITS = procedure_limits(provider_calls=0)


def _spec(**fields: Any) -> ProcedureSpec:
    """A Procedure returning its ``limit`` parameter, defaulted to 3."""

    return procedure_spec(
        procedure="test.limited",
        params={"limit": {"type": "int", "default": 3}},
        output={"limit": "int"},
        steps=[ProjectStep(project="result", fields={"limit": ref("$params.limit")})],
        returns="result",
        hard_caps=LIMITS,
        changes=fields,
    )


def _admission(instance: Any, run_id: str) -> Any:
    from cruxible_core.exhaust.payloads import read_payload
    from cruxible_core.procedures.execution import parse_admission_payload
    from cruxible_core.service.procedures.measurements import _journal
    from cruxible_core.storage.cas import BodyAccessContext

    journal, stream = _journal(instance)
    for stored in journal.select_records(stream, event_kind="admission_bound"):
        if stored.record.run_id == run_id:
            payload = read_payload(
                instance.body_store(),
                stored.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
            return parse_admission_payload(payload).admission
    raise AssertionError(f"no admission for run {run_id!r}")


def test_inline_default_sugar_lowers_into_the_parameter_defaults_map() -> None:
    sugared = lower_procedure_spec(_spec())
    explicit = lower_procedure_spec(_spec(params={"limit": "int"}, parameter_defaults={"limit": 3}))
    assert sugared.definition["parameter_defaults"] == {"limit": 3}
    # The params Contract stays shape-only: the default is not part of it.
    assert sugared.generated["params"]["limit"].spelling == "int"
    assert sugared.definition == explicit.definition
    assert sugared.generated == explicit.generated
    # A $params reference is a value template (G1).
    assert sugared.definition["nodes"][0]["fields"] == {"limit": "$params.limit"}


def test_a_defaulted_procedure_decompiles_to_a_spec_that_lowers_back() -> None:
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.procedures.artifacts import ProcedureArtifact
    from cruxible_client.contracts.procedures.decompile import decompile_procedure
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from tests.support.procedures import boundary_contract, contract_resolver

    lowered = lower_procedure_spec(_spec())
    definition = ProcedureDefinition.model_validate(lowered.definition)
    artifact = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        activation_policy="snapshot",
    )
    contracts = contract_resolver(
        *(
            boundary_contract(
                f"test.limited.{role}",
                {name: field.spelling for name, field in fields.items()},
            )
            for role, fields in lowered.generated.items()
        )
    )
    spec = decompile_procedure(artifact, contracts=contracts)
    assert spec.parameter_defaults == {"limit": 3}
    assert lower_procedure_spec(spec).definition == lowered.definition


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        (
            {
                "params": {"limit": {"type": "int", "default": 3}},
                "parameter_defaults": {"limit": 4},
            },
            "procedure_parameter_default_ambiguous",
        ),
        (
            {"input": {"value": {"type": "int", "default": 1}}},
            "procedure_default_not_a_parameter",
        ),
    ],
)
def test_a_default_is_stated_once_and_only_on_a_parameter(fields: Any, code: str) -> None:
    with pytest.raises(ValidationError, match=code):
        _spec(**fields)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"parameter_defaults": {"other": 1}}, "procedure_parameter_default_undeclared"),
        (
            {"params": {"limit": {"type": "int", "default": "x"}}},
            "procedure_parameter_default_invalid",
        ),
        (
            {"params": {"limit": {"type": "int{min: 5}", "default": 3}}},
            "procedure_parameter_default_invalid",
        ),
    ],
)
def test_each_default_must_fit_its_params_field(fields: Any, code: str) -> None:
    with pytest.raises(ProcedureSpecError) as caught:
        lower_procedure_spec(_spec(**fields))
    assert caught.value.code == f"cruxible.authoring.{code}"


def test_a_definition_without_params_states_no_defaults() -> None:
    lowered = lower_procedure_spec(_spec())
    raw = dict(lowered.definition)
    raw.pop("parameter_contract")
    with pytest.raises(ValidationError, match="procedure_parameter_default_undeclared"):
        ProcedureDefinition.model_validate(raw)


def test_acceptance_checks_each_default_against_the_accepted_params_contract() -> None:
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.procedures.artifacts import (
        ProcedureArtifact,
        evaluate_procedure_law,
        procedure_path,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from tests.support.procedures import boundary_contract, contract_resolver

    params = boundary_contract("test.limited.params", {"limit": "int"})
    other = boundary_contract("test.limited.other-params", {"limit": "string"})
    lowered = lower_procedure_spec(_spec())
    definition = ProcedureDefinition.model_validate(
        {**lowered.definition, "parameter_contract": {"target": params.identity}}
    )
    artifact = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        activation_policy="snapshot",
    )

    def law(*contracts: Any) -> Any:
        resolve = contract_resolver(*contracts)
        return evaluate_procedure_law(
            artifact,
            path=procedure_path(definition.name),
            predecessor=None,
            contract_lookup=resolve,
        )

    assert law(params).verdict == "accepted"
    # The same name at another shape: the default 3 is not a string.
    mistyped = other.model_copy(update={"name": params.name})
    refused = law(mistyped)
    assert refused.verdict == "refused"
    assert [item.code for item in refused.diagnostics] == [
        "cruxible.procedure.parameter_default_invalid"
    ]


def test_effective_parameters_are_the_run_values_over_the_defaults(tmp_path: Any) -> None:
    instance = world(tmp_path)
    author_procedure(instance, _spec())

    def run(**request: Any) -> Any:
        return service_run_playbill_procedure(
            instance,
            name="test.limited",
            request=ProcedureRunRequest(input={}, evaluation_time=NOW, **request),
            actor_context=actor(),
        )

    defaulted = run()
    assert defaulted.status == "succeeded", defaulted.terminal
    assert defaulted.result == {"limit": 3}
    # The run's admission (journaled in its receipt chain) records the effective values.
    assert _admission(instance, defaulted.run_id).parameters == {"limit": 3}

    supplied = run(parameters={"limit": 7})
    assert supplied.result == {"limit": 7}
    assert _admission(instance, supplied.run_id).parameters == {"limit": 7}
    assert supplied.run_id != defaulted.run_id

    # Checked against the params Contract after the defaults apply.
    refused = run(parameters={"limit": "seven"})
    assert refused.status == "node_refused"
    assert refused.terminal.code == "contract_input_refused"
    assert "test.limited.params@1" in refused.terminal.message
    undeclared = run(parameters={"other": 1})
    assert undeclared.terminal.code == "contract_input_refused"


def test_a_line_supplies_parameters_over_the_defaults(tmp_path: Any) -> None:
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.test_procedures.test_b2_operations import Clock

    instance = world(tmp_path)
    author_procedure(instance, _spec())

    def run_line(name: str) -> Any:
        return service_run_playbill_line(
            instance,
            path_identity_digest=name,
            request=LineRunRequest(line=name),
            actor_context=actor(),
            caller_rung=2,
            daemon_clock=Clock(NOW),
        )

    author_line(instance, name="test.defaulted", procedure="test.limited", parameters={})
    author_line(instance, name="test.supplied", procedure="test.limited", parameters={"limit": 9})
    defaulted = run_line("test.defaulted")
    assert defaulted.status == "succeeded", defaulted.terminal
    assert defaulted.result == {"limit": 3}
    supplied = run_line("test.supplied")
    assert supplied.result == {"limit": 9}
    assert _admission(instance, supplied.run_id).invocation_input == {}
    assert _admission(instance, supplied.run_id).parameters == {"limit": 9}

    # A Line whose parameters the params Contract refuses is refused when authored.
    with pytest.raises(AuthoringLoweringError) as refused:
        author_line(
            instance, name="test.mistyped", procedure="test.limited", parameters={"limit": "x"}
        )
    assert refused.value.code == "cruxible.authoring.line_parameters_refused"


def test_a_line_run_takes_no_input(tmp_path: Any) -> None:
    """A Line supplies parameters: a Procedure whose input requires a field
    cannot be run by a Line, and the refusal says to declare a parameter."""

    instance = world(tmp_path)
    author_procedure(
        instance,
        procedure_spec(
            procedure="test.inputs",
            input={"limit": "int"},
            output={"limit": "int"},
            steps=[ProjectStep(project="result", fields={"limit": ref("$input.limit")})],
            returns="result",
            hard_caps=LIMITS,
        ),
    )
    with pytest.raises(AuthoringLoweringError) as refused:
        author_line(instance, name="test.inputs", procedure="test.inputs", parameters={"limit": 1})
    assert refused.value.code == "cruxible.authoring.line_parameters_refused"
    assert "a Line supplies parameters" in refused.value.message


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("defaulted", [False, True])
def test_enum_parameters_defaults_lines_law_and_carry(tmp_path, nested, defaulted):
    from cruxible_client.contracts.procedures.artifacts import (
        AcceptedProcedure,
        evaluate_procedure_law,
        parse_procedure,
        procedure_artifact_digest,
        procedure_path,
    )
    from cruxible_client.contracts.procedures.line_specs import (
        evaluate_line_spec_law,
        line_spec_path,
        parse_line_spec,
        render_line_spec,
    )
    from cruxible_client.contracts.shapes import tree_contract_resolver
    from cruxible_core.claims.enum_resolution import tree_enum_resolver
    from cruxible_core.providers.automation_carry import validate_carried_line
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._knowledge_loop_support import seed_claims
    from tests.support.procedures import boundary_contract, contract_files

    instance, _ = seed_claims(tmp_path)
    inner = boundary_contract("test.enum-inner", {"status": "enum_of:project.work_item.status"})
    accept_tree(instance, contract_files(inner), name="enum-inner")
    field_type = "test.enum-inner@1" if nested else "enum_of:project.work_item.status"
    value = {"status": "ready"} if nested else "ready"
    bad_value = {"status": "unknown"} if nested else "unknown"
    spec = _spec(
        params={"limit": field_type},
        output={"limit": field_type},
        parameter_defaults={"limit": value} if defaulted else {},
    )
    # Offline lowering defers vocabulary-dependent defaults until acceptance.
    lower_procedure_spec(spec)
    content = author_procedure(instance, spec)
    artifact = parse_procedure(content, path=procedure_path(spec.procedure))
    accepted = AcceptedProcedure(
        path=procedure_path(spec.procedure),
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
    )
    author_line(
        instance,
        name="test.enum-line",
        procedure=spec.procedure,
        parameters={} if defaulted else {"limit": value},
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    resolve, enums = tree_contract_resolver(tree), tree_enum_resolver(tree)
    line_path = line_spec_path("test.enum-line")
    line = parse_line_spec(tree[line_path], path=line_path)
    assert (
        evaluate_line_spec_law(
            line,
            path=line_path,
            procedure=accepted,
            predecessor=None,
            contracts=resolve,
            enum_members=enums,
        ).verdict
        == "accepted"
    )
    validate_carried_line(
        tree[line_path], path=line_path, procedure=accepted, contracts=resolve, enum_members=enums
    )
    bad_line = line.model_copy(update={"parameters": {"limit": bad_value}})
    assert (
        evaluate_line_spec_law(
            bad_line,
            path=line_path,
            procedure=accepted,
            predecessor=None,
            contracts=resolve,
            enum_members=enums,
        ).verdict
        == "refused"
    )
    with pytest.raises(ValueError, match="refuses"):
        validate_carried_line(
            render_line_spec(bad_line),
            path=line_path,
            procedure=accepted,
            contracts=resolve,
            enum_members=enums,
        )
    with pytest.raises(AuthoringLoweringError, match="refuses"):
        author_line(
            instance,
            name="test.enum-bad",
            procedure=spec.procedure,
            parameters={"limit": bad_value},
        )
    bad_definition = artifact.definition.model_copy(
        update={"parameter_defaults": {"limit": bad_value}}
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest

    bad_artifact = artifact.model_copy(
        update={
            "definition": bad_definition,
            "definition_digest": compute_procedure_definition_digest(bad_definition).tagged,
        }
    )
    law = evaluate_procedure_law(
        bad_artifact,
        path=accepted.path,
        predecessor=None,
        contract_lookup=resolve,
        enum_members=enums,
    )
    assert (
        law.verdict == "refused"
        and law.diagnostics[0].code == "cruxible.procedure.parameter_default_invalid"
    )
    # Runtime Line checks resolve enums at the same admitted coordinate too.
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.test_procedures.test_b2_operations import Clock

    result = service_run_playbill_line(
        instance,
        path_identity_digest=line.identity.name,
        request=LineRunRequest(line=line.identity.name),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=Clock(NOW),
    )
    assert result.status == "succeeded", result.terminal
    assert result.result == {"limit": value}


@pytest.mark.parametrize("value", ["1.0", "0.50"])
def test_noncanonical_number_defaults_refuse_before_hashing(value):
    with pytest.raises(ProcedureSpecError, match="noncanonical number"):
        lower_procedure_spec(
            _spec(
                params={"limit": "number"},
                output={"limit": "number"},
                parameter_defaults={"limit": value},
            )
        )


def test_number_defaults_and_parameters_have_one_successful_wire_encoding(tmp_path):
    from cruxible_client.contracts.procedures.artifacts import (
        parse_procedure,
        procedure_artifact_digest,
    )
    from cruxible_client.contracts.shapes import canonical_number

    spec = _spec(
        params={"limit": "number"}, output={"limit": "number"}, parameter_defaults={"limit": 1}
    )
    same = _spec(
        params={"limit": "number"},
        output={"limit": "number"},
        parameter_defaults={"limit": canonical_number("1.0")},
    )
    assert lower_procedure_spec(spec) == lower_procedure_spec(same)
    instance = world(tmp_path)
    content = author_procedure(instance, spec)
    artifact = parse_procedure(content, path="procedures/test.limited.json")
    assert artifact.definition.parameter_defaults == {"limit": 1}
    assert procedure_artifact_digest(artifact) == procedure_artifact_digest(
        parse_procedure(content, path="procedures/test.limited.json")
    )
    valid = service_run_playbill_procedure(
        instance,
        name=spec.procedure,
        request=ProcedureRunRequest(input={}, parameters={"limit": 1}, evaluation_time=NOW),
        actor_context=actor(),
    )
    assert valid.status == "succeeded" and valid.result == {"limit": 1}
    refused = service_run_playbill_procedure(
        instance,
        name=spec.procedure,
        request=ProcedureRunRequest(input={}, parameters={"limit": "1.0"}, evaluation_time=NOW),
        actor_context=actor(),
    )
    assert refused.terminal.code == "contract_input_refused"
    assert "noncanonical number" in refused.terminal.message


def test_acceptance_refuses_nested_noncanonical_number_defaults(tmp_path):
    from cruxible_client.contracts.procedures.artifacts import parameter_default_problem
    from cruxible_client.contracts.shapes import contract_ref_for_identity
    from tests.support.procedures import boundary_contract, contract_resolver

    inner = boundary_contract("test.numbers", {"n": "number"})
    params = boundary_contract("test.limited.params", {"limit": "test.numbers@1"}, inner)
    definition = ProcedureDefinition.model_validate(lower_procedure_spec(_spec()).definition)
    resolve = contract_resolver(inner, params)
    assert resolve(contract_ref_for_identity(definition.parameter_contract.target)) == params
    invalid = definition.model_copy(update={"parameter_defaults": {"limit": {"n": "0.50"}}})
    valid = definition.model_copy(update={"parameter_defaults": {"limit": {"n": "0.5"}}})
    assert "noncanonical number" in parameter_default_problem(invalid, resolve)
    assert parameter_default_problem(valid, resolve) is None
