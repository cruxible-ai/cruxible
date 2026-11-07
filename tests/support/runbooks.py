"""Runbook fixtures (D-R36): Lines that settle, propose and fail, and Runbooks over them.

The world is the D-R39 override world: Procedure ``test.override`` reads the
workspace file its ``path`` parameter names, extracts affected components and
writes ``security.component.affected_by`` Claims, creating vulnerability
Subjects as it goes.

- ``test.override-line`` reads data/doc.json and holds a settle grant: it settles;
- ``test.propose-line`` reads data/other.json under a propose grant: it proposes;
- ``test.fail-line`` reads a file that does not exist: its run refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.authoring.inputs import RunbookInput, TriggerInput
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
)
from tests.core_support._identity_support import accept_tree
from tests.support.mandates import procedure_mandate
from tests.support.operation_world import NOW, actor, author_line

SETTLE = "test.override-line"
PROPOSE = "test.propose-line"
FAIL = "test.fail-line"
PROCEDURE = "test.override"


def grant_propose(instance: Any, line_name: str) -> None:
    """A propose grant for one Line (its Claims and the vulnerabilities it creates)."""

    from tests.test_procedures.test_claims_value_upsert import KIND

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line = parse_line_spec(tree[line_spec_path(line_name)], path=line_spec_path(line_name))
    grant = procedure_mandate(
        line_name + "-grant",
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        grants="propose",
        namespace=("claims", f"subjects/{KIND}"),
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    accept_tree(
        instance,
        {procedure_mandate_path(grant.identity.name): render_procedure_mandate(grant)},
        name=line_name + "-grant",
    )


def propose_line(instance: Any, name: str, *, path: str = "data/other.json") -> None:
    author_line(
        instance,
        name=name,
        procedure=PROCEDURE,
        parameters={"path": path},
        max_authority="propose",
        label=name,
    )
    grant_propose(instance, name)


def runbook_world(tmp_path: Path) -> tuple[Any, Path]:
    from tests.test_procedures.test_line_run_parameter_override import _settle_world

    instance, root = _settle_world(tmp_path)
    propose_line(instance, PROPOSE)
    propose_line(instance, FAIL, path="data/missing.json")
    return instance, root


_INSTANTS = iter(range(1, 10**6))


def author(instance: Any, payload: Any, *, name: str) -> dict[str, bytes]:
    """Lower one authoring input at the head as a NEW intent, and accept what it changes.

    The coordinator resumes an earlier intent of the same content (at its old
    base); a Runbook test re-authors on purpose, so each call lowers a fresh
    draft at the head.
    """

    from cruxible_client.contracts.authoring.inputs import lower_authoring_input
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.proposals.proposals import AuthenticatedActor

    from cruxible_core.indexes.projection import AcceptedCoordinate

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    # A draft, never stored: the coordinator would resume an earlier intent
    # with the same content at its old base.
    intent = coordinator._draft_intent(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=lower_authoring_input(payload),
        canonical_timestamp=f"2026-10-29T11:00:00.{next(_INSTANTS):06d}Z",
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        reference_expectations=None,
        intent_id=coordinator.store.mint_intent_id(),
    )
    changed = dict(lower_authoring(instance, intent=intent, actor_id="owner").changed_members)
    if changed:
        accept_tree(instance, changed, name=name)
    return changed


def author_runbook(
    instance: Any, name: str, steps: list[dict[str, str]], *, description: str | None = None
) -> dict[str, bytes]:
    return author(
        instance,
        RunbookInput.model_validate(
            {"kind": "runbook", "name": name, "steps": steps, "description": description}
        ),
        name="runbook-" + name,
    )


def author_runbook_trigger(
    instance: Any, name: str, *, runbook: str, schedule: dict[str, Any]
) -> dict[str, bytes]:
    return author(
        instance,
        TriggerInput.model_validate(
            {"kind": "trigger", "name": name, "schedule": schedule, "runbook_name": runbook}
        ),
        name="trigger-" + name,
    )


def line_steps(*names: str) -> list[dict[str, str]]:
    return [{"line": name} for name in names]


def readers(instance: Any, root: Path) -> dict[str, Any]:
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

    return {
        "provider_runtime_operator": ProviderRuntimeOperator(root.parent / "state"),
        "workspace_file_reader": WorkspaceFileReader(
            instance_id=instance.descriptor.instance_id,
            operating_profile="local",
            attached_roots=(root,),
            managed_roots=(instance.root,),
        ),
    }


def run_runbook(
    instance: Any, root: Path, name: str, *, minute: int = 0, caller_rung: int = 3
) -> Any:
    from cruxible_core.service.runbooks import service_run_runbook
    from tests.test_procedures.test_completeness_transfer import _Clock

    return service_run_runbook(
        instance,
        name=name,
        actor_context=actor(),
        caller_rung=caller_rung,
        daemon_clock=_Clock(NOW + timedelta(minutes=minute)),
        **readers(instance, root),
    )


class FakeManager:
    """The daemon manager surface a Trigger-fired Runbook run reads."""

    def __init__(self, instance: Any, root: Path) -> None:
        self.instance = instance
        self._readers = readers(instance, root)

    def get(self, instance_id: str) -> Any:
        return self.instance

    def workspace_file_reader(self, instance_id: str) -> Any:
        return self._readers["workspace_file_reader"]

    def provider_runtime_operator(self) -> Any:
        return self._readers["provider_runtime_operator"]

    def procedure_run_config(self) -> Any:
        from cruxible_core.service.procedures.procedure_runs import (
            ProcedureRunOperationalConfigV1,
        )

        # Generous: the fixture clock is the shared fixture instant, not now.
        return ProcedureRunOperationalConfigV1(evaluation_instant_skew_seconds=10**9)


__all__ = [
    "FAIL",
    "PROCEDURE",
    "PROPOSE",
    "SETTLE",
    "FakeManager",
    "author_runbook",
    "author_runbook_trigger",
    "grant_propose",
    "line_steps",
    "propose_line",
    "readers",
    "run_runbook",
    "runbook_world",
]
