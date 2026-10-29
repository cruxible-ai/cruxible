"""Real core-service adapter for revision-35 Provider/Kit history regressions."""

from datetime import UTC, datetime
from types import SimpleNamespace

from cruxible_client.contracts.authoring.inputs import LineInput, TriggerInput
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.test_server.test_playbill_kits import _CoreWorld

AT = "2026-10-24T12:00:00.000000Z"


class CoreDraft:
    def __init__(self, world, rationale):
        self.world = world
        self.rationale = rationale
        self.members = []

    def procedure(self, *, definition):
        self.members.append(definition)
        return self

    def line(self, *, name, procedure, **kwargs):
        self.members.append(LineInput(kind="line", name=name, procedure_name=procedure, **kwargs))
        return self

    def trigger(self, *, name, line, schedule):
        self.members.append(
            TriggerInput(kind="trigger", name=name, line_name=line, schedule=schedule)
        )
        return self

    def prepare(self):
        from cruxible_client.contracts.authoring.inputs import lower_authoring_input
        from cruxible_client.contracts.authoring.models import (
            ChangeSetAuthoringPayload,
            authoring_member_identity,
        )

        self.coordinator = AuthoringIntentCoordinator.for_instance(self.world.instance)
        self.actor = AuthenticatedActor(actor_id="owner")
        members = tuple(
            lower_authoring_input(item) if hasattr(item, "kind") else item for item in self.members
        )
        payload = ChangeSetAuthoringPayload(
            members=tuple(sorted(members, key=authoring_member_identity)), rationale=self.rationale
        )
        self.intent = self.coordinator.create(
            actor=self.actor, payload=payload, canonical_timestamp=AT
        ).intent
        result = self.coordinator.preflight(self.intent.intent_id, actor=self.actor)
        self.refused = result.verdict != "passed"
        self.diagnostics = result.frontier
        return self

    def submit(self, *, accept):
        result = self.coordinator.submit(self.intent.intent_id, actor=self.actor)
        return SimpleNamespace(_candidate_status=result.status)


class CoreProviderClient:
    def __init__(self, world):
        self.world = world

    def store_body(self, instance_id, content):
        return self.world.instance.body_store().store(content)

    def install_provider(self, instance_id, request):
        from cruxible_core.service.procedures.provider_installation import service_install_provider

        return service_install_provider(
            self.world.instance,
            operator=self.world.operator,
            request=request,
            actor_id="owner",
            timestamp=AT,
        )

    def move_provider(self, instance_id, request):
        from cruxible_core.service.procedures.provider_builds import service_move_provider

        return service_move_provider(self.world.instance, request, actor_id="owner", timestamp=AT)

    def retire_provider(self, instance_id, request):
        from cruxible_core.service.procedures.provider_builds import service_retire_provider

        return service_retire_provider(self.world.instance, request, actor_id="owner", timestamp=AT)

    def list_provider_packages(self, instance_id):
        from cruxible_core.service.procedures.provider_installation import installed_providers

        return SimpleNamespace(installed=installed_providers(self.world.instance))

    def get(self, instance_id, *, request):
        from cruxible_core.service.discovery.get import service_playbill_get
        from cruxible_core.storage.cas import BodyAccessContext

        return service_playbill_get(
            self.world.instance,
            request=request,
            access=BodyAccessContext(principal_id="owner", can_read_body=True),
        )

    def enable_line(self, instance_id, line):
        from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
        from cruxible_core.service.procedures.line_dispatch import service_enable_line

        return service_enable_line(
            self.world.instance,
            line,
            principal=LineEnablementPrincipal(kind="local_operator", label="operator"),
            actor=self.world.actor(),
            now=datetime.now(UTC),
            daemon_id="core-history",
        )


class CoreProviderWorld(_CoreWorld):
    def __init__(self, root, instance_id, operator):
        root.mkdir(parents=True, exist_ok=True)
        super().__init__(root, instance_id)
        self.operator = operator
        self.http = CoreProviderClient(self)
        self.pb.changes = lambda *, rationale: CoreDraft(self, rationale)

    def actor(self):
        from cruxible_core.governance.actor_context import GovernedActorContext

        return GovernedActorContext(
            actor_type="human_user",
            actor_id="owner",
            org_id=self.instance_id,
            operation_id="core-history",
            timestamp=datetime.now(UTC),
        )

    def run(self, name, n):
        from cruxible_core.service.procedures.procedure_runs import (
            ProcedureRunRequest,
            service_run_playbill_procedure,
        )

        state = service_run_playbill_procedure(
            self.instance,
            name=name,
            request=ProcedureRunRequest(input={"n": n}, evaluation_time=datetime.now(UTC)),
            actor_context=self.actor(),
            provider_runtime_operator=self.operator,
        )
        assert state.status == "succeeded", state
        return state.result

    def accept_members(self, members, *, timestamp):
        from tests.test_indexes.test_resolution_contracts import _accept_tree

        _accept_tree(
            self.instance,
            self.owner,
            {**self.tree(), **members},
            timestamp=timestamp,
            proposal_name="core-provider-members",
        )
