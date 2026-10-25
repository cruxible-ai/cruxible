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


def test_each_v3_tag_has_one_named_law_object():
    from cruxible_client.contracts.laws import (
        ACCEPTANCE_LAWS,
        LIGHT_PROCEDURE_LAW,
        NAMED_REFERENCE_LAWS,
        QUERY_DEFINITION_V3_ACCEPTANCE_LAW,
    )

    for alias in (LIGHT_PROCEDURE_LAW, QUERY_DEFINITION_V3_ACCEPTANCE_LAW):
        (entry,) = (law for law in NAMED_REFERENCE_LAWS if law.artifact_tag == alias.artifact_tag)
        assert alias is entry
        assert ACCEPTANCE_LAWS.resolve_member(artifact_tag=alias.artifact_tag) is entry
        assert (
            ACCEPTANCE_LAWS.require_historical(
                identifier=entry.coordinate.identifier,
                digest=entry.coordinate.digest,
            )
            is entry
        )


def test_authored_procedure_lowers_directly_to_the_named_graph():
    from cruxible_client.contracts.authoring.inputs import lower_authoring_input
    from cruxible_client.contracts.procedures.authored import ProcedureSpec, lower_procedure_spec
    from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition

    authored = ProcedureSpec(
        procedure="test.native",
        input={"value": "integer"},
        returns="result",
        steps=({"project": "result", "fields": {"value": "$input.value"}},),
    )
    lowered = lower_procedure_spec(authored)
    definition = NamedProcedureDefinition.model_validate(lowered.definition)
    assert definition.graph_format == 7
    assert definition.contract_in.target.name == "test.native-input"
    payload = lower_authoring_input(lowered)
    assert payload.definition == lowered.definition
    assert "artifact_digest" not in json.dumps(payload.definition)
    assert "role" not in json.dumps(payload.definition)


def test_named_v3_wire_omits_pin_tags_defaults_and_duplicate_node_ids():
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, render_procedure
    from cruxible_client.contracts.procedures.names import name_procedure
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

    original = name_procedure(_artifact(_definition()))
    wire = json.loads(render_procedure(original))
    node = wire["definition"]["nodes"][0]
    assert node["id"] == original.definition.nodes[0].node_id
    assert "node_id" not in node
    assert node["as"] == original.definition.nodes[0].as_
    assert "tag" not in wire["definition"]["contract_in"]
    assert "pins" not in wire
    assert (
        parse_procedure(
            render_procedure(original), path="procedures/" + original.identity.name + ".json"
        )
        == original
    )


@pytest.mark.parametrize("historical", [False, True])
def test_current_and_pre36_authoring_choose_one_format_per_kind(tmp_path, historical):
    from cruxible_client.contracts.authoring.inputs import ProcedureInput, lower_authoring_input
    from cruxible_client.contracts.authoring.models import QueryDefinitionAuthoringPayload
    from cruxible_client.contracts.procedures.authored import ProcedureSpec
    from cruxible_client.contracts.query.definitions import QueryDefinitionSpec
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.authoring.store import AuthoringIntentStore
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._support import initialize_local
    from tests.core_support.legacy_capture_builders import initialize_legacy_local

    initialize = initialize_legacy_local if historical else initialize_local
    instance, _ = initialize(tmp_path)
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
    )
    authored = ProcedureSpec(
        procedure="test.writer",
        input={"value": "integer"},
        returns="result",
        steps=({"project": "result", "fields": {"value": "$input.value"}},),
    )
    query = QueryDefinitionSpec(
        identity=ArtifactIdentity(kind="QueryDefinition", name="test.writer"),
        entry={
            "tag": "playbill-query-artifacts-entry-v2",
            "artifact_kind": "Procedure",
            "selection": "all",
        },
        result_binding="definition",
    )
    cases = (
        (
            lower_authoring_input(ProcedureInput(kind="procedure", spec=authored)),
            "procedures/test.writer.json",
            "playbill-procedure-v2" if historical else "cruxible-procedure-v3",
        ),
        (
            QueryDefinitionAuthoringPayload(query_definition=query),
            "query-definitions/test.writer.json",
            "playbill-query-definition-v2" if historical else "cruxible-query-definition-v3",
        ),
    )
    for payload, path, expected in cases:
        view = coordinator.create(
            actor=AuthenticatedActor(actor_id="owner"),
            payload=payload,
            canonical_timestamp="2026-10-25T12:00:00.000000Z",
        )
        lowered = lower_authoring(instance, intent=view.intent, actor_id="owner")
        body = json.loads(lowered.proposed_tree[path])
        assert body["artifact_format"] == expected
        if not historical:
            assert "pins" not in body


def test_frozen_blueprint_instantiation_chooses_frozen_or_unified_procedure_models():
    from cruxible_client.contracts.procedures.artifacts import NamedProcedureArtifact
    from cruxible_client.contracts.procedures.blueprints import (
        blueprint_digest,
        instantiate_blueprint,
    )
    from tests.core_support._p2b1_support import accepted_interface, accepted_provider
    from tests.test_procedures.test_blueprints import _blueprint

    blueprint = _blueprint()
    interface = accepted_interface()
    arguments = dict(
        blueprint_artifact_digest=blueprint_digest(blueprint).tagged,
        name="test.instance",
        providers={"lookup": accepted_provider()},
        interface_registrations={interface.registration.identity.qualified: interface},
    )
    frozen = instantiate_blueprint(blueprint, **arguments)
    current = instantiate_blueprint(blueprint, artifact_format="cruxible-procedure-v3", **arguments)
    assert frozen.artifact_format == "playbill-procedure-v2"
    assert frozen.definition.graph_format == 6
    assert isinstance(current, NamedProcedureArtifact)
    assert current.definition.graph_format == 7
    assert "pins" not in current.model_dump()


def test_retained_query_bindings_use_the_same_compact_v3_query_wire():
    from cruxible_client.contracts.named_references import name_query
    from cruxible_client.contracts.procedures.source_program import NamedSourceQueryBinding
    from cruxible_client.contracts.procedures.wire_v3 import compact_procedure
    from cruxible_client.contracts.query.definitions import render_query_definition
    from tests.test_query.test_query_definitions import active_work_query

    query = name_query(active_work_query())
    binding = NamedSourceQueryBinding(name=query.identity.name, definition=query)
    wire = compact_procedure(binding)
    assert wire["definition"] == json.loads(render_query_definition(query))
    assert "result_cardinality" not in wire["definition"]
    assert NamedSourceQueryBinding.model_validate(wire) == binding


@pytest.mark.parametrize(
    "literal",
    [
        {"kind": "accepted", "role": "provider", "target": "Provider:literal-data"},
        {"kind": "carried_contract", "name": "literal-data", "role": "contract-out"},
    ],
)
def test_named_authoring_keeps_legacy_reference_spellings_opaque_in_templates(literal):
    from cruxible_client.contracts.authoring.inputs import lower_authoring_input
    from cruxible_client.contracts.procedures.authored import ProcedureSpec, lower_procedure_spec

    authored = ProcedureSpec(
        procedure="test.opaque",
        input={},
        returns="result",
        steps=({"project": "result", "fields": {"data": literal}},),
    )
    lowered = lower_procedure_spec(authored)
    payload = lower_authoring_input(lowered)
    assert payload.definition["nodes"][0]["fields"]["data"] == literal
    assert payload.definition == lowered.definition


def test_named_query_read_validation_preserves_native_owner_and_rejects_old_pins():
    from pydantic import ValidationError

    from cruxible_client.contracts.query.definitions import (
        AcceptedQueryDefinition,
        NamedQueryDefinitionSpec,
        query_definition_digest,
        validate_query_definition,
    )
    from cruxible_core.service.discovery.compact_query import _accepted
    from tests.test_procedures.test_named_reference_closure import authored_query

    query = authored_query("test.native-read")
    assert validate_query_definition(query) is query
    accepted = _accepted(query)
    assert accepted.query == query
    assert accepted.artifact_digest == query_definition_digest(query).tagged
    assert AcceptedQueryDefinition.model_validate(accepted.model_dump(mode="json")) == accepted
    assert set(
        AcceptedQueryDefinition.model_json_schema()["properties"]["query"]["discriminator"][
            "mapping"
        ]
    ) == {
        "playbill-query-definition-v1",
        "playbill-query-definition-v2",
        "cruxible-query-definition-v3",
    }
    assert (
        validate_query_definition(NamedQueryDefinitionSpec.model_validate(query.model_dump()))
        == query
    )
    with pytest.raises(ValidationError, match="pins"):
        validate_query_definition({**query.model_dump(mode="json"), "pins": []})


def test_graph7_blueprint_refuses_before_writing_a_procedure(tmp_path):
    from cruxible_client.contracts.authoring.models import BlueprintAuthoringPayload
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import AuthoringLoweringError, lower_authoring
    from cruxible_core.authoring.store import AuthoringIntentStore
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._support import initialize_local

    instance, _ = initialize_local(tmp_path)
    before = instance.accepted_coordinate()
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
    )
    view = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=BlueprintAuthoringPayload(
            definition={"name": "test.blueprint", "graph_format": 7},
            activation_policy="snapshot",
            owned_contracts=(),
        ),
        canonical_timestamp="2026-10-25T12:00:00.000000Z",
    )
    with pytest.raises(AuthoringLoweringError) as caught:
        lower_authoring(instance, intent=view.intent, actor_id="owner")
    assert caught.value.code == "cruxible.authoring.blueprint_graph_unsupported"
    assert caught.value.repairs[0].description == (
        "author graph 6 or a ProcedureSpec; Blueprints fold into slotted Procedures next"
    )
    assert instance.accepted_coordinate() == before
    assert "procedures/test.blueprint.json" not in instance.tree_at(before.git_oid)


def test_frozen_v1_erasure_authorization_keeps_its_exact_rule_pin():
    from cruxible_client.contracts.captures import CaptureContractV1, capture_retention_policy
    from tests.core_support._pc_c_support import capture_contract

    contract = capture_contract()
    original = render_capture_contract(contract)
    authorized = authorize_erasure(contract)
    assert isinstance(authorized, CaptureContractV1)
    assert capture_retention_policy(authorized).erasure == "authorized_by_rule"
    rule = next(pin for pin in authorized.pins if pin.role == "erasure-rule")
    assert capture_retention_policy(authorized).erasure_rule_digest == rule.artifact_digest
    assert (
        parse_capture_contract(
            render_capture_contract(authorized),
            path=capture_contract_path(authorized.identity.name),
        )
        == authorized
    )
    assert render_capture_contract(contract) == original


def test_named_line_admission_checks_exact_coordinate_bindings(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.errors import ExecutionError
    from cruxible_client.contracts.named_references import name_line
    from cruxible_client.contracts.procedures.line_specs import AcceptedLineSpec, line_spec_digest
    from cruxible_core.procedures.execution import verify_line_admission_spec
    from tests.test_procedures.test_procedure_execution import (
        _accepted_line_for_admission,
        _fixture,
        _line_admission,
        _state_procedure,
    )

    procedure = _state_procedure()
    admission = _line_admission(procedure, _fixture(tmp_path))
    old = _accepted_line_for_admission(admission, procedure)
    line = name_line(old.line)
    named = AcceptedLineSpec(
        path=old.path, line=line, artifact_digest=line_spec_digest(line).tagged
    )
    admission = admission.model_copy(update={"line_spec_digest": named.artifact_digest})
    binding = ArtifactPin(
        role="procedure",
        target=procedure.procedure.identity,
        artifact_digest=procedure.artifact_digest,
    )
    policy = old.line.acquisition_policy
    verify_line_admission_spec(
        admission,
        named,
        procedure_binding=binding,
        acquisition_policy_binding=policy,
    )
    with pytest.raises(ExecutionError, match="coordinate-resolved"):
        verify_line_admission_spec(admission, named)
    with pytest.raises(ExecutionError, match="another Procedure"):
        verify_line_admission_spec(
            admission,
            named,
            procedure_binding=binding.model_copy(update={"artifact_digest": "sha256:" + "f" * 64}),
            acquisition_policy_binding=policy,
        )


def test_named_trigger_noop_keeps_accepted_bytes():
    from cruxible_client.contracts.authoring.models import TriggerAuthoringPayload
    from cruxible_client.contracts.triggers import CadenceSchedule
    from cruxible_core.authoring.lowering import _render_trigger_member

    payload = TriggerAuthoringPayload(
        name="test.noop",
        action="evidence.sweep",
        schedule=CadenceSchedule(interval_seconds=60),
    )
    path, content, digest = _render_trigger_member(payload, tree={}, light=True)
    assert _render_trigger_member(payload, tree={path: content}, light=True) == (
        path,
        content,
        digest,
    )


def test_native_artifacts_round_trip_through_sdk_readiness_and_authoring_carriers():
    from cruxible_client.contracts import ProcedureReadiness
    from cruxible_client.contracts.authoring.models import CaptureContractAuthoringPayload
    from cruxible_client.contracts.procedures.names import name_procedure
    from tests.core_support._light_capture_support import record_contract
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

    artifact = name_procedure(_artifact(_definition()))
    # The SDK receives expanded typed artifacts from readiness, rather than stored compact bytes.
    from tests.test_procedures.test_procedure_execution import _coordinate

    readiness = ProcedureReadiness.model_validate(
        {
            "coordinate": _coordinate().model_dump(mode="json"),
            "procedure_identity": artifact.identity.model_dump(mode="json"),
            "artifact_digest": "sha256:" + "a" * 64,
            "definition_digest": artifact.definition_digest,
            "artifact": artifact.model_dump(mode="json"),
            "runnable": "direct",
            "unsupported_nodes": [],
            "next_operation": {"kind": "run"},
        }
    )
    assert readiness.artifact == artifact
    assert readiness.artifact.definition.graph_format == 7
    payload = CaptureContractAuthoringPayload(capture_contract=record_contract())
    assert (
        CaptureContractAuthoringPayload.model_validate(payload.model_dump(mode="json")) == payload
    )


@pytest.mark.parametrize("named", [False, True])
def test_noop_reference_move_keeps_the_owner_wire_form(named):
    from cruxible_client.contracts.procedures.artifacts import render_procedure
    from cruxible_client.contracts.procedures.names import name_procedure
    from cruxible_core.claims.artifact_references import move_references
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

    procedure = _artifact(_definition())
    if named:
        procedure = name_procedure(procedure)
    payload = json.loads(render_procedure(procedure))
    assert move_references("procedures/triage.json", payload, {}) == payload


def test_native_trigger_missing_line_keeps_the_target_law_refusal(tmp_path):
    from cruxible_client.contracts.triggers import NamedTrigger, render_trigger
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from tests.core_support._support import initialize_local

    instance, _ = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    trigger = NamedTrigger.model_validate(
        {
            "identity": {"kind": "Trigger", "name": "test.missing-line"},
            "target": {"kind": "line", "line": {"target": {"kind": "Line", "name": "absent"}}},
            "schedule": {"kind": "cadence", "interval_seconds": 60},
        }
    )
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/native-missing-line",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree={
            **instance.tree_at(base.git_oid),
            "triggers/test.missing-line.json": render_trigger(trigger),
        },
        timestamp="2026-10-25T12:00:00.000000Z",
    )
    assert proposed.candidate is None
    assert [d.code for d in proposed.evaluation.diagnostics] == [
        "cruxible.trigger.target_line_unavailable"
    ]
