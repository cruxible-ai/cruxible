"""Lower a Runbook decision, and a Trigger aimed at a Runbook (D-R36).

``lowering.py`` dispatches here; everything Runbook-specific is in this module.
A Runbook names its Lines by identity (never pinned), so authoring reads one
candidate path per step: the Line must be present and live at the base, and
must not bind its triggering Capture (a Runbook step runs without an event).
Every action must be registered. The acceptance law judges the same again in
the final candidate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
)
from cruxible_client.contracts.authoring.models import (
    RunbookAuthoringPayload,
    TriggerAuthoringPayload,
)
from cruxible_client.contracts.procedures.line_specs import (
    LineSpecFormatError,
    line_spec_path,
    parse_line_spec,
)
from cruxible_client.contracts.runbooks import (
    Runbook,
    RunbookFormatError,
    RunbookStep,
    parse_runbook,
    render_runbook,
    runbook_digest,
    runbook_path,
)
from cruxible_client.contracts.triggers import (
    INTERNAL_ACTIONS,
    ActionTarget,
    CadenceSchedule,
    CronSchedule,
    GenerationAcceptedSchedule,
    LineTarget,
    RunbookTarget,
    Trigger,
    parse_trigger,
    render_trigger,
    trigger_digest,
    trigger_path,
)
from cruxible_core.authoring.lowering import (
    LoweredAuthoring,
    _encoded_members,
    _refuse,
    _same_revision_content,
)
from cruxible_core.derived.derived_state import fork_tree

if TYPE_CHECKING:
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.runtime.instance import PlaybillInstance


def _step(
    index: int, line: str | None, action: str | None, tree: Mapping[str, bytes]
) -> RunbookStep:
    if action is not None:
        if action not in INTERNAL_ACTIONS:
            _refuse(
                "cruxible.authoring.runbook_action_unknown",
                f"steps[{index}].action",
                f"Internal action {action!r} is not registered; a Runbook step may fire one "
                "of: " + ", ".join(sorted(INTERNAL_ACTIONS)) + ".",
                repair_kind="replace_input",
                repair_description="Name a registered internal action.",
            )
        return ActionTarget(action=action)
    assert line is not None
    path = line_spec_path(line)
    content = tree.get(path)
    accepted = None
    if content is not None:
        try:
            accepted = parse_line_spec(content, path=path)
        except LineSpecFormatError:
            accepted = None
    if accepted is None or accepted.lifecycle.state != "live":
        _refuse(
            "cruxible.authoring.runbook_line_missing",
            f"steps[{index}].line",
            f"Line {line!r} is not a live accepted Line at this base.",
            repair_kind="replace_input",
            repair_description="Accept the Line first, or name an accepted live Line.",
        )
    if accepted.trigger_input is not None:
        _refuse(
            "cruxible.authoring.runbook_line_takes_event",
            f"steps[{index}].line",
            f"Line {line!r} binds its triggering Capture to a Source input; a Runbook step "
            "runs without an event.",
            repair_kind="replace_input",
            repair_description=(
                "Leave this Line to its own capture_landing Trigger, or name a Line that "
                "runs without an event."
            ),
        )
    return LineTarget(line=ArtifactNameRef(target=ArtifactIdentity(kind="Line", name=line)))


def render_runbook_member(
    payload: RunbookAuthoringPayload, *, tree: Mapping[str, bytes]
) -> tuple[str, bytes, str]:
    """The Runbook artifact this decision writes: O(steps) candidate lookups."""

    path = runbook_path(payload.name)
    previous_content = tree.get(path)
    previous: Runbook | None = None
    if previous_content is not None:
        try:
            previous = parse_runbook(previous_content, path=path)
        except RunbookFormatError:
            previous = None
    if payload.retire:
        if previous is None:
            _refuse(
                "cruxible.authoring.runbook_missing",
                "name",
                "Retiring a Runbook requires its accepted predecessor.",
                repair_kind="replace_input",
                repair_description="Name an accepted Runbook to retire.",
            )
        if previous.lifecycle.state == "retired":
            assert previous_content is not None
            return path, previous_content, runbook_digest(previous).tagged
        retired = previous.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired", predecessor_digest=runbook_digest(previous).tagged
                )
            }
        )
        return path, render_runbook(retired), runbook_digest(retired).tagged
    if previous is not None and previous.lifecycle.state == "retired":
        _refuse(
            "cruxible.runbook.revival_refused",
            "name",
            "A retired Runbook cannot be revived.",
            repair_kind="replace_input",
            repair_description="Author the Runbook under a new name.",
        )
    runbook = Runbook(
        identity=ArtifactIdentity(kind="Runbook", name=payload.name),
        description=payload.description,
        steps=tuple(
            _step(index, step.line, step.action, tree) for index, step in enumerate(payload.steps)
        ),
        lifecycle=ArtifactLifecycle(
            predecessor_digest=None if previous is None else runbook_digest(previous).tagged
        ),
    )
    if previous is not None and previous_content is not None:
        if _same_revision_content(runbook, previous):
            return path, previous_content, runbook_digest(previous).tagged
    return path, render_runbook(runbook), runbook_digest(runbook).tagged


def lower_runbook(
    instance: PlaybillInstance,
    *,
    payload: RunbookAuthoringPayload,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
) -> LoweredAuthoring:
    path, content, digest = render_runbook_member(payload, tree=base_tree)
    candidate_tree = fork_tree(base_tree)
    candidate_tree[path] = content
    changed = () if base_tree.get(path) == content else ((path, content),)
    return LoweredAuthoring(
        proposed_tree=candidate_tree,
        resolved_authoring={
            "artifact_digest": digest,
            "changed_members": _encoded_members(changed),
            "identity": f"Runbook:{payload.name}",
        },
        changed_members=changed,
        idempotent=not changed and base.git_oid == instance.accepted_coordinate().git_oid,
    )


def render_runbook_trigger(
    payload: TriggerAuthoringPayload, *, tree: Mapping[str, bytes]
) -> tuple[str, bytes, str]:
    """A live Trigger aimed at a Runbook: the Runbook must be live in the staged tree.

    A Runbook takes a time schedule or generation acceptance, as an internal
    action does: its steps run as manual occurrences, never on an event.
    Retirement is lowered by the ordinary Trigger path before this is reached.
    """

    assert payload.runbook_name is not None
    name = payload.runbook_name
    try:
        target_path = runbook_path(name)
    except RunbookFormatError:
        target_path = ""
    content = tree.get(target_path)
    live = False
    if content is not None:
        try:
            live = parse_runbook(content, path=target_path).lifecycle.state == "live"
        except RunbookFormatError:
            live = False
    if not live:
        _refuse(
            "cruxible.authoring.trigger_runbook_missing",
            "runbook_name",
            f"Runbook {name!r} is not a live accepted Runbook at this base.",
            repair_kind="replace_input",
            repair_description="Accept the Runbook first, or name an accepted live Runbook.",
        )
    schedule = payload.schedule
    if not isinstance(schedule, CadenceSchedule | CronSchedule | GenerationAcceptedSchedule):
        _refuse(
            "cruxible.trigger.schedule_unsupported_for_runbook",
            "schedule",
            f"A Runbook takes cadence, cron or generation_accepted; a {schedule.kind} schedule "
            "has no event to give its steps, which run as manual occurrences.",
            repair_kind="replace_schedule",
            repair_description="Use a cadence, cron or generation_accepted schedule.",
        )
    path = trigger_path(payload.name)
    previous_content = tree.get(path)
    previous = None if previous_content is None else parse_trigger(previous_content, path=path)
    trigger = Trigger(
        identity=ArtifactIdentity(kind="Trigger", name=payload.name),
        schedule=schedule,
        target=RunbookTarget(
            runbook=ArtifactNameRef(target=ArtifactIdentity(kind="Runbook", name=name))
        ),
        lifecycle=ArtifactLifecycle(
            state="live",
            predecessor_digest=None if previous is None else trigger_digest(previous).tagged,
        ),
    )
    if previous_content is not None and previous is not None:
        if _same_revision_content(trigger, previous):
            return path, previous_content, trigger_digest(previous).tagged
    return path, render_trigger(trigger), trigger_digest(trigger).tagged


__all__ = ["lower_runbook", "render_runbook_member", "render_runbook_trigger"]
