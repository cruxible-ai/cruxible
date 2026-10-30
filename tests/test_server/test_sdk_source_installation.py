"""Typed source authoring uses real installed provider wheels and normal run doors."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cruxible_client import Cruxible, Disposition
from cruxible_client.authoring.inputs import CarriedContractInput
from cruxible_client.authoring.procedures import ProcedureCompositionError
from cruxible_client.authoring.source import (
    claim_candidate,
    emit_capture,
    halt,
    invoke,
    procedure,
    propose_change_set,
    source,
)
from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
from cruxible_client.contracts.captures import (
    CanonicalDuration,
    capture_contract_digest,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV2,
    ClaimEvidenceAdmissionRuleV2,
)
from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import ProcedureBudget, ProcedureHardCaps
from tests.core_support._light_capture_support import http_contract, procedure_contract
from tests.core_support._subject_kind_support import declare_by_slug
from tests.test_procedures.test_procedure_proposal_delivery import (
    SUBJECT_ID,
    SUBJECT_KIND,
    _claim_type,
)
from tests.test_procedures.test_procedure_source_runs import _policy
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate
from tests.test_server.test_provider_installation import (  # noqa: F401
    installed_web,
    installer_http,
)

# web.fetch refuses private and loopback origins, so every run reads the recording the web
# package ships for the reserved fixture.invalid host; it opens no socket.
URL = "https://fixture.invalid/api/v1/measurements.json"


def _calls(run) -> list[str]:
    """The Provider invocation receipts a run tree journaled, one per web.fetch call.

    Each run's receipt lists the calls it made, and each call's Source observation names
    the same invocation receipt beside the Capture that call produced.
    """

    state = run.outcome
    own = sorted(item.invocation_receipt_digest for item in state.source_observations)
    assert all(item.capture_digest for item in state.source_observations)
    assert own == (
        [] if state.receipt is None else sorted(state.receipt.invocation_receipt_digests)
    )
    return own + [digest for child in run.children for digest in _calls(child)]


def _fetches(run) -> int:
    """How many web.fetch calls a run tree made; the recorded origin keeps no count."""

    calls = _calls(run)
    receipt = run.outcome.receipt
    # One budget spans the run tree, so the root's receipt counts every call beneath it.
    assert (0 if receipt is None else receipt.budget.observed.provider_calls) == len(calls)
    return len(calls)


def _proof(client: Any, instance_id: str, ref: str) -> dict[str, Any]:
    from cruxible_client.contracts.get_reads import GetRequest

    proof = client.get(instance_id, request=GetRequest(ref=ref, detail="proof")).proof
    assert proof is not None
    return dict(proof)


@pytest.mark.xfail(
    strict=True,
    raises=ProcedureCompositionError,
    reason="needs B3: content.json extraction -- the source compiler types a Source "
    "observation as its slot's out Contract (Capture wire), so the decoded bundle's "
    "observation.retrieved / observation.derived do not resolve",
)
def test_installed_fetch_parent_proposal_and_accepted_derivation(
    installer_http,  # noqa: F811
    installed_web,  # noqa: F811
    tmp_path,
):
    http, instance_id, reviewer = installer_http
    # The real web wheel, installed after the Contracts its operations pin.
    client, installed, _digests = installed_web
    fetch = f"{installed.build}#web.fetch"
    pb = Cruxible._from_client(client, instance_id=instance_id, workspace=tmp_path)

    def accept(draft):
        prepared = draft.prepare()
        assert not prepared.refused, prepared.diagnostics
        prepared.submit(accept="never")
        _approve_and_activate(http, instance_id, reviewer, prepared.proposal.proposal_id)

    contract = http_contract(name="test.web")
    output_contract = procedure_contract(name="test.web-output", max_bytes=1_048_576)
    accept(
        pb.changes(rationale="Observe a typed HTTP response.")
        .capture_contract(contract)
        .capture_contract(output_contract)
    )

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
    budget = ProcedureBudget(
        wall_clock=CanonicalDuration(microseconds=30_000_000),
        max_provider_calls=1,
        max_capture_bytes=2_097_152,
    )
    caps = ProcedureHardCaps(
        max_wall_clock=CanonicalDuration(microseconds=60_000_000),
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
        # One slot of web.fetch's exact signature; a Line binds it to a build.
        slots={
            "fetch": {
                "in": "web.fetch.request@1",
                "out": "web.fetch.result@1",
                "effect": "external_read",
            }
        },
    )
    def observer(request, bindings):
        observation = source(
            bindings.fetch,
            request=bindings.fetch.input(url=request.url, expected_format="json"),
            capture_contract="test.web",
        )
        return emit_capture(
            observation,
            capture_contract="test.web-output",
            result=Result.value(
                status=observation.retrieved.status_code, text=observation.derived.text
            ),
        )

    authored = observer.build(world=pb.world())
    assert "sha256:" not in authored.model_dump_json()
    preview = observer.preview(world=pb.world())
    assert preview.ready_for_prepare, preview.errors
    # The Source's acquisition rule is the child Procedure's own default: a
    # Line's acquisition rules name only its root Procedure's Sources.
    built = type(authored).model_validate(
        {**authored.model_dump(mode="json"), "acquisition_defaults": {"observation": _policy()}}
    )
    accept(pb.procedure(definition=built))

    knowledge = pb.changes(rationale="Describe the advisory observation.")
    # Core assigns a new Subject's id from its declared kind's key (identity D1).
    declare_by_slug(knowledge, SUBJECT_KIND)
    knowledge.subject(pb.new_subject(SUBJECT_KIND, key=SUBJECT_ID))
    definition = _claim_type(capture_contract_digest(output_contract).tagged)
    observed_rule = ClaimEvidenceAdmissionRuleV2.model_validate(
        definition.evidence_admission_policy.rules[0].model_dump(
            exclude={"tag", "allowed_reducer_digests"}
        )
    )
    definition = definition.model_copy(
        update={
            "artifact_format": "playbill-claim-type-v5",
            "permitted_roles": ("derivation", "observation"),
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV2(
                rules=tuple(
                    sorted(
                        (
                            observed_rule,
                            observed_rule.model_copy(
                                update={
                                    "rule_id": "derived-observation",
                                    "claim_roles": ("derivation",),
                                    "admission": "derivational",
                                }
                            ),
                        ),
                        key=lambda rule: rule.rule_id,
                    )
                )
            ),
        }
    )
    knowledge.claim_type(definition)
    accept(knowledge)

    @procedure(
        name="observe-parent",
        input=Request,
        output=Result,
        budget=budget,
        hard_caps=caps,
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
    refused = accepted_parent.run(input=accepted_parent.input(url=URL))
    assert refused.status == "admission_refused"
    # Child capture cannot acquire a Line's authority directly: nothing ran, nothing fetched.
    assert refused.run_id is None and not refused.outcome.children
    assert _fetches(refused) == 0
    now = datetime.now(timezone.utc)
    # Only the root is granted a mandate. The child inherits the same ceiling.
    accept(
        pb.changes(rationale="Run the exact parent under a governed Line.")
        .line(
            name="source-line",
            procedure="observe-parent",
            bindings={"invoke_1.fetch": fetch},
            max_authority="propose",
            parameters={"url": URL},
        )
        .procedure_mandate(
            ProcedureMandateInput(
                kind="procedure_mandate",
                name="source-authority",
                line_name="source-line",
                grants="propose",
                resource_ceiling=caps,
                namespace=("claims",),
                valid_from=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
        )
    )
    pb.refresh()
    before_run = pb.coordinate
    run = pb.line("source-line").run()
    assert run.succeeded, run.outcome.model_dump_json(indent=2)
    assert run.result.status == 200
    assert '"station": "newlyn"' in run.result.text
    assert _fetches(run) == 1
    (child,) = run.children
    assert child.succeeded
    assert child.result.text == run.result.text
    (observation,) = child.outcome.source_observations
    assert observation.capture_digest
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
        pb.changes(rationale="Verify the same baseline with an exact child.").line(
            name="verify-line",
            procedure="verify-parent",
            bindings={"invoke_1.fetch": fetch},
            max_authority="propose",
            parameters={"url": URL},
        )
    )
    pb.refresh()
    denied = pb.line("verify-line").run()
    assert denied.status == "admission_refused"
    assert denied.run_id is None and not denied.outcome.children
    assert _fetches(denied) == 0
    accept(
        pb.changes(
            rationale="Authorize the exact derivation Procedure separately from its ClaimType."
        ).procedure_mandate(
            ProcedureMandateInput(
                kind="procedure_mandate",
                name="verify-authority",
                line_name="verify-line",
                grants="propose",
                resource_ceiling=caps,
                namespace=("claims",),
                valid_from=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
        )
    )
    pb.refresh()
    verified = pb.line("verify-line").run()
    assert verified.succeeded, verified.outcome.model_dump_json(indent=2)
    assert '"station": "newlyn"' in verified.result.text
    assert _fetches(verified) == 1
    (rechecked,) = verified.children
    (fresh,) = rechecked.outcome.source_observations
    assert fresh.capture_digest
    # A fresh call, not a reuse of the first run's: its own run and invocation receipt.
    assert rechecked.run_id != child.run_id
    assert fresh.invocation_receipt_digest != observation.invocation_receipt_digest
    (derived,) = verified.outcome.terminal_egress
    assert derived.verdict == "delivered" and derived.proposal_id

    def severity_claims() -> list[Any]:
        # The advisory also holds its key Claim (security.advisory.slug).
        subject = pb.world().security.advisory[SUBJECT_ID]
        return [view for view in subject.claims if view.predicate == definition.predicate]

    assert len(severity_claims()) == 1
    _approve_and_activate(http, instance_id, reviewer, derived.proposal_id)
    pb.refresh()
    claims = severity_claims()
    assert len(claims) == 2
    original = _proof(
        client, instance_id, next(v.claim_id for v in claims if v.role == "observation")
    )
    derived_claim = _proof(
        client, instance_id, next(v.claim_id for v in claims if v.role == "derivation")
    )
    from cruxible_client.contracts.claims import ClaimBacking

    backing = ClaimBacking.model_validate(
        next(
            fact["value"]
            for fact in derived_claim["facts"]
            if fact["schema_id"] == "cruxible.claim.backing"
        )
    )
    assert backing.input_claim_digests == (original["envelope"]["artifact_digest"],)
    assert (
        backing.reducer_digest
        == procedure_artifact_digest(pb.accepted_procedure("verify-parent").definition).tagged
    )
    claim_type = _proof(client, instance_id, f"ClaimType:{definition.predicate}")
    assert claim_type["envelope"] == definition.model_dump(mode="json")
