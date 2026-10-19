"""All served authoring entry points land according to policy and caller tier."""

from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.authoring.inputs import ClaimInput, lower_authoring_input
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_authoring.test_authoring_program_stamp import _stamp


@pytest.mark.parametrize("entrypoint", ["input", "compiled", "staged"])
@pytest.mark.parametrize(
    ("self_approval", "tier", "accept", "expected"),
    [
        (True, PermissionMode.GRAPH_WRITE, "if_allowed", "landed"),
        (False, PermissionMode.GRAPH_WRITE, "if_allowed", "independent_approval_required"),
        (True, PermissionMode.GOVERNED_WRITE, "if_allowed", "activation_not_permitted"),
        (True, PermissionMode.GRAPH_WRITE, "never", "accept_never"),
    ],
)
def test_authoring_submit_lands_when_allowed(
    tmp_path: Path, monkeypatch, entrypoint, self_approval, tier, accept, expected
) -> None:
    instance, _ = seed_write_surface(tmp_path, self_approval=self_approval)
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    monkeypatch.setattr(playbill_api, "_authoring_coordinator", lambda _: (coordinator, actor))
    value = ClaimInput.model_validate(
        {
            "kind": "claim",
            "subject": f"{KIND}/wi-1",
            "predicate": f"{KIND}.title",
            "object": {"kind": "literal", "value": "land this"},
            "role": "observation",
            "rationale": "Record the observed title.",
            "source": {"kind": "self_source", "body": "land this"},
        }
    )
    before = instance.accepted_coordinate()
    with request_permission_scope(tier):
        if entrypoint == "input":
            submitted = playbill_api.playbill_authoring_submit_input(
                "test", input=value, accept=accept
            )
        elif entrypoint == "compiled":
            submitted = playbill_api.playbill_authoring_compile_and_submit(
                "test",
                payload=lower_authoring_input(value),
                reference_expectations=(),
                program_stamp=_stamp(),
                accept=accept,
            )
        else:
            intent = coordinator.create_input(
                actor=actor, input=value, canonical_timestamp="2026-10-18T12:00:00.000000Z"
            ).intent
            submitted = playbill_api.playbill_authoring_submit(
                "test", intent.intent_id, accept=accept
            )
    if expected == "landed":
        assert isinstance(submitted.outcome, contracts.AuthoringLanded), submitted
        assert submitted.status.state == "accepted"
        assert submitted.outcome.accepted_coordinate == contracts.AcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        )
        assert instance.accepted_coordinate() != before
        # A retry reports the accepted result without another generation.
        with request_permission_scope(tier):
            retry = playbill_api.playbill_authoring_submit("test", submitted.intent["intent_id"])
        assert retry.outcome == submitted.outcome
    else:
        assert isinstance(submitted.outcome, contracts.AuthoringProposed), submitted
        assert submitted.outcome.reason == expected
        assert submitted.outcome.proposal_id == submitted.status.proposal_id
        assert submitted.outcome.proposal_id is not None
        assert submitted.outcome.next is not None
        assert (
            "approve" if expected == "independent_approval_required" else "activate"
        ) in submitted.outcome.next
        assert instance.accepted_coordinate() == before


def test_submit_reports_the_recompiled_candidate_after_a_head_race(tmp_path, monkeypatch):
    from cruxible_client.contracts.claims import claim_path
    from cruxible_core.service.authoring import landing
    from cruxible_core.service.proposals.proposals import _proposal_result
    from tests.test_service.test_write_verbs import _set, _write

    instance, _ = seed_write_surface(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    monkeypatch.setattr(playbill_api, "_authoring_coordinator", lambda _: (coordinator, actor))
    activate = landing.service_activate_playbill_proposal
    moved = []

    def racing(target, **kwargs):
        if not moved:
            moved.append(True)
            assert _write(target, _set(f"{KIND}/wi-2", "title", "contender")).status == "accepted"
        return activate(target, **kwargs)

    monkeypatch.setattr(landing, "service_activate_playbill_proposal", racing)
    value = ClaimInput.model_validate(
        {
            "kind": "claim",
            "subject": f"{KIND}/wi-1",
            "predicate": f"{KIND}.title",
            "object": {"kind": "literal", "value": "survives"},
            "role": "observation",
            "rationale": "Record the title",
            "source": {"kind": "self_source", "body": "survives"},
        }
    )
    with request_permission_scope(PermissionMode.GRAPH_WRITE):
        result = playbill_api.playbill_authoring_submit_input("test", input=value)
    assert isinstance(result.outcome, contracts.AuthoringLanded)
    proposal = _proposal_result(instance, result.outcome.proposal_id)
    assert proposal.admission.readmits is not None and proposal.candidate is not None
    assert result.status.candidate_digest == proposal.candidate.candidate_digest
    assert result.status.proposal_id == result.outcome.proposal_id
    assert result.status == contracts.CandidateStatusRecord.model_validate(
        coordinator.status(result.intent["intent_id"], actor=actor).model_dump(mode="json")
    )
    assert result.intent["candidate_status"] == result.status.model_dump(mode="json")
    member = result.members[0]
    path = claim_path(member["identity"].removeprefix("Claim:"))
    actual = next(item for item in proposal.candidate.members if item.path == path)
    assert member["artifact_digest"] == actual.candidate_artifact_digest
