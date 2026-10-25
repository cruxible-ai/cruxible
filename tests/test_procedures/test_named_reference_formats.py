"""New reference grammars preserve authored payloads and historical bytes."""

import json

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    CanonicalDuration,
    CaptureContract,
    CaptureRetentionErasurePolicy,
    CaptureSelectionBudget,
    authorize_erasure,
    capture_components,
    capture_contract_digest,
    capture_contract_path,
    capture_retention_policy,
    capture_shape_components,
    evaluate_capture_contract_law,
    name_capture_contract,
    parse_capture_contract,
    render_capture_contract,
)


@pytest.mark.parametrize(
    "shape", ["http_response", "procedure_output", "foreign_span", "database_snapshot"]
)
@pytest.mark.parametrize("erasable", [False, True])
def test_named_capture_components_keep_their_interpretation_without_stored_pins(shape, erasable):
    previous = CaptureContract(
        identity=ArtifactIdentity(kind="CaptureContract", name="test.capture"),
        shape=shape,
        replay="exact",
        components=capture_shape_components(shape, "exact"),
        selection_budget=CaptureSelectionBudget(max_bytes=100, max_rows=1, max_items=1),
        retention_erasure_policy=CaptureRetentionErasurePolicy(
            body_retention="required_for_duration",
            minimum_retention=CanonicalDuration(microseconds=1),
            erasure="prohibited",
            selector_privacy="direct_allowed",
        ),
    )
    if erasable:
        previous = authorize_erasure(previous)
    original = render_capture_contract(previous)
    digest = capture_contract_digest(previous)
    named = name_capture_contract(previous)
    wire = json.loads(render_capture_contract(named))
    assert capture_components(named) == capture_components(previous)
    assert capture_retention_policy(named) == capture_retention_policy(previous)
    assert "artifact_digest" not in json.dumps(wire)
    assert "role" not in json.dumps(wire)
    assert "digest" not in json.dumps(wire["components"])
    assert "digest" not in json.dumps(wire["retention_erasure_policy"])
    assert wire["lifecycle"]["predecessor_digest"] is None
    assert "pins" not in wire
    assert not hasattr(named, "pins")
    path = capture_contract_path(named.identity.name)
    assert parse_capture_contract(render_capture_contract(named), path=path) == named
    assert evaluate_capture_contract_law(named, path=path, predecessor=None).verdict == "accepted"
    assert render_capture_contract(previous) == original
    assert capture_contract_digest(previous) == digest


def test_named_procedure_is_admitted_and_runs_without_rewriting_its_authored_graph(tmp_path):
    from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
    from cruxible_client.contracts.procedures.models import ConstantNode
    from cruxible_client.contracts.procedures.names import name_procedure
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._support import initialize_local
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition
    from tests.test_procedures.test_procedure_execution import _actor

    definition = _definition()
    definition = definition.model_copy(
        update={
            "nodes": (
                ConstantNode(
                    node_id="result", contract_out=definition.contract_out, fields={}, as_="result"
                ),
            )
        }
    )
    artifact = name_procedure(_artifact(definition))
    instance, _ = initialize_local(tmp_path)
    path = procedure_path(artifact.identity.name)
    original = render_procedure(artifact)
    accept_tree(instance, {path: original}, name="named-procedure")
    before = instance.accepted_coordinate()
    result = service_run_playbill_procedure(
        instance,
        name=artifact.identity.name,
        request=ProcedureRunRequest(input={}),
        actor_context=_actor(),
    )
    assert result.status == "succeeded", result
    assert instance.blob_at(before.git_oid, path) == original


def test_named_provider_keeps_install_code_identity_and_resolves_registration_by_name():
    from cruxible_client.contracts.named_references import name_provider
    from cruxible_client.contracts.provider_interfaces import (
        AcceptedProviderInterfaceRegistration,
        provider_interface_digest,
        provider_interface_path,
    )
    from cruxible_client.contracts.providers import (
        evaluate_provider_law,
        parse_provider,
        provider_path,
        render_provider,
    )
    from tests.core_support._p2b1_support import interface_registration, provider_v2
    from tests.support.provider_builds import as_build

    old = as_build(provider_v2())
    named = name_provider(old)
    registration = interface_registration()
    accepted = AcceptedProviderInterfaceRegistration(
        path=provider_interface_path(registration.identity.name),
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )
    assert named.implementations == old.implementations
    assert "pins" not in named.model_dump()
    assert "capture_contract_digests" not in named.model_dump()
    assert "artifact_digest" not in json.dumps(named.model_dump(mode="json")["interfaces"])
    assert parse_provider(render_provider(named), path=provider_path(named.identity.name)) == named
    assert (
        evaluate_provider_law(
            named,
            path=provider_path(named.identity.name),
            predecessor=None,
            interface_registrations={registration.identity.qualified: accepted},
        ).verdict
        == "accepted"
    )


def test_named_line_and_trigger_keep_their_bytes_when_contract_advances(tmp_path):
    from datetime import UTC, datetime, timedelta

    from cruxible_client.contracts.artifacts import ArtifactLifecycle, ArtifactRef
    from cruxible_client.contracts.captures import foreign_source_capture_contract
    from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
    from cruxible_client.contracts.named_references import name_line, name_trigger
    from cruxible_client.contracts.procedures.artifacts import (
        AcceptedProcedure,
        procedure_artifact_digest,
        procedure_path,
        render_procedure,
    )
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_path,
        parse_line_spec,
        render_line_spec,
    )
    from cruxible_client.contracts.procedures.models import ConstantNode
    from cruxible_client.contracts.procedures.names import name_procedure
    from cruxible_client.contracts.procedures.windows import CaptureEventSelector
    from cruxible_client.contracts.triggers import (
        CaptureLandingSchedule,
        LineTarget,
        Trigger,
        parse_trigger,
        render_trigger,
        trigger_path,
        trigger_schedule_pins,
    )
    from cruxible_core.service.procedures.line_dispatch import service_evaluate_line
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._support import initialize_local
    from tests.test_procedures.test_line_specs import _line
    from tests.test_procedures.test_line_triggers import capture
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

    definition = _definition()
    definition = definition.model_copy(
        update={
            "nodes": (
                ConstantNode(
                    node_id="result", contract_out=definition.contract_out, fields={}, as_="result"
                ),
            )
        }
    )
    procedure = name_procedure(_artifact(definition))
    accepted = AcceptedProcedure(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    line = name_line(_line(requested_rung=1)[0])
    contract = foreign_source_capture_contract("named.feed")
    schedule = CaptureLandingSchedule(
        event=CaptureEventSelector(
            capture_contract_identity=contract.identity,
            capture_contract_digest=capture_contract_digest(contract).tagged,
        )
    )
    trigger = name_trigger(
        Trigger(
            identity=ArtifactIdentity(kind="Trigger", name="on-feed"),
            target=LineTarget(line=ArtifactRef(role="line", target=line.identity)),
            schedule=schedule,
            pins=trigger_schedule_pins(schedule),
        ).model_copy()
    )
    instance, _ = initialize_local(tmp_path)
    lpath, tpath, cpath = (
        line_spec_path(line.identity.name),
        trigger_path(trigger.identity.name),
        capture_contract_path(contract.identity.name),
    )
    lb, tb = render_line_spec(line), render_trigger(trigger)
    assert parse_line_spec(lb, path=lpath) == line
    assert parse_trigger(tb, path=tpath) == trigger
    assert "capture_contract_digest" not in tb.decode()
    accept_tree(
        instance,
        {
            accepted.path: render_procedure(procedure),
            lpath: lb,
            tpath: tb,
            cpath: render_capture_contract(contract),
        },
        name="named-trigger",
    )
    now = datetime(2026, 10, 25, 4, tzinfo=UTC)
    event = capture(instance, accepted, at=now, digest=capture_contract_digest(contract).tagged)
    successor = contract.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=capture_contract_digest(contract).tagged
            ),
            "selection_budget": contract.selection_budget.model_copy(
                update={"max_bytes": contract.selection_budget.max_bytes * 2}
            ),
        }
    )
    accept_tree(instance, {cpath: render_capture_contract(successor)}, name="contract-successor")
    result = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(until=now + timedelta(seconds=1), dry_run=True),
        actor=None,
        now=now + timedelta(seconds=1),
    )
    assert result.status == "met", result
    assert result.occurrences[0].binding.event.record_digest == event.record_digest
    coordinate = instance.accepted_coordinate()
    assert instance.blob_at(coordinate.git_oid, lpath) == lb
    assert instance.blob_at(coordinate.git_oid, tpath) == tb


def test_named_signing_and_package_providers_retain_capture_declarations():
    from cruxible_client.contracts.named_references import name_provider, name_signing_provider
    from cruxible_client.contracts.providers import (
        parse_provider,
        provider_declares_capture_contract,
        provider_path,
        render_provider,
    )
    from tests.core_support._pc_c_support import capture_contract, provider
    from tests.support.provider_builds import capture_provider_build

    contract = capture_contract()
    for original, named in (
        (provider(contract), name_signing_provider(provider(contract))),
        (capture_provider_build(contract), name_provider(capture_provider_build(contract))),
    ):
        before = render_provider(original)
        wire = named.model_dump(mode="json")
        assert wire["capture_contracts"] == [{"target": contract.identity.model_dump()}]
        assert "pins" not in wire
        assert "capture_contract_digests" not in wire
        assert provider_declares_capture_contract(named, contract.identity)
        assert (
            parse_provider(render_provider(named), path=provider_path(named.identity.name)) == named
        )
        assert render_provider(original) == before


def test_named_blueprint_instantiates_names_and_admission_refuses_ambiguous_slots():
    from cruxible_client.contracts.errors import ExecutionError
    from cruxible_client.contracts.procedures.artifacts import (
        AcceptedProcedure,
        procedure_artifact_digest,
        procedure_path,
    )
    from cruxible_client.contracts.procedures.blueprints import (
        AcceptedBlueprint,
        blueprint_digest,
        blueprint_path,
        instantiate_blueprint,
    )
    from cruxible_client.contracts.procedures.names import name_procedure
    from cruxible_core.procedures.reference_binding import bind_procedure_references
    from tests.core_support._p2b1_support import accepted_interface, accepted_provider
    from tests.test_procedures.test_blueprints import _blueprint

    old = _blueprint()
    named = name_procedure(old)
    accepted_blueprint = AcceptedBlueprint(
        path=blueprint_path(named.identity.name),
        blueprint=named,
        artifact_digest=blueprint_digest(named).tagged,
    )
    assert type(accepted_blueprint.blueprint) is type(named)
    interface = accepted_interface()
    provider = accepted_provider()
    registrations = {interface.registration.identity.qualified: interface}
    instance = instantiate_blueprint(
        named,
        blueprint_artifact_digest=blueprint_digest(named).tagged,
        name="named-instance",
        providers={"lookup": provider},
        interface_registrations=registrations,
    )
    wire = instance.model_dump(mode="json")
    assert wire["definition"]["nodes"][0]["provider"] == {
        "target": provider.provider.identity.model_dump()
    }
    assert "artifact_digest" not in json.dumps(wire["blueprint"])
    assert "pins" not in wire
    artifact = type(instance).model_validate(
        {
            **named.model_dump(exclude={"artifact_format", "identity"}),
            "identity": {"kind": "Procedure", "name": named.identity.name},
        }
    )
    slotted = AcceptedProcedure(
        path=procedure_path(named.identity.name),
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
    )

    def lookup(identity):
        return interface.registration, interface.artifact_digest

    with pytest.raises(ExecutionError, match="one fitting build"):
        bind_procedure_references(
            slotted,
            lookup=lookup,
            slots=lambda identity: (
                (provider.provider, provider.artifact_digest),
                (provider.provider, provider.artifact_digest),
            ),
        )


@pytest.mark.parametrize(
    "path,old,new",
    [
        ("procedures/check.json", "playbill-procedure-v2", "cruxible-procedure-v3"),
        ("blueprints/check.json", "cruxible-blueprint-v1", "cruxible-blueprint-v2"),
        (
            "query-definitions/check.json",
            "playbill-query-definition-v2",
            "cruxible-query-definition-v3",
        ),
        ("lines/check.json", "playbill-line-v6", "cruxible-line-v7"),
        ("triggers/check.json", "playbill-trigger-v1", "cruxible-trigger-v2"),
        (
            "capture-contracts/check.json",
            "cruxible-capture-contract-v2",
            "cruxible-capture-contract-v3",
        ),
        ("providers/check.json", "cruxible-provider-v4", "cruxible-provider-v5"),
        ("providers/check.json", "playbill-provider-v1", "cruxible-provider-identity-v2"),
        (
            "provider-interfaces/check.json",
            "cruxible-provider-interface-v3",
            "cruxible-provider-interface-v4",
        ),
    ],
)
def test_new_formats_are_admitted_only_at_compiler_36_and_old_bytes_remain_verifiable(
    path, old, new
):
    from types import SimpleNamespace

    from cruxible_client.contracts.errors import FormatError
    from cruxible_client.contracts.laws import ACCEPTANCE_LAWS
    from cruxible_core.compiler.compiler import CLAIM_REBIND_COMPILER, LIGHT_ARTIFACTS_COMPILER
    from cruxible_core.proposals.proposals import _installed

    def context(compiler):
        return SimpleNamespace(
            path=path,
            current=SimpleNamespace(compiler=compiler),
            acceptance_laws=ACCEPTANCE_LAWS,
            historical_law_coordinate=None,
            rechecking=False,
        )

    before, current = context(CLAIM_REBIND_COMPILER), context(LIGHT_ARTIFACTS_COMPILER)
    assert _installed(before, old).artifact_tag == old
    assert _installed(current, new).artifact_tag == new
    with pytest.raises(FormatError, match="upgrade the compiler"):
        _installed(before, new)
    with pytest.raises(FormatError, match="upgrade the compiler"):
        _installed(current, old)
    law = ACCEPTANCE_LAWS.resolve_member(artifact_tag=old)
    current.historical_law_coordinate = (law.coordinate.identifier, law.coordinate.digest)
    assert _installed(current, old) == law


def test_sdk_root_record_constructor_accepts_a_named_owner_contract():
    from cruxible_client.authoring.procedures import procedure_record_constructor
    from cruxible_client.contracts.procedures.names import name_procedure
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

    original = _artifact(_definition())
    named = name_procedure(original)
    for direction in ("input", "output"):
        old = procedure_record_constructor(original, direction)
        current = procedure_record_constructor(named, direction)
        assert type(current) is type(old)
        assert current() == old()


def test_named_query_read_can_be_submitted_through_the_typed_authoring_surface():
    from cruxible_client.contracts.authoring.inputs import (
        QueryDefinitionInput,
        lower_authoring_input,
    )
    from cruxible_client.contracts.named_references import name_query
    from cruxible_client.contracts.query.definitions import render_query_definition
    from cruxible_core.authoring.lowering import _render_non_procedure_member
    from tests.test_procedures.test_named_reference_closure import world

    named, _, tree = world()
    input_ = QueryDefinitionInput.model_validate(
        {"kind": "query_definition", "query_definition": named.model_dump(mode="json")}
    )
    payload = lower_authoring_input(input_)
    assert name_query(payload.query_definition) == named
    path, content, _ = _render_non_procedure_member(payload, tree=tree, light=True)
    assert path == "query-definitions/claims-by-status.json"
    assert content == render_query_definition(named)


def test_frozen_provider_runtime_and_trigger_event_checks_stay_exact():
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.procedures.windows import CaptureEventSelector
    from cruxible_client.contracts.providers import _build_runtime
    from cruxible_client.contracts.triggers import (
        CaptureEventInput,
        CaptureLandingSchedule,
        schedule_satisfies_input,
    )
    from tests.core_support._p2b1_support import provider_v2
    from tests.support.provider_builds import as_build

    provider = as_build(provider_v2())
    replacement = provider.model_copy(
        update={
            "pins": tuple(
                pin.model_copy(update={"artifact_digest": "sha256:" + "a" * 64})
                for pin in provider.pins
            )
        }
    )
    assert _build_runtime(provider) != _build_runtime(replacement)
    assert _build_runtime(provider, by_name=True) == _build_runtime(replacement, by_name=True)
    selector = CaptureEventSelector(
        capture_contract_identity=ArtifactIdentity(kind="CaptureContract", name="test.events"),
        capture_contract_digest="sha256:" + "b" * 64,
    )
    schedule = CaptureLandingSchedule(event=selector)
    required = CaptureEventInput(
        event=selector.model_copy(update={"capture_contract_digest": "sha256:" + "c" * 64})
    )
    assert not schedule_satisfies_input(schedule, required, by_name=False)
    assert schedule_satisfies_input(schedule, required, by_name=True)
