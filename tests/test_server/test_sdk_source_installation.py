"""The typed SDK builder uses real installed provider wheels and normal run doors."""

from datetime import datetime, timedelta, timezone
from typing import Any

from cruxible_client import Cruxible
from cruxible_client.authoring.procedures import Handle, ProcedureLimits, Sequence, eq, halt
from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
from cruxible_client.contracts.procedures.authored import (
    ClaimsSubjectSpec,
    ClaimTemplateSpec,
    ReturnFields,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.shapes import Contract, contract_path, render_contract
from tests.core_support._light_capture_support import (
    JSON_DECODING,
    http_contract,
    procedure_contract,
)
from tests.core_support._subject_kind_support import declare_by_slug
from tests.test_procedures.test_procedure_proposal_delivery import (
    SUBJECT_ID,
    SUBJECT_KIND,
    _claim_type,
)
from tests.test_procedures.test_procedure_source_runs import _policy
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate
from tests.test_server.test_provider_installation import (  # noqa: F401
    _accept,
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

    # The fetched body is the origin's JSON: an extract reads it by the
    # Contract's decoding.
    contract = http_contract(name="test.web", decoding=JSON_DECODING)
    output_contract = procedure_contract(name="test.web-output", max_bytes=1_048_576)
    accept(
        pb.changes(rationale="Observe a typed HTTP response.")
        .capture_contract(contract)
        .capture_contract(output_contract)
    )

    # The accepted Contract an extract reads the fetched document's one record as.
    fetched = Contract.model_validate(
        {
            "name": "fetch.document",
            "version": 1,
            "fields": {"unit": {"type": "string"}, "station": {"type": "string"}},
        }
    )
    _accept(
        http,
        instance_id,
        reviewer,
        {contract_path(fetched.name, fetched.version): render_contract(fetched)},
        name="fetch-document",
    )

    request = {"url": "string"}
    # Typed as the extracted fetch.document row declares it.
    result = {"unit": "string", "station": "string"}
    limits = ProcedureLimits(
        wall_clock="PT60S",
        provider_calls=2,
        capture_bytes=4_194_304,
        repeat_attempts=1,
        items=100,
    )

    fetch_slot = ProcedureSlot.model_validate(
        {
            "in": "web.fetch.request@1",
            "out": "web.fetch.result@1",
            "effect": "external_read",
        }
    )

    def sequence(name: str) -> Sequence:
        return Sequence(
            name,
            params=request,
            output=result,
            slots={"fetch": fetch_slot},
            hard_caps=limits,
        )

    # Claims cite an extract of their parent's acquiring Call. The nested child
    # still exercises typed invocation and capture emission, without presenting
    # its opaque terminal Capture as the parent's extraction evidence.
    observer = Sequence(
        "observe-web",
        input=result,
        output=result,
        hard_caps=limits,
    )
    observer.emit(
        "test.web-output",
        input=observer.input.whole,
        result={
            "unit": observer.input["unit"],
            "station": observer.input["station"],
        },
    )
    authored = observer.build()
    assert "sha256:" not in authored.model_dump_json()
    preview = observer.preview()
    assert preview.ready_for_prepare, preview.errors
    accept(pb.procedure(definition=authored))

    knowledge = pb.changes(rationale="Describe the advisory observation.")
    # Core assigns a new Subject's id from its declared kind's key (identity D1).
    declare_by_slug(knowledge, SUBJECT_KIND)
    knowledge.subject(pb.new_subject(SUBJECT_KIND, key=SUBJECT_ID))
    definition = _claim_type(contract.identity.name)
    observed_rule = ClaimEvidenceAdmissionRule.model_validate(
        definition.evidence_admission_policy.rules[0].model_dump(exclude={"tag"})
    )
    definition = definition.model_copy(
        update={
            "permitted_roles": ("derivation", "observation"),
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
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

    def observed_result(observed: Handle) -> dict[str, Any]:
        return {
            "unit": observed.ref("value", "unit"),
            "station": observed.ref("value", "station"),
        }

    def observed_result_from(document: Handle) -> dict[str, Any]:
        return {
            "unit": document.ref("rows", "0", "unit"),
            "station": document.ref("rows", "0", "station"),
        }

    def acquire(parent: Sequence) -> Handle:
        observation = parent.call(
            "observation",
            slot="fetch",
            input={"url": parent.params["url"], "expected_format": "json"},
            captures={"capture": "test.web"},
        )
        document = parent.extract(
            "document",
            capture=observation["capture"],
            records="/",
            row="fetch.document@1",
            fields={"unit": {"at": "/unit"}, "station": {"at": "/station"}},
        )
        parent.guard(
            eq(document.ref("rows", "0", "unit"), "metres"), else_=halt("Observation failed")
        )
        observed = parent.invoke(
            "observe-web", input=observed_result_from(document), as_="observed"
        )
        parent.returns(ReturnFields(fields=observed_result(observed)))
        return document

    subject = ClaimsSubjectSpec(kind=SUBJECT_KIND, key={"slug": SUBJECT_ID}, create="never")
    parent = sequence("observe-parent")
    document = acquire(parent)
    parent.claims(
        (
            ClaimTemplateSpec(
                type=definition.predicate,
                value="high",
                role="observation",
                rationale="The acquired report is high severity.",
            ),
        ),
        for_each=document["records"],
        subject=subject,
    )
    accept(pb.procedure(definition=parent))
    accepted_parent = pb.accepted_procedure("observe-parent")
    refused = accepted_parent.run(parameters=accepted_parent.params(url=URL))
    assert refused.status == "admission_refused"
    # The child's terminal needs the Line lane: admission refuses before the
    # parent's acquiring Call, so nothing ran and nothing was fetched.
    assert refused.run_id is None and not refused.outcome.children
    assert _fetches(refused) == 0
    now = datetime.now(timezone.utc)
    # Only the root is granted a mandate. The child inherits the same ceiling.
    accept(
        pb.changes(rationale="Run the exact parent under a governed Line.").line(
            name="source-line",
            procedure="observe-parent",
            bindings={"fetch": fetch},
            acquisition={"observation.capture": _policy().model_dump(mode="json")},
            max_authority="propose",
            parameters={"url": URL},
        )
    )
    accept(
        pb.changes(rationale="Authorize the accepted source Line.").procedure_mandate(
            ProcedureMandateInput(
                kind="procedure_mandate",
                name="source-authority",
                line_name="source-line",
                grants="propose",
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
    assert run.result.unit == "metres"
    assert run.result.station == "newlyn"
    assert _fetches(run) == 1
    (child,) = run.children
    assert child.succeeded
    assert child.result.station == run.result.station
    assert _fetches(child) == 0
    assert child.outcome.source_observations == []
    (observation,) = run.outcome.source_observations
    assert observation.capture_digest
    capture = child.outcome.terminal_egress[0].children[0].egress_digest
    assert capture
    assert pb.coordinate == before_run  # A proposal is not an accepted state mutation.
    (proposal,) = run.outcome.terminal_egress
    assert proposal.kind == "claims" and proposal.verdict == "delivered"
    assert proposal.proposal_id
    _approve_and_activate(http, instance_id, reviewer, proposal.proposal_id)
    pb.refresh()

    verify_parent = sequence("verify-parent")
    baseline = verify_parent.read_claims(
        "baseline",
        claim_type="security.advisory.severity",
        subject="osv-2026-0001",
        subject_kind="security.advisory",
    )
    document = acquire(verify_parent)
    verify_parent.claims(
        (
            ClaimTemplateSpec(
                type=definition.predicate,
                value=baseline["value"],
                # Preserve the original observation alongside its verification;
                # a lower-precedence derivation cannot replace that observation.
                qualifier="verified",
                role="derivation",
                rationale="Rechecked the accepted baseline against a fresh observation.",
                inputs=(baseline.whole,),
            ),
        ),
        for_each=document["records"],
        subject=subject,
    )
    inspected = verify_parent.preview()
    assert inspected.ready_for_prepare, inspected.errors
    assert [node["kind"] for node in inspected.nodes] == [
        "read",
        "call",
        "extract",
        "guard",
        "invoke",
        "project",
        "claims",
        "halt",
    ]
    accept(pb.procedure(definition=verify_parent))
    accept(
        pb.changes(rationale="Verify the same baseline with an exact child.").line(
            name="verify-line",
            procedure="verify-parent",
            bindings={"fetch": fetch},
            acquisition={"observation.capture": _policy().model_dump(mode="json")},
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
                namespace=("claims",),
                valid_from=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
        )
    )
    pb.refresh()
    verified = pb.line("verify-line").run()
    assert verified.succeeded, verified.outcome.model_dump_json(indent=2)
    assert verified.result.station == "newlyn"
    assert _fetches(verified) == 1
    (rechecked,) = verified.children
    assert rechecked.succeeded
    assert rechecked.result.station == verified.result.station
    assert _fetches(rechecked) == 0
    assert rechecked.outcome.source_observations == []
    (fresh,) = verified.outcome.source_observations
    assert fresh.capture_digest
    # A fresh call, not a reuse of the first run's: its own run and invocation receipt.
    assert rechecked.run_id != child.run_id
    assert fresh.invocation_receipt_digest != observation.invocation_receipt_digest
    (derived,) = verified.outcome.terminal_egress
    assert derived.kind == "claims" and derived.verdict == "delivered" and derived.proposal_id

    def severity_claims() -> list[Any]:
        # The advisory also holds its key Claim (security.advisory.slug).
        subject = pb.world().security.advisory[SUBJECT_ID]
        return [view for view in subject.claims if view.predicate == definition.predicate]

    assert len(severity_claims()) == 1
    _approve_and_activate(http, instance_id, reviewer, derived.proposal_id)
    pb.refresh()
    claims = severity_claims()
    assert len(claims) == 2
    assert {view.role: view.qualifier for view in claims} == {
        "observation": None,
        "derivation": "verified",
    }
    assert all(view.value == "high" for view in claims)
    original = _proof(
        client, instance_id, next(v.claim_id for v in claims if v.role == "observation")
    )
    derived_claim = _proof(
        client, instance_id, next(v.claim_id for v in claims if v.role == "derivation")
    )
    from cruxible_client.contracts.claims import ClaimBacking

    original_backing = ClaimBacking.model_validate(
        next(
            fact["value"]
            for fact in original["facts"]
            if fact["schema_id"] == "cruxible.claim.backing"
        )
    )
    assert original_backing.capture_digests == (observation.capture_digest,)
    backing = ClaimBacking.model_validate(
        next(
            fact["value"]
            for fact in derived_claim["facts"]
            if fact["schema_id"] == "cruxible.claim.backing"
        )
    )
    assert backing.capture_digests == (fresh.capture_digest,)
    assert backing.input_claim_digests == (original["envelope"]["artifact_digest"],)
    assert (
        backing.reducer_digest
        == procedure_artifact_digest(pb.accepted_procedure("verify-parent").definition).tagged
    )
    claim_type = _proof(client, instance_id, f"ClaimType:{definition.predicate}")
    assert claim_type["envelope"] == definition.model_dump(mode="json")
