"""Typed source authoring uses real installed provider wheels and normal run doors."""

import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cruxible_client import Disposition, Playbill
from cruxible_client.authoring.inputs import CarriedContractInput
from cruxible_client.authoring.source import (
    claim_candidate,
    emit_capture,
    halt,
    invoke,
    procedure,
    propose_change_set,
    source,
)
from cruxible_client.contracts.authoring.inputs import ProcedureMandateInputV1
from cruxible_client.contracts.captures import (
    CanonicalDurationV1,
    capture_component_pin,
    capture_contract_digest,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import ProcedureBudgetV3, ProcedureHardCapsV3
from cruxible_client.provider_installation import install_provider_package
from cruxible_client.transport.http import CruxibleClient
from tests.core_support._pc_c_support import capture_contract
from tests.test_procedures.test_procedure_proposal_delivery import _claim_type, _subject
from tests.test_procedures.test_procedure_source_runs import _policy
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate
from tests.test_server.test_provider_installation import installer_http  # noqa: F401


def test_installed_fetch_parent_proposal_and_derivation_policy_boundary(
    installer_http,  # noqa: F811
    tmp_path,
):
    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = Path(os.environ["CRUXIBLE_TEST_PROVIDER_REPOSITORY"])
    wheels = Path(os.environ["CRUXIBLE_TEST_PROVIDER_WHEELS"])
    installed = install_provider_package(
        client,
        instance_id,
        wheel=next(wheels.glob("cruxible_provider_web-*.whl")),
        lock=repository / "packages/cruxible-provider-web/uv.lock",
        dependency_wheels=(next(wheels.glob("cruxible_provider_runtime-*.whl")),),
        extras=("browser",),
    )
    assert installed.registered, installed
    pb = Playbill._from_client(client, instance_id=instance_id, workspace=tmp_path)

    def accept(draft):
        prepared = draft.prepare()
        assert not prepared.refused, prepared.diagnostics
        prepared.submit()
        _approve_and_activate(http, instance_id, reviewer, prepared.proposal.proposal_id)

    base = capture_contract(name="test.web")
    contract = base.model_copy(
        update={
            "logical_source_identities": ("web.response",),
            "coordinate_schema_pins": (
                capture_component_pin("coordinate-schema", "http-response-v1"),
            ),
            "selector_schema_pins": (
                capture_component_pin("selector-schema", "whole-response-v1"),
            ),
            "selection_budget": base.selection_budget.model_copy(update={"max_bytes": 1_048_576}),
        }
    )
    policy = _policy(name="source-reads", input_name="observation")
    accept(
        pb.changes(rationale="Observe a typed HTTP response.")
        .capture_contract(contract)
        .acquisition_policy(policy)
    )

    class Origin(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self):
            type(self).calls += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"severity":"high"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        Request = CarriedContractInput(
            name="fetch.input", fields={"url": PropertySchema(type="string")}
        )
        Result = CarriedContractInput(
            name="fetch.output",
            fields={
                "status": PropertySchema(type="int"),
                "text": PropertySchema(type="json", json_schema={"type": ["string", "null"]}),
            },
        )
        budget = ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=30_000_000),
            max_provider_calls=1,
            max_capture_bytes=2_097_152,
        )
        caps = ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=60_000_000),
            max_provider_calls=2,
            max_capture_bytes=4_194_304,
            max_repeat_attempts=1,
            max_items=100,
        )

        @procedure(
            name="observe-web",
            input=Request,
            output=Result,
            budget=budget,
            hard_caps=caps,
            acquisition_policy="source-reads",
        )
        def observer(request, bindings):
            observation = source(
                bindings.fetch,
                request=bindings.fetch.input(url=request.url, expected_format="json"),
                capture_contract="test.web",
            )
            return emit_capture(
                observation,
                capture_contract="test.web",
                result=Result.value(
                    status=observation.retrieved.status_code, text=observation.derived.text
                ),
            )

        observer = observer.bind(fetch=pb.provider_binding("web.fetch"))
        authored = observer.build(world=pb.world())
        assert "sha256:" not in authored.model_dump_json()
        preview = observer.preview(world=pb.world())
        assert preview.ready_for_prepare, preview.errors
        accept(pb.procedure(definition=observer))

        knowledge = pb.changes(rationale="Describe the advisory observation.")
        knowledge.subject(_subject())
        knowledge.claim_type(
            _claim_type(
                capture_contract_digest(contract).tagged, roles=("derivation", "observation")
            ).model_copy(update={"permitted_roles": ("derivation", "observation")})
        )
        accept(knowledge)

        @procedure(
            name="observe-parent",
            input=Request,
            output=Result,
            budget=budget,
            hard_caps=caps,
            terminal_capability=2,
        )
        def parent(request, world, bindings):
            observed = invoke(bindings.observer, input=bindings.observer.input(url=request.url))
            if not observed.succeeded:
                return halt("Observation failed")
            candidate = claim_candidate(
                subject=world.security.advisory["osv-2026-0001"],
                predicate=world.claim_type("security.advisory.severity"),
                value="high",
                role="observation",
                rationale="The acquired report is high severity.",
                supported_by=observed.terminal.capture,
            )
            return propose_change_set(
                candidates=[candidate],
                result=Result.value(status=observed.value.status, text=observed.value.text),
            )

        parent = parent.bind(observer=pb.accepted_procedure("observe-web").ref)
        accept(pb.procedure(definition=parent))
        accepted_parent = pb.accepted_procedure("observe-parent")
        refused = accepted_parent.run(
            input=accepted_parent.input(url=f"http://127.0.0.1:{server.server_port}/state.json")
        )
        assert refused.status == "admission_refused"
        assert Origin.calls == 0  # Child capture cannot acquire a Line's authority directly.
        now = datetime.now(timezone.utc)
        # Only the root is granted a mandate. The child inherits the same ceiling.
        accept(
            pb.changes(rationale="Run the exact parent under a governed Line.")
            .line(
                name="source-line",
                procedure="observe-parent",
                acquisition_policy="source-reads",
                requested_terminal_rung=2,
                parameters={"url": f"http://127.0.0.1:{server.server_port}/state.json"},
            )
            .procedure_mandate(
                ProcedureMandateInputV1(
                    kind="procedure_mandate",
                    name="source-authority",
                    procedure_name="observe-parent",
                    rung=2,
                    authority_ceiling=caps,
                    namespace=("claims",),
                    valid_from=now - timedelta(days=1),
                    expires_at=now + timedelta(days=1),
                )
            )
        )
        pb.refresh()
        before_run = pb.coordinate
        run = pb.run_line("source-line")
        assert run.succeeded, run.outcome.model_dump_json(indent=2)
        assert run.result.status == 200
        assert run.result.text == '{"severity":"high"}'
        assert Origin.calls == 1
        (child,) = run.children
        assert child.succeeded
        assert child.result.text == run.result.text
        assert child.outcome.source_observations[0].capture_digest
        capture = child.outcome.terminal_egress[0].children[0].egress_digest
        assert capture
        assert pb.coordinate == before_run  # A proposal is not an accepted state mutation.
        (proposal,) = run.outcome.terminal_egress
        assert proposal.kind == "propose_change_set" and proposal.verdict == "delivered"
        assert proposal.proposal_id
        _approve_and_activate(http, instance_id, reviewer, proposal.proposal_id)
        pb.refresh()

        @procedure(
            name="verify-parent",
            input=Request,
            output=Result,
            budget=budget,
            hard_caps=caps,
            terminal_capability=2,
        )
        def verify_parent(request, world, bindings):
            baseline = world.security.advisory["osv-2026-0001"].severity.one()
            observed = invoke(bindings.observer, input=bindings.observer.input(url=request.url))
            if not observed.succeeded:
                return halt("Observation failed")
            candidate = claim_candidate(
                subject=world.security.advisory["osv-2026-0001"],
                predicate=world.claim_type("security.advisory.severity"),
                value=baseline.value,
                role="derivation",
                rationale="Rechecked the accepted baseline against a fresh observation.",
                supported_by=observed.terminal.capture,
                basis=(baseline,),
                dispositions={baseline: Disposition.NOT_TESTED},
            )
            return propose_change_set(
                candidates=[candidate],
                result=Result.value(status=observed.value.status, text=observed.value.text),
            )

        verify_parent = verify_parent.bind(observer=pb.accepted_procedure("observe-web").ref)
        inspected = verify_parent.preview(world=pb.world())
        assert len(inspected.state_dependencies) == 1 and len(inspected.children) == 1
        assert inspected.state_dependencies[0].subject_kind == "security.advisory"
        accept(pb.procedure(definition=verify_parent))
        accept(
            pb.changes(rationale="Verify the same baseline with an exact child.")
            .line(
                name="verify-line",
                procedure="verify-parent",
                acquisition_policy="source-reads",
                requested_terminal_rung=2,
                parameters={"url": f"http://127.0.0.1:{server.server_port}/state.json"},
            )
            .procedure_mandate(
                ProcedureMandateInputV1(
                    kind="procedure_mandate",
                    name="verify-authority",
                    procedure_name="verify-parent",
                    rung=2,
                    authority_ceiling=caps,
                    namespace=("claims",),
                    valid_from=now - timedelta(days=1),
                    expires_at=now + timedelta(days=1),
                )
            )
        )
        pb.refresh()
        verified = pb.run_line("verify-line")
        # A direct-evidence rule cannot authorize a reducer merely because it
        # executed successfully. Preserve the existing gate until reducer
        # authorization can be bound without a ClaimType/Procedure digest cycle.
        assert verified.status == "node_refused"
        assert Origin.calls == 2
        (derived,) = verified.outcome.terminal_egress
        assert derived.verdict == "refused" and derived.proposal_id is None
        assert verified.outcome.terminal.code == "proposal_lowering_refused"
        assert (
            "playbill.evidence.reducer_not_allowed" in verified.outcome.terminal.model_dump_json()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
