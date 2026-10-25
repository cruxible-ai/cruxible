"""Prepare and runtime boundaries for authoring-only CaptureContract names."""

from datetime import timedelta

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.authoring.inputs import TriggerInput
from cruxible_client.contracts.authoring.models import (
    CaptureLandingByNameSchedule,
    TriggerAuthoringPayload,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT as CONTRACT,
)
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract_v1,
    render_capture_contract,
)
from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
from cruxible_client.contracts.procedures.windows import NamedCaptureEventSelector
from cruxible_client.contracts.triggers import CaptureLandingSchedule, parse_trigger, trigger_path
from cruxible_core.authoring import lowering
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.service.procedures.line_dispatch import service_evaluate_line
from tests.core_support._identity_support import OWNER
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME


@pytest.mark.parametrize("window_close", [False, True])
@pytest.mark.parametrize("sources", [(), ("sarif.log",)])
def test_decision_input_prepare_calls_the_same_resolver_and_explains_the_pin(
    tmp_path,
    monkeypatch,
    window_close,
    sources,
):
    instance, line, _procedure = line_world(
        tmp_path, CaptureLandingSchedule(event=SELECTOR), triggers=()
    )
    calls = []
    resolve = lowering._resolve_trigger_authoring_schedule

    def tracked(schedule, **kwargs):
        calls.append(schedule)
        return resolve(schedule, **kwargs)

    monkeypatch.setattr(lowering, "_resolve_trigger_authoring_schedule", tracked)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    schedule = (
        {
            "kind": "window_close",
            "window": {
                "kind": "capture_event",
                "capture_contract": CONTRACT.identity.name,
                "duration_seconds": 60,
            },
        }
        if window_close
        else {"kind": "capture_landing", "capture_contract": CONTRACT.identity.name}
    )
    if sources:
        (schedule["window"] if window_close else schedule)["logical_sources"] = list(sources)
    result = coordinator.compile_input(
        actor=OWNER,
        input=TriggerInput.model_validate(
            {
                "kind": "trigger",
                "name": "on-feed",
                "line_name": line.identity.name,
                "schedule": schedule,
            }
        ),
        canonical_timestamp="2026-10-17T12:00:00.000000Z",
    )
    assert result.verdict == "passed", result.frontier
    assert calls and all(item.model_dump(mode="json") == schedule for item in calls)
    assert result.resolved_trigger_contracts[0].trigger_name == "on-feed"
    assert result.resolved_trigger_contracts[0].event == NamedCaptureEventSelector(
        capture_contract_identity=SELECTOR.capture_contract_identity, logical_sources=sources
    )
    from cruxible_client.contracts import AuthoringPreflightResult

    served = AuthoringPreflightResult.model_validate(result.model_dump(mode="json"))
    assert served.resolved_trigger_contracts == result.resolved_trigger_contracts


def test_contract_succession_requires_closure_and_cannot_retarget_a_stored_trigger(tmp_path):
    instance, line, procedure, owner = line_world(
        tmp_path,
        CaptureLandingSchedule(event=SELECTOR),
        triggers=(),
        with_owner=True,
        historical=True,
    )
    contract = foreign_source_capture_contract_v1("security.advisory")
    old_digest = capture_contract_digest(contract).tagged
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
    path, content, _ = lowering._render_trigger_member(
        TriggerAuthoringPayload(
            name="on-feed",
            line_name=line.identity.name,
            schedule=CaptureLandingByNameSchedule(capture_contract=contract.identity.name),
        ),
        tree=tree,
    )
    tree[path] = content
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T15:01:00.000000Z", proposal_name="named"
    )
    successor = contract.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=old_digest),
            "selection_budget": contract.selection_budget.model_copy(
                update={"max_bytes": contract.selection_budget.max_bytes * 2}
            ),
        }
    )
    new_digest = capture_contract_digest(successor).tagged
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[capture_contract_path(contract.identity.name)] = render_capture_contract(successor)
    # Existing closure treats this pin as a live dependency, not historical
    # provenance. Preserve that law: an unchanged live Trigger blocks succession.
    import json

    from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest

    proposed = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/contract",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-24T15:02:00.000000Z",
    )
    assert proposed.candidate is None
    diagnostic = next(
        d
        for d in proposed.evaluation.diagnostics
        if d.code == "cruxible.change_set.incomplete_closure"
    )
    missing = json.loads(diagnostic.message)["missing"]
    assert any(
        item["path"] == path and item["dependency_edge_role"] == "trigger-capture-contract"
        for item in missing
    )
    accepted = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert accepted[trigger_path("on-feed")] == content
    assert parse_trigger(content, path=path).schedule.event.capture_contract_digest == old_digest
    capture(instance, procedure, digest=new_digest, partition="run:new", at=READ_TIME)
    old = capture(
        instance,
        procedure,
        digest=old_digest,
        partition="run:old",
        at=READ_TIME + timedelta(seconds=1),
    )
    result = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(until=READ_TIME + timedelta(seconds=2), dry_run=True),
        actor=None,
        now=READ_TIME + timedelta(seconds=2),
    )
    assert result.status == "met", result
    assert [item.binding.event.record_digest for item in result.occurrences] == [old.record_digest]


def test_retire_prepare_preserves_predecessor_without_contract_lookup(tmp_path, monkeypatch):
    from tests.test_procedures.test_line_triggers import TRIGGER

    instance, line, _procedure = line_world(tmp_path, CaptureLandingSchedule(event=SELECTOR))
    monkeypatch.setattr(
        lowering,
        "_resolve_trigger_authoring_schedule",
        lambda *args, **kwargs: pytest.fail("retirement must not resolve a contract"),
    )
    result = AuthoringIntentCoordinator.for_instance(instance).compile_input(
        actor=OWNER,
        input=TriggerInput(
            kind="trigger",
            name=TRIGGER,
            line_name=line.identity.name,
            schedule=CaptureLandingByNameSchedule(capture_contract="absent.contract"),
            retire=True,
        ),
        canonical_timestamp="2026-10-17T12:00:00.000000Z",
    )
    assert result.verdict == "passed", result.frontier
    assert result.resolved_trigger_contracts[0].event == NamedCaptureEventSelector(
        capture_contract_identity=SELECTOR.capture_contract_identity
    )
