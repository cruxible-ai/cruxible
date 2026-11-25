"""Check a kit's automation template at ``kit build`` (D-R48, fix round F3; R2/R3).

A kit that suggests automation ships ONE template: a change set of Lines,
Triggers, Mandates and Runbooks the adopter tailors and submits as their own
reviewed change. The build checks that it still authors cleanly against
exactly what this build exports -- never the publisher's other local
definitions, which an adopter never receives -- so a template that names a
publisher-local definition the kit does not carry refuses the build instead
of reaching an adopter and failing there.

The check is the authoring preview (``authoring submit --dry-run``): the same
lowering and acceptance law a submit runs, behind every preview guard, and
nothing is stored -- no intent, no body, no proposal. It runs on a throwaway
instance seeded with exactly the exported closure (``KitBundle.contents()``),
the bundled provider packages' own Provider builds and operations (pinned
against exactly that closure's Contracts, never a publisher-local one; D-R48
review fix round 2, R2), and the closure's governed Document bodies (R3), at
its one accepted coordinate. The template's own staged members (new Lines,
Runbooks, Triggers, Mandates it declares) resolve each other through the
ordinary same-change candidate-tree reads, and a name the closure does not
carry and the template does not stage either is undefined, exactly as it
would be for a fresh adopter. Cost is bounded by the closure plus the bundled
providers plus the template's members, never the publisher instance.
"""

from __future__ import annotations

import functools
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import structlog
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
from cruxible_core.providers.package_inspection import InspectedProviderPackage
from cruxible_core.runtime.instance import PlaybillInstance

_log = structlog.get_logger(__name__)
TEMPLATE_INVALID = "cruxible.kit.automation_template_invalid"
RECIPE_INVALID = "cruxible.kit.recipe_invalid"
#: The throwaway instance's control domain for every bundled build it seeds:
#: fixed and arbitrary -- nothing here depends on what a real adopter picks,
#: and the instance never holds two builds of the same name to disagree.
_CONTROL_DOMAIN = "kit-template-check"

_INPUT: TypeAdapter[AuthoringInput] = TypeAdapter(AuthoringInput)
#: A fixed instant for the throwaway instance's one raw acceptance: nothing
#: about this check depends on wall-clock time.
_CLOSURE_TIMESTAMP = "2026-01-01T00:00:00.000000Z"


def _refuse(template: KitAutomationTemplate, first: str) -> RequestRefusedError:
    return RequestRefusedError(
        TEMPLATE_INVALID,
        f"the automation template {template.path} does not author against this "
        f"kit's exported definitions: {first}; fix the template (or the "
        "definitions it names) and build again",
        repair=RepairOperation(operation="cruxible.kit.build"),
    )


@contextmanager
def _closure_instance(
    closure: Mapping[str, bytes],
    *,
    actor_id: str,
    providers: tuple[InspectedProviderPackage, ...] = (),
    document_bodies: Mapping[str, bytes] = {},
) -> Iterator[PlaybillInstance]:
    """A throwaway instance whose one accepted coordinate holds exactly ``closure``,
    the bundled providers' own builds and operations, and the Document bodies
    ``closure`` names.

    Genesis accepts only its fixed seed set, so ``closure`` (never empty: a
    built kit always exports at least one definition) plus each bundled
    package's own Provider build (R2: never a publisher-local build; pinned
    against exactly ``closure``'s and genesis's Contracts, the same material
    ``kit add`` installs before the definitions) is accepted as one further
    raw change under self-approval, over an owner principal named
    ``actor_id``. Every Document body ``document_bodies`` carries lands in the
    body store first (R3), so a Document the closure names with a non-genesis
    body admits exactly as it would for a fresh adopter who received that body
    with the release. Everything here is deleted when the block exits.
    """

    from cruxible_core.governance.keys import generate_client_principal_key
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    scratch = Path(tempfile.mkdtemp(prefix="cruxible-kit-template-check-"))
    try:
        material = generate_client_principal_key(
            scratch / "key",
            principal_id=actor_id,
            kind="ordinary",
            forbidden_roots=(scratch / "instance",),
        )
        instance = PlaybillInstance.initialize(
            scratch / "instance",
            instance_id="kit-template-check",
            client_principals=(material.principal,),
            workspace_roots=(),
            # Exactly the exported closure: the bundled base kit stays out.
            install_base_kit=False,
        )
        base = instance.accepted_coordinate()
        candidate_tree = {**instance.tree_at(base.git_oid), **closure}
        for package in providers:
            built = package.build_artifact(control_domain=_CONTROL_DOMAIN)
            if built is not None:
                path, content = built
                candidate_tree[path] = content
        for digest, content in document_bodies.items():
            stored = instance.body_store().store(content)
            assert stored.digest == digest, "a Document body differs from its own digest"
        proposed = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id=actor_id),
            request=ProposalAdmissionRequest(
                target_ref=f"refs/proposals/{actor_id}/kit-template-check",
                proposed_base_oid=base.git_oid,
            ),
            candidate_tree=candidate_tree,
            timestamp=_CLOSURE_TIMESTAMP,
        )
        if proposed.candidate is None:
            # The exported closure itself does not admit: build_kit's own
            # dependency-closure guarantee was not enough (a provider or
            # system definition the closure assumes is missing or mismatched).
            # This is the build's own failure, not the template's: let the
            # repair point at the build, not the template's definitions.
            first = next(iter(proposed.evaluation.diagnostics), None)
            detail = "its exported definitions did not admit" if first is None else first.message
            raise RequestRefusedError(
                TEMPLATE_INVALID,
                f"this kit's own exported definitions do not author on a fresh instance: "
                f"{detail}; this is the kit build's own closure, not the template",
                repair=RepairOperation(operation="cruxible.kit.build"),
            )
        receipt = service_activate_playbill_proposal(
            instance, proposal_id=proposed.admission.proposal_id, activated_by=actor_id
        )
        if getattr(receipt, "status", None) != "accepted":
            raise RequestRefusedError(
                TEMPLATE_INVALID,
                "this kit's own exported definitions did not activate on a fresh instance",
                repair=RepairOperation(operation="cruxible.kit.build"),
            )
        instance.refresh()
        yield instance
    finally:
        # Best effort (the check's own refusal, not the caller's, must win on
        # any conflict), but never silent: a failed sweep leaves scratch state
        # on disk, which is worth knowing about, not swallowing.
        try:
            shutil.rmtree(scratch)
        except OSError:
            _log.warning("kit_template_check_cleanup_failed", scratch=str(scratch))


def check_automation_template(
    closure: Mapping[str, bytes],
    template: KitAutomationTemplate,
    *,
    actor_id: str,
    providers: tuple[InspectedProviderPackage, ...] = (),
    document_bodies: Mapping[str, bytes] = {},
) -> None:
    """Refuse the build unless the template previews cleanly against ``closure`` alone.

    ``closure`` is exactly what this build exports (``KitBundle.contents()``),
    never the publisher's other local definitions. ``providers`` are the
    bundled packages the build validated (R2): each one's own Provider build
    and operations are seeded, never any other publisher-local build.
    ``document_bodies`` (digest -> bytes) are the closure's own governed
    Document bodies (R3), landed before admission so a non-genesis body
    admits exactly as it would for a fresh adopter.
    """

    check_kit_change_sets(
        closure,
        templates=(template,),
        actor_id=actor_id,
        providers=providers,
        document_bodies=document_bodies,
    )


def _recipe_refusal(name: str, path: str, first: str) -> RequestRefusedError:
    return RequestRefusedError(
        RECIPE_INVALID,
        f"the recipe {name} ({path}), rendered with its defaults and examples, does not "
        f"author against this kit's exported definitions: {first}; fix the recipe (or the "
        "definitions it names) and build again",
        repair=RepairOperation(operation="cruxible.kit.build"),
    )


def check_kit_change_sets(
    closure: Mapping[str, bytes],
    *,
    templates: tuple[KitAutomationTemplate, ...] = (),
    recipes: tuple[tuple[str, str, Mapping[str, object]], ...] = (),
    actor_id: str,
    providers: tuple[InspectedProviderPackage, ...] = (),
    document_bodies: Mapping[str, bytes] = {},
) -> None:
    """Preview the automation template and each rendered recipe against ``closure``.

    ``recipes`` holds (name, path, rendered change set). Every input is parsed
    before the throwaway instance opens, and all of them preview on that one
    instance (each preview stores nothing, so none sees another), so the cost
    is one closure instance for the whole build, not one per recipe.
    """

    from cruxible_client.contracts.candidates import canonical_candidate_timestamp
    from cruxible_client.contracts.temporal import utc_now
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.change_preview import preview_guards

    checks: list[tuple[ChangeSetInput, Callable[[str], RequestRefusedError]]] = []
    for template in templates:
        refuse: Callable[[str], RequestRefusedError] = functools.partial(_refuse, template)
        try:
            parsed = _INPUT.validate_python(json.loads(template.content))
        except json.JSONDecodeError as exc:
            raise refuse(f"not JSON ({exc.msg} at line {exc.lineno})") from exc
        except ValidationError as exc:
            raise refuse(f"cruxible.authoring.input_invalid: {validation_summary(exc)}")
        if not isinstance(parsed, ChangeSetInput):
            raise refuse("a template is one change_set authoring input")
        checks.append((parsed, refuse))
    for name, path, rendered in recipes:
        refuse = functools.partial(_recipe_refusal, name, path)
        try:
            parsed = _INPUT.validate_python(rendered)
        except ValidationError as exc:
            raise refuse(f"cruxible.authoring.input_invalid: {validation_summary(exc)}")
        if not isinstance(parsed, ChangeSetInput):
            raise refuse("a recipe is one change_set authoring input")
        checks.append((parsed, refuse))
    if not checks:
        return
    with (
        _closure_instance(
            closure, actor_id=actor_id, providers=providers, document_bodies=document_bodies
        ) as instance,
        preview_guards(True),
    ):
        coordinator = AuthoringIntentCoordinator.for_instance(instance)
        for parsed, refuse in checks:
            try:
                _intent, computed = coordinator.preview_input(
                    actor=AuthenticatedActor(actor_id=actor_id),
                    input=parsed,
                    canonical_timestamp=canonical_candidate_timestamp(utc_now()),
                )
            except AuthoringInputError as exc:
                raise refuse(f"{exc.code} at {exc.field_path}: {exc.message}") from exc
            result = computed.result
            if result.verdict == "passed":
                continue
            frontier = result.frontier
            if frontier.diagnostics:
                first = frontier.diagnostics[0]
                raise refuse(f"{first.code} at {first.offending_element}: {first.message}")
            if frontier.blocked_checks:
                blocked = frontier.blocked_checks[0]
                raise refuse(f"check {blocked.check} is blocked: {blocked.reason}")
            raise refuse("its preview did not complete")


__all__ = [
    "RECIPE_INVALID",
    "TEMPLATE_INVALID",
    "check_automation_template",
    "check_kit_change_sets",
]
