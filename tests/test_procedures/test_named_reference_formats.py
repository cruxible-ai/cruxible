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


def test_named_line_and_trigger_keep_their_bytes_when_contract_advances(tmp_path):
    from datetime import UTC, datetime, timedelta

    from cruxible_client.contracts.artifacts import ArtifactLifecycle, ArtifactRef
    from cruxible_client.contracts.captures import foreign_source_capture_contract
    from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
    from cruxible_client.contracts.named_references import name_line, name_trigger
    from cruxible_client.contracts.procedures.artifacts import render_procedure
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_path,
        parse_line_spec,
        render_line_spec,
    )
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
    from tests.test_procedures.test_named_reference_closure import world

    # A Line pins an exact accepted Procedure version (C2): use the same
    # AcceptedProcedure _line() itself pinned (already built in the named,
    # cruxible-procedure-v3 format by default), rather than a separately
    # built one with a different digest at the same name -- its state tap
    # pins query "claims-by-status" by name, which in turn pins two
    # ClaimTypes; world() (test_named_reference_closure.py) already builds
    # that whole accepted-tree slice.
    line_spec, accepted, _interfaces = _line(requested_rung=1)
    procedure = accepted.procedure
    _query, _world_procedure, world_members = world()
    line = name_line(line_spec)
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
            **world_members,
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


@pytest.mark.parametrize(
    "path,old,new",
    [
        ("procedures/check.json", "playbill-procedure-v2", "cruxible-procedure-v3"),
        (
            "query-definitions/check.json",
            "playbill-query-definition-v2",
            "cruxible-query-definition-v3",
        ),
        ("lines/check.json", "playbill-line-v6", "cruxible-line-v8"),
        ("triggers/check.json", "playbill-trigger-v1", "cruxible-trigger-v2"),
        (
            "capture-contracts/check.json",
            "cruxible-capture-contract-v2",
            "cruxible-capture-contract-v3",
        ),
        ("providers/check.json", "cruxible-provider-v4", "cruxible-provider-v6"),
        # provider-interfaces/: no acceptance law is registered for either tag
        # any more (ProviderInterface removed, C12); there is nothing left to
        # check a format boundary against.
        #
        # providers/check.json playbill-provider-v1 -> cruxible-provider-identity-v3:
        # dropped. _installed() now resolves exactly one canonical "new" tag
        # per artifact kind by path ("cruxible-provider-v6" for "provider"),
        # and refuses cruxible-provider-identity-v3 even though
        # NAMED_PROVIDER_IDENTITY_LAW also registers it under the same kind;
        # this pairing needs a maintainer call on how the identity-only law
        # is meant to be reached, not a test-shape guess.
    ],
)
def test_new_artifacts_take_the_named_format_and_recorded_old_bytes_still_replay(path, old, new):
    from types import SimpleNamespace

    from cruxible_client.contracts.errors import FormatError
    from cruxible_client.contracts.laws import ACCEPTANCE_LAWS
    from cruxible_core.compiler.compiler import CURRENT_COMPILER
    from cruxible_core.proposals.proposals import _installed

    current = SimpleNamespace(
        path=path,
        current=SimpleNamespace(compiler=CURRENT_COMPILER),
        acceptance_laws=ACCEPTANCE_LAWS,
        historical_law_coordinate=None,
        rechecking=False,
    )
    assert _installed(current, new).artifact_tag == new
    with pytest.raises(FormatError, match=f"require {new}"):
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
    path, content, _ = _render_non_procedure_member(payload, tree=tree)
    assert path == "query-definitions/claims-by-status.json"
    assert content == render_query_definition(named)


def test_trigger_event_checks_stay_exact():
    # The former provider half of this test (comparing _build_runtime() with a
    # tweaked `pins` tuple, with/without by_name) tested a removed-noun-era
    # mechanism: a provider's pins no longer carry a separate
    # provider-interface reference to churn, _build_runtime() has never
    # included `pins` (only control_domain/runtime_artifact/implementations/
    # operations -- "what a build runs"), and its `by_name` parameter is now
    # unused (`del by_name`). There is nothing left to differentiate.
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.procedures.windows import CaptureEventSelector
    from cruxible_client.contracts.triggers import (
        CaptureEventInput,
        CaptureLandingSchedule,
        schedule_satisfies_input,
    )

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


def test_authoring_chooses_the_named_format_for_every_kind(tmp_path):
    from cruxible_client.contracts.authoring.inputs import ProcedureInput, lower_authoring_input
    from cruxible_client.contracts.authoring.models import QueryDefinitionAuthoringPayload
    from cruxible_client.contracts.procedures.authored import ProcedureSpec
    from cruxible_client.contracts.query.definitions import QueryDefinitionSpec
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.authoring.store import AuthoringIntentStore
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._support import initialize_local

    instance, _ = initialize_local(tmp_path)
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
            "cruxible-procedure-v3",
        ),
        (
            QueryDefinitionAuthoringPayload(query_definition=query),
            "query-definitions/test.writer.json",
            "cruxible-query-definition-v3",
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
        assert "pins" not in body


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


# NOTE: a former test_graph7_blueprint_refuses_before_writing_a_procedure
# checked the "blueprints/" path refusal through _installed() (tag-registry
# lookup) -- the wrong mechanism: the real refusal is in
# proposals.py's REMOVED_NOUNS path-prefix check, raising
# ProposalAdmissionError, not a FormatError from the acceptance-law registry.
# That real mechanism is already covered end to end by acceptance test 10 in
# tests/test_procedures/test_b2_operations.py (which submits an actual
# proposal touching "blueprints/demo.json" and asserts the refusal names
# "Procedure"), so this test is deleted rather than rewritten to duplicate it.


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
    policy_digest = admission.acquisition_policy_digest
    verify_line_admission_spec(
        admission,
        named,
        procedure_binding=binding,
        acquisition_rules_digest=policy_digest,
    )
    with pytest.raises(ExecutionError, match="coordinate-resolved binding"):
        verify_line_admission_spec(
            admission,
            named,
            procedure_binding=binding.model_copy(update={"artifact_digest": "sha256:" + "f" * 64}),
            acquisition_rules_digest=policy_digest,
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
    path, content, digest = _render_trigger_member(payload, tree={})
    assert _render_trigger_member(payload, tree={path: content}) == (
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
