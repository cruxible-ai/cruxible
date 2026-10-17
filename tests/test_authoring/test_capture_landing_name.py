"""Contract names are authoring conveniences; accepted Triggers stay exact."""

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.models import (
    CaptureContractAuthoringPayload,
    CaptureLandingByNameSchedule,
    ChangeSetAuthoringPayload,
    TriggerAuthoringPayload,
)
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT as CONTRACT,
)
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.procedures.windows import CaptureEventSelector
from cruxible_client.contracts.triggers import (
    CaptureLandingSchedule,
    parse_trigger,
    trigger_schedule_pins,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import AuthoringLoweringError, _render_trigger_member
from tests.core_support._identity_support import OWNER
from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
from tests.test_procedures.test_trigger_artifacts import _line_world
from tests.test_server.test_playbill_line_run_refusals import (
    _acquisition_policy,
    _served_line,
)

NAME = CONTRACT.identity.name
PATH = capture_contract_path(NAME)
SELECTOR = CaptureEventSelector(
    capture_contract_identity=CONTRACT.identity,
    capture_contract_digest=capture_contract_digest(CONTRACT).tagged,
)


def tree():
    line = _served_line(
        "triage", accepted=_slotless_procedure("triage"), policy=_acquisition_policy("triage")
    )
    return {
        PATH: render_capture_contract(CONTRACT),
        line_spec_path("triage"): render_line_spec(line),
    }


def payload(**updates):
    return TriggerAuthoringPayload(
        **{
            "name": "on-landing",
            "line_name": "triage",
            "schedule": CaptureLandingByNameSchedule(capture_contract=NAME),
            **updates,
        }
    )


def test_name_form_produces_identical_trigger_bytes_digest_and_pins():
    staged = tree()
    named = _render_trigger_member(payload(), tree=staged)
    exact = _render_trigger_member(
        payload(schedule=CaptureLandingSchedule(event=SELECTOR)), tree=staged
    )
    assert named == exact
    trigger = parse_trigger(named[1], path=named[0])
    assert trigger.artifact_format == "playbill-trigger-v1"
    assert trigger.pins == trigger_schedule_pins(CaptureLandingSchedule(event=SELECTOR))
    assert trigger.schedule.event == SELECTOR
    # The exact stored parser never accepts an authoring-only name.
    import json

    wire = json.loads(named[1])
    wire["schedule"] = {"kind": "capture_landing", "capture_contract": NAME}
    from cruxible_client.contracts.triggers import TriggerFormatError

    with pytest.raises(TriggerFormatError):
        parse_trigger(json.dumps(wire).encode(), path=named[0])


@pytest.mark.parametrize("failure", ["missing", "retired", "wrong_kind"])
def test_unavailable_named_contract_is_a_typed_authoring_refusal(failure):
    staged = tree()
    if failure == "missing":
        staged.pop(PATH)
    elif failure == "retired":
        staged[PATH] = render_capture_contract(
            CONTRACT.model_copy(update={"lifecycle": ArtifactLifecycle(state="retired")})
        )
    else:
        staged[PATH] = staged[line_spec_path("triage")]
    with pytest.raises(AuthoringLoweringError) as caught:
        _render_trigger_member(payload(), tree=staged)
    assert caught.value.code == "cruxible.authoring.trigger_capture_missing"
    assert caught.value.offending_element == "schedule"
    assert PATH in caught.value.message
    assert "Declare or install" in caught.value.repairs[0].description
    assert "exact" in caught.value.repairs[0].description


@pytest.mark.parametrize(
    "extra",
    [{"event": SELECTOR.model_dump()}, {"expected_digest": SELECTOR.capture_contract_digest}],
)
def test_name_schedule_refuses_exact_selector_or_expected_digest(extra):
    with pytest.raises(ValidationError):
        TriggerAuthoringPayload.model_validate(
            {
                "name": "on-feed",
                "line_name": "triage",
                "schedule": {
                    "kind": "capture_landing",
                    "capture_contract": NAME,
                    **extra,
                },
            }
        )


def test_source_pinned_mismatch_names_both_digests():
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec

    staged = tree()
    path = line_spec_path("triage")
    line = parse_line_spec(staged[path], path=path)
    other = SELECTOR.model_copy(update={"capture_contract_digest": "sha256:" + "b" * 64})
    staged[path] = render_line_spec(
        line.model_copy(
            update={
                "trigger_input": "feed",
                "trigger_event": other,
                "pins": tuple(
                    sorted(
                        (*line.pins, *trigger_schedule_pins(CaptureLandingSchedule(event=other))),
                        key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
                    )
                ),
            }
        )
    )
    with pytest.raises(AuthoringLoweringError) as caught:
        _render_trigger_member(payload(), tree=staged)
    assert caught.value.code == "cruxible.trigger.event_not_accepted"
    assert SELECTOR.capture_contract_digest in caught.value.message
    assert other.capture_contract_digest in caught.value.message
    assert caught.value.repairs


def test_named_resolution_reads_one_contract_path_without_enumerating_the_tree():
    class DirectReads(Mapping[str, bytes]):
        def __init__(self):
            self.values = tree()
            self.reads: list[str] = []

        def __getitem__(self, key: str) -> bytes:
            self.reads.append(key)
            return self.values[key]

        def __iter__(self) -> Iterator[str]:
            pytest.fail("named Trigger resolution must not enumerate the instance")

        def __len__(self) -> int:
            pytest.fail("named Trigger resolution must not count the instance")

    staged = DirectReads()
    _render_trigger_member(payload(), tree=staged)
    assert staged.reads.count(PATH) == 1
    assert set(staged.reads) <= {PATH, line_spec_path("triage"), "triggers/on-landing.json"}


def test_payload_prepare_resolves_against_candidate_overlay_and_explains_the_pin(tmp_path: Path):
    instance, _owner, line = _line_world(tmp_path)
    contract = foreign_source_capture_contract("security.advisory").model_copy(
        update={
            "identity": ArtifactIdentity(kind="CaptureContract", name="security.advisory"),
        }
    )
    declaration = CaptureContractAuthoringPayload(capture_contract=contract)
    trigger = payload(
        line_name=line.identity.name,
        schedule=CaptureLandingByNameSchedule(
            capture_contract=contract.identity.name,
        ),
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    authored = coordinator.create(
        actor=OWNER,
        payload=ChangeSetAuthoringPayload(members=(declaration, trigger)),
        canonical_timestamp="2026-10-17T12:00:00.000000Z",
    ).intent
    computed, _ = coordinator._compute_and_bind_preflight(authored.intent_id, actor=OWNER)
    assert computed.result.verdict == "passed", computed.result.frontier
    assert computed.lowered is not None
    member = next(
        m
        for m in computed.lowered.resolved_authoring["members"]
        if m["identity"].startswith("Trigger:")
    )
    assert member["capture_contract"] == {
        "capture_contract_identity": contract.identity.model_dump(mode="json"),
        "capture_contract_digest": capture_contract_digest(contract).tagged,
    }
    stored = parse_trigger(
        computed.lowered.proposed_tree["triggers/on-landing.json"], path="triggers/on-landing.json"
    )
    assert stored.pins[0].artifact_digest == capture_contract_digest(contract).tagged
