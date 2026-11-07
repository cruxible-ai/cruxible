"""Check a kit's automation template at ``kit build`` (D-R48).

A kit that suggests automation ships ONE template: a change set of Lines,
Triggers, Mandates and Runbooks the adopter tailors and submits as their own
reviewed change. The build checks that it still authors cleanly against the
kit's own definitions, as accepted on the building instance, so a template
that drifted from them refuses the build instead of reaching an adopter.

The check is the authoring preview (``authoring submit --dry-run``): the same
lowering and acceptance law a submit runs, behind every preview guard, and
nothing is stored -- no intent, no body, no proposal. Its cost is that preview's
for the template's members (one lowering per member against the accepted tree,
and the candidate's law), never a walk of the instance.
"""

from __future__ import annotations

import json

from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.authoring.inputs import (
    AuthoringInput,
    AuthoringInputError,
    ChangeSetInput,
)
from cruxible_client.contracts.kits import KitAutomationTemplate
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_core.errors import RequestRefusedError
from cruxible_core.runtime.instance import PlaybillInstance

TEMPLATE_INVALID = "cruxible.kit.automation_template_invalid"

_INPUT: TypeAdapter[AuthoringInput] = TypeAdapter(AuthoringInput)


def _refuse(template: KitAutomationTemplate, first: str) -> RequestRefusedError:
    return RequestRefusedError(
        TEMPLATE_INVALID,
        f"the automation template {template.path} does not author against this "
        f"instance's accepted definitions: {first}; fix the template (or the "
        "definitions it names) and build again",
        repair=RepairOperation(operation="cruxible.kit.build"),
    )


def check_automation_template(
    instance: PlaybillInstance, template: KitAutomationTemplate, *, actor_id: str
) -> None:
    """Refuse the build unless the template previews cleanly as one change set."""

    from cruxible_client.contracts.candidates import canonical_candidate_timestamp
    from cruxible_client.contracts.temporal import utc_now
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.change_preview import preview_guards

    try:
        parsed = _INPUT.validate_python(json.loads(template.content))
    except json.JSONDecodeError as exc:
        raise _refuse(template, f"not JSON ({exc.msg} at line {exc.lineno})") from exc
    except ValidationError as exc:
        raise _refuse(template, f"cruxible.authoring.input_invalid: {validation_summary(exc)}")
    if not isinstance(parsed, ChangeSetInput):
        raise _refuse(template, "a template is one change_set authoring input")
    with preview_guards(True):
        coordinator = AuthoringIntentCoordinator.for_instance(instance)
        try:
            _intent, computed = coordinator.preview_input(
                actor=AuthenticatedActor(actor_id=actor_id),
                input=parsed,
                canonical_timestamp=canonical_candidate_timestamp(utc_now()),
            )
        except AuthoringInputError as exc:
            raise _refuse(template, f"{exc.code} at {exc.field_path}: {exc.message}") from exc
    result = computed.result
    if result.verdict == "passed":
        return
    frontier = result.frontier
    if frontier.diagnostics:
        first = frontier.diagnostics[0]
        raise _refuse(template, f"{first.code} at {first.offending_element}: {first.message}")
    if frontier.blocked_checks:
        blocked = frontier.blocked_checks[0]
        raise _refuse(template, f"check {blocked.check} is blocked: {blocked.reason}")
    raise _refuse(template, "its preview did not complete")


__all__ = ["TEMPLATE_INVALID", "check_automation_template"]
