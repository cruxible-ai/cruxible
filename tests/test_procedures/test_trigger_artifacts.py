"""Every trigger has one governed home: a Trigger aimed at a Line or an internal action."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelectorV1,
    CaptureEventWindowV1,
    FixedWindowV1,
)
from cruxible_client.contracts.triggers import (
    AcceptedTriggerV1,
    CadenceScheduleV1,
    CaptureLandingScheduleV1,
    TriggerFormatError,
    TriggerV1,
    WindowCloseScheduleV1,
    evaluate_trigger_law,
    parse_trigger,
    render_trigger,
    trigger_digest,
    trigger_path,
)
from cruxible_core.compiler.compiler import (
    AUTHORITY_VERBS_COMPILER,
    GOVERNED_TRIGGERS_COMPILER,
    artifact_kinds_for_compiler,
    projection_registry_for_compiler,
)
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from tests.core_support._support import initialize_local
from tests.support.lines import (
    action_trigger,
    graph_v4,
    line_trigger,
    successor,
    trigger_members,
)
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
from tests.test_server.test_playbill_line_run_refusals import (
    _acquisition_policy,
    _line_mandate,
    _served_line,
)

SELECTOR = CaptureEventSelectorV1(
    capture_contract_identity=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
    capture_contract_digest=capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged,
)


def _accepted(trigger: TriggerV1) -> AcceptedTriggerV1:
    return AcceptedTriggerV1(
        path=trigger_path(trigger.identity.name),
        trigger=trigger,
        artifact_digest=trigger_digest(trigger).tagged,
    )


def _law(trigger: TriggerV1, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("predecessor", None)
    return evaluate_trigger_law(trigger, path=trigger_path(trigger.identity.name), **kwargs)


def _code(result) -> str:  # type: ignore[no-untyped-def]
    assert result.verdict == "refused", result
    return result.diagnostics[0].code


def test_a_trigger_round_trips_and_pins_exactly_the_contract_its_schedule_names() -> None:
    trigger = line_trigger(
        "on-landing", line="triage", schedule=CaptureLandingScheduleV1(event=SELECTOR)
    )
    content = render_trigger(trigger)
    assert parse_trigger(content, path=trigger_path("on-landing")) == trigger
    assert [pin.role for pin in trigger.pins] == ["trigger-capture-contract"]
    assert trigger.pins[0].artifact_digest == SELECTOR.capture_contract_digest
    with pytest.raises(TriggerFormatError, match="identity/path"):
        parse_trigger(content, path=trigger_path("elsewhere"))
    with pytest.raises(ValidationError, match="exactly the CaptureContract"):
        TriggerV1.model_validate({**json.loads(content), "pins": []})
    ticking = line_trigger("hourly", line="triage", schedule=CadenceScheduleV1(interval_seconds=60))
    assert ticking.pins == ()
    # The Line is named by identity, never by a version.
    assert json.loads(render_trigger(ticking))["target"] == {
        "kind": "line",
        "line": {"role": "line", "target": {"kind": "Line", "name": "triage"}},
    }


def test_the_trigger_law_judges_targets_actions_and_accepted_events() -> None:
    sweep = action_trigger("sweep", action="evidence.sweep", interval_seconds=86400)
    assert _law(sweep).verdict == "accepted"
    windowed = sweep.model_copy(
        update={
            "schedule": WindowCloseScheduleV1(
                window=FixedWindowV1(starts_at="2026-09-30T00:00:00Z", duration_seconds=60)
            )
        }
    )
    assert _code(_law(windowed)) == "playbill.trigger.action_schedule_unsupported"

    landing = line_trigger(
        "on-landing", line="triage", schedule=CaptureLandingScheduleV1(event=SELECTOR)
    )
    assert (
        _code(_law(landing, target_line_live=False)) == "playbill.trigger.target_line_unavailable"
    )
    assert _law(landing, target_line_live=True).verdict == "accepted"
    # A Line that binds its triggering Capture accepts exactly its declared event.
    assert (
        _law(
            landing, target_line_live=True, target_line_binds_event=True, accepted_event=SELECTOR
        ).verdict
        == "accepted"
    )
    other = SELECTOR.model_copy(update={"capture_contract_digest": "sha256:" + "b" * 64})
    assert (
        _code(
            _law(landing, target_line_live=True, target_line_binds_event=True, accepted_event=other)
        )
        == "playbill.trigger.event_not_accepted"
    )
    ticking = line_trigger("hourly", line="triage", schedule=CadenceScheduleV1(interval_seconds=60))
    assert (
        _code(
            _law(
                ticking,
                target_line_live=True,
                target_line_binds_event=True,
                accepted_event=SELECTOR,
            )
        )
        == "playbill.trigger.event_not_accepted"
    )
    event_window = line_trigger(
        "window",
        line="triage",
        schedule=WindowCloseScheduleV1(
            window=CaptureEventWindowV1(event=SELECTOR, duration_seconds=60)
        ),
    )
    assert (
        _law(
            event_window,
            target_line_live=True,
            target_line_binds_event=True,
            accepted_event=SELECTOR,
        ).verdict
        == "accepted"
    )


def test_the_trigger_law_holds_succession_and_never_revives() -> None:
    first = line_trigger("hourly", line="triage", schedule=CadenceScheduleV1(interval_seconds=3600))
    assert (
        _code(
            _law(
                first.model_copy(
                    update={
                        "lifecycle": ArtifactLifecycle(
                            predecessor_digest=trigger_digest(first).tagged
                        )
                    }
                ),
                predecessor=None,
                target_line_live=True,
            )
        )
        == "playbill.trigger.invalid_genesis"
    )
    changed = successor(first, schedule=CadenceScheduleV1(interval_seconds=60))
    assert _law(changed, predecessor=_accepted(first), target_line_live=True).verdict == "accepted"
    assert (
        _code(
            _law(
                changed.model_copy(
                    update={"lifecycle": ArtifactLifecycle(predecessor_digest="sha256:" + "a" * 64)}
                ),
                predecessor=_accepted(first),
                target_line_live=True,
            )
        )
        == "playbill.trigger.predecessor_mismatch"
    )
    retired = successor(first, state="retired")
    # A retiring Trigger sets nothing off: the Line it named may already be gone.
    assert _law(retired, predecessor=_accepted(first), target_line_live=False).verdict == "accepted"
    revived = successor(retired)
    assert (
        _code(_law(revived, predecessor=_accepted(retired), target_line_live=True))
        == "playbill.trigger.revival_refused"
    )


def test_only_compiler_revision_32_admits_triggers_and_v6_lines() -> None:
    trigger = action_trigger("sweep", action="evidence.sweep", interval_seconds=86400)
    tree = trigger_members(trigger)
    parsed = parse_projection_tree(
        tree,
        registry=projection_registry_for_compiler(GOVERNED_TRIGGERS_COMPILER),
        artifact_kinds=artifact_kinds_for_compiler(GOVERNED_TRIGGERS_COMPILER),
    )
    (envelope,) = parsed.envelopes
    assert (envelope.identity, envelope.kind) == ("Trigger:sweep", "trigger")
    with pytest.raises(ProjectionFormatError, match="no registered format"):
        parse_projection_tree(
            tree,
            registry=projection_registry_for_compiler(AUTHORITY_VERBS_COMPILER),
            artifact_kinds=artifact_kinds_for_compiler(AUTHORITY_VERBS_COMPILER),
        )


def _line_world(tmp_path):  # type: ignore[no-untyped-def]
    """An accepted Line v6 with no Trigger aimed at it yet."""

    instance, owner = initialize_local(tmp_path)
    accepted = graph_v4(_slotless_procedure("triggered-method"))
    policy = _acquisition_policy("triggered-policy")
    line = _served_line("triggered-line", accepted=accepted, policy=policy)
    mandate = _line_mandate(accepted)
    from cruxible_client.contracts.acquisition_policies import (
        acquisition_policy_path,
        render_acquisition_policy,
    )

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(
        {
            capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
                render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
            ),
            accepted.path: render_procedure(accepted.procedure),
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            line_spec_path(line.identity.name): render_line_spec(line),
        }
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-09-30T10:00:00.000000Z", proposal_name="line"
    )
    return instance, owner, line


def _submit(instance, members, name):  # type: ignore[no-untyped-def]
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree.update(members)
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-30T10:05:00.000000Z",
    )


def _refused(result) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    assert result.candidate is None, result
    return tuple(item.code for item in result.evaluation.diagnostics)


def test_proposals_hold_triggers_to_the_line_they_aim_at_and_lines_to_their_triggers(tmp_path):
    instance, owner, line = _line_world(tmp_path)
    stray = line_trigger(
        "stray", line="absent-line", schedule=CadenceScheduleV1(interval_seconds=60)
    )
    assert _refused(_submit(instance, trigger_members(stray), "stray")) == (
        "playbill.trigger.target_line_unavailable",
    )
    windowed_action = TriggerV1(
        identity=ArtifactIdentity(kind="Trigger", name="windowed-sweep"),
        schedule=CaptureLandingScheduleV1(event=SELECTOR),
        target=action_trigger("x", action="evidence.sweep", interval_seconds=60).target,
        pins=line_trigger("x", line="x", schedule=CaptureLandingScheduleV1(event=SELECTOR)).pins,
    )
    assert _refused(_submit(instance, trigger_members(windowed_action), "windowed")) == (
        "playbill.trigger.action_schedule_unsupported",
    )

    hourly = line_trigger(
        "hourly", line=line.identity.name, schedule=CadenceScheduleV1(interval_seconds=3600)
    )
    landing = line_trigger(
        "on-landing", line=line.identity.name, schedule=CaptureLandingScheduleV1(event=SELECTOR)
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(trigger_members(hourly, landing))
    # A Line can have several Triggers.
    _accept_tree(
        instance, owner, tree, timestamp="2026-09-30T10:01:00.000000Z", proposal_name="two"
    )

    retired_line = line.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=line_spec_digest(line).tagged
            )
        }
    )
    refused = _submit(
        instance, {line_spec_path(line.identity.name): render_line_spec(retired_line)}, "retire"
    )
    assert _refused(refused) == ("playbill.line.triggers_not_settled",)
    message = refused.evaluation.diagnostics[0].message
    assert "Trigger:hourly" in message and "Trigger:on-landing" in message
    # Retiring one of the two is still not enough; retiring both in the set is.
    half = _submit(
        instance,
        {
            line_spec_path(line.identity.name): render_line_spec(retired_line),
            **trigger_members(successor(hourly, state="retired")),
        },
        "retire-half",
    )
    assert _refused(half) == ("playbill.line.triggers_not_settled",)
    whole = _submit(
        instance,
        {
            line_spec_path(line.identity.name): render_line_spec(retired_line),
            **trigger_members(
                successor(hourly, state="retired"), successor(landing, state="retired")
            ),
        },
        "retire-whole",
    )
    assert whole.candidate is not None, whole.evaluation.diagnostics
    # A Trigger cannot be removed, only retired.
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree.pop(trigger_path("hourly"))
    removed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/remove", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-30T10:06:00.000000Z",
    )
    assert _refused(removed) == ("playbill.trigger.removal_unsupported",)


def test_new_instances_start_with_the_default_internal_triggers(tmp_path):
    from cruxible_core.triggers.journal import internal_trigger_cadences

    instance, _owner = initialize_local(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    defaults = {
        path: parse_trigger(tree[path], path=path) for path in tree if path.startswith("triggers/")
    }
    assert {
        path: (item.action, item.schedule.interval_seconds) for path, item in defaults.items()
    } == {
        "triggers/evidence-sweep.json": ("evidence.sweep", 86400),
        "triggers/prediction-anchor-retry.json": ("prediction.anchor_retry", 3600),
    }
    assert [
        (item.action, item.interval.total_seconds()) for item in internal_trigger_cadences(instance)
    ] == [
        ("evidence.sweep", 86400),
        ("prediction.anchor_retry", 3600),
    ]


def test_triggers_are_authored_lowered_and_read_like_other_definitions(tmp_path):
    from datetime import UTC, datetime

    from cruxible_client.authoring.examples import (
        AUTHORING_EXAMPLE_FACTORIES,
        AUTHORING_EXAMPLE_NAMES,
    )
    from cruxible_client.contracts.authoring.inputs import TriggerInput, lower_authoring_input
    from cruxible_client.contracts.authoring.models import TriggerAuthoringPayloadV1
    from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
    from cruxible_core.authoring.lowering import AuthoringLoweringError, _render_trigger_member
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext

    instance, owner, line = _line_world(tmp_path)
    assert "trigger" in AUTHORING_EXAMPLE_NAMES
    example = AUTHORING_EXAMPLE_FACTORIES["trigger"]()
    assert isinstance(example, TriggerInput) and example.line_name == "replace-me"
    payload = lower_authoring_input(
        TriggerInput(
            kind="trigger",
            name="hourly",
            schedule=CadenceScheduleV1(interval_seconds=3600),
            line_name=line.identity.name,
        )
    )
    assert isinstance(payload, TriggerAuthoringPayloadV1)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path, content, digest = _render_trigger_member(payload, tree=tree)
    authored = parse_trigger(content, path=path)
    assert path == trigger_path("hourly") and digest == trigger_digest(authored).tagged
    assert authored.line == line.identity and authored.lifecycle.predecessor_digest is None
    with pytest.raises(AuthoringLoweringError) as missing:
        _render_trigger_member(payload.model_copy(update={"line_name": "absent"}), tree=tree)
    assert missing.value.code == "playbill.authoring.trigger_line_missing"

    tree[path] = content
    _accept_tree(instance, owner, tree, timestamp="2026-09-30T10:02:00.000000Z", proposal_name="a")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    # Unchanged, it lowers to the accepted bytes; changed, to a successor of them.
    assert _render_trigger_member(payload, tree=tree)[1] == content
    _path, retired, _digest = _render_trigger_member(
        payload.model_copy(update={"retire": True}), tree=tree
    )
    successor_trigger = parse_trigger(retired, path=path)
    assert successor_trigger.lifecycle.state == "retired"
    assert successor_trigger.lifecycle.predecessor_digest == digest

    card = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(
            ref="Trigger:hourly", evaluation_time=datetime(2026, 9, 30, tzinfo=UTC)
        ),
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    )
    assert card.kind == "trigger"
    assert (card.card.trigger, card.card.target, card.card.lifecycle) == (
        "Trigger:hourly",
        line.identity.qualified,
        "live",
    )
    assert card.card.schedule == {"interval_seconds": 3600, "kind": "cadence"}
