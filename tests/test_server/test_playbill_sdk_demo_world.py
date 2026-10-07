"""S1 acceptance: demo-world beat 1 through the public SDK and HTTP daemon."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import (
    Cardinality,
    ClaimObjectKind,
    ClaimRef,
    ClaimRole,
    ClaimTypeRef,
    Cruxible,
    Disposition,
    Duration,
    ReferentSensitivity,
    SubjectRef,
)
from cruxible_client.authoring.bind import bind_working_selection_input
from cruxible_client.authoring.examples import authoring_example
from cruxible_client.authoring.inputs import (
    CarriedContractInput,
    ClaimInput,
    ProcedureInput,
    QueryDefinitionInput,
)
from cruxible_client.contracts import ClaimViewRecord
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.authoring.models import PreflightResult
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CanonicalDuration,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import claim_type_digest
from cruxible_client.contracts.get_reads import GetProcedureCard, GetRequest
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureHardCaps,
)
from cruxible_client.contracts.query.definitions import QueryDefinition, QueryEvaluationPolicy
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimPresenceFilter,
    QueryEntry,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._claim_type_support import (
    claim_type_input_example,
    defaulted_claim_type_input_example,
)
from tests.support.scoped_query_oracle import _scoped_facts_answer_as_whole_facts  # noqa: F401


def _catalog(workspace: Path) -> None:
    (workspace / ".cruxible").mkdir()
    (workspace / "corpus").mkdir()
    (workspace / ".cruxible" / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: corpus.escalation-policy
    locator: corpus/escalation-policy.md
    document_id: escalation-policy
    document_kind: policy
    title: Escalation policy
    media_type: text/markdown
    governance_scope: [Document:escalation-policy]
  - name: corpus.infra-inventory
    locator: corpus/infra-inventory.md
    document_id: infra-inventory
    document_kind: inventory
    title: Infrastructure inventory
    media_type: text/markdown
    governance_scope: [Document:infra-inventory]
  - name: corpus.vuln-response-runbook
    locator: corpus/vuln-response-runbook.md
    document_id: vuln-response-runbook
    document_kind: runbook
    title: Vulnerability response runbook
    media_type: text/markdown
    governance_scope: [Document:vuln-response-runbook]
""",
        encoding="utf-8",
    )
    (workspace / "corpus" / "escalation-policy.md").write_text(
        "# Escalation and change policy\n\n"
        "Emergency changes require one approver from the platform-leads group.\n",
        encoding="utf-8",
    )
    (workspace / "corpus" / "infra-inventory.md").write_text(
        "# Service inventory — edge tier\n\n"
        "## payments-edge\n"
        "Runs nginx 1.24.0 with the lua-gateway module in the request path. "
        "Reachable from the public internet.\n\n"
        "## partner-api\n"
        "Runs nginx 1.24.0. The lua-gateway module is installed but disabled. VPN-only.\n\n"
        "## batch-ingest\n"
        "Runs nginx 1.22.1 without lua-gateway. Internal subnet only.\n",
        encoding="utf-8",
    )
    (workspace / "corpus" / "vuln-response-runbook.md").write_text(
        "# Vulnerability response runbook\n\n"
        "Critical internet-facing systems must patch within seventy-two hours.\n"
        "The KEV patch deadline tightens to forty-eight hours.\n"
        "Similarity is not verification: confirm the exact deployed version.\n",
        encoding="utf-8",
    )


def _approve_and_activate(
    client: TestClient,
    instance_id: str,
    private_key_path: Path,
    proposal_id: str,
) -> None:
    challenge_response = client.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "reviewer"},
    )
    assert challenge_response.status_code == 200, challenge_response.text
    challenge = challenge_response.json()
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=private_key_path,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    approved = client.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    activated = client.post(f"/api/v1/{instance_id}/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "accepted"


def _install_direct_capture_contract(
    client: TestClient,
    instance_id: str,
    private_key_path: Path,
) -> str:
    """Accept the built-in direct contract so lint has a real replacement target."""

    instance = get_playbill_manager().get(instance_id)
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    contract = DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
    tree[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
    submitted = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="operator"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/operator/install-direct-capture-contract",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-29T12:00:00.000000Z",
    )
    assert submitted.candidate is not None
    _approve_and_activate(
        client,
        instance_id,
        private_key_path,
        submitted.admission.proposal_id,
    )
    instance.refresh()
    return capture_contract_digest(contract).tagged


def _claim_proof(transport: CruxibleClient, instance_id: str, claim_id: str) -> ClaimViewRecord:
    """One accepted Claim's full read, through get(detail="proof")."""

    proof = transport.get(instance_id, request=GetRequest(ref=claim_id, detail="proof")).proof
    return ClaimViewRecord.model_validate(proof)


def _accepted_claims(pb: Cruxible, predicate: str) -> list[str]:
    """Every accepted Claim of ``predicate`` across the demo's two Subject kinds."""

    return [
        entry["claim"]
        for kind in ("secops.policy", "secops.service")
        for row in pb.query(kind, select=[predicate], claims=True, limit=500).rows
        for entries in (row.get("claims") or {}).values()
        for entry in entries
        if entry["status"] == "accepted"
    ]


def test_empty_evidence_policy_is_candidate_through_cli_and_sdk(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, instance_id, private_key_path = playbill_http
    _install_direct_capture_contract(http, instance_id, private_key_path)
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    input_value = claim_type_input_example()
    input_path = tmp_path / "claim-type-input.json"
    input_path.write_text(json.dumps(input_value.model_dump(mode="json")), encoding="utf-8")

    cli_result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://cruxible",
            "--instance-id",
            instance_id,
            "claim-type",
            "propose",
            "--input",
            str(input_path),
            "--name",
            "empty-policy-cli",
            "--json",
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output
    cli_proposal = json.loads(cli_result.stdout)
    assert cli_proposal["proposal"]["proposal"]["evaluation"]["verdict"] == "candidate"

    workspace = tmp_path / "sdk-workspace"
    workspace.mkdir()
    _catalog(workspace)
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)
    draft = pb.claim_type(
        predicate=input_value.predicate,
        subject_kinds=input_value.allowed_subject_kinds,
        object_kind=input_value.object_kind,
        value_schema=input_value.literal_schema,
        object_subject_kinds=input_value.allowed_object_subject_kinds,
        cardinality=input_value.cardinality,
        permitted_roles=input_value.permitted_roles,
        referent_sensitivity=input_value.referent_sensitivity,
        sources=(),
        admission_policy=ClaimAdmissionPolicy.model_validate(input_value.admission_policy),
        resolution_policy=ClaimResolutionPolicy.model_validate(input_value.resolution_policy),
        pins=(),
        evidence_freshness=None,
    )
    # The same definition through the HTTP envelope route the draft uses.
    http_proposal = http.post(
        f"/api/v1/{instance_id}/claim-types/proposals",
        json={
            "claim_type": draft.definition.model_dump(mode="json"),
            "proposal_name": "empty-policy-http",
        },
    )
    assert http_proposal.status_code == 200, http_proposal.text
    sdk_proposal = draft.propose(proposal_name="empty-policy-sdk")

    # get is the one proposal read; the typed propose result carries the lint.
    assert pb.get(sdk_proposal.proposal.proposal_id).value.verdict == "candidate"
    assert cli_proposal["lint"]["warnings"]
    assert {warning["code"] for warning in cli_proposal["lint"]["warnings"]} == {
        "cruxible.claim_type.evidence_policy_admits_no_accepted_contract"
    }
    # Warning parity: CLI, HTTP and SDK lint the same draft identically.
    sdk_warnings = [item.model_dump(mode="json") for item in sdk_proposal.lint.warnings]
    assert sdk_warnings == http_proposal.json()["lint"]["warnings"]
    assert sdk_warnings == cli_proposal["lint"]["warnings"]


def test_cli_claim_type_input_is_accepted_in_a_fresh_world(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, instance_id, private_key_path = playbill_http
    _install_direct_capture_contract(http, instance_id, private_key_path)
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    runner = CliRunner()
    example_payload = defaulted_claim_type_input_example().model_dump(mode="json")
    # The rule also names the accepted direct contract, by identity.
    rule = example_payload["evidence_admission_policy"]["rules"][0]
    rule["capture_contracts"] = sorted(
        [*rule["capture_contracts"], DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.qualified]
    )
    input_path = tmp_path / "claim-type-input.json"
    input_path.write_text(json.dumps(example_payload), encoding="utf-8")

    proposed = runner.invoke(
        cli,
        [
            "--server-url",
            "http://cruxible",
            "--instance-id",
            instance_id,
            "claim-type",
            "propose",
            "--input",
            str(input_path),
            "--json",
        ],
    )

    assert proposed.exit_code == 0, proposed.output
    payload = json.loads(proposed.stdout)
    assert payload["proposal"]["proposal"]["evaluation"]["verdict"] == "candidate"
    assert payload["lint"]["warnings"] == []


def _assess_procedure(query: str) -> ProcedureInput:
    """The assess Procedure over the accepted policy query, its Contracts carried."""

    def carried(name: str, role: str) -> dict[str, str]:
        return {"kind": "carried_contract", "name": name, "role": role}

    contract_in = carried("assess-input", "contract-in")
    contract_out = carried("assess-result", "contract-out")
    return ProcedureInput(
        kind="procedure",
        definition={
            "graph_format": 6,
            "name": "secops.vuln.assess",
            "description": "Classify a vulnerability from governed policy and service facts.",
            "contract_in": contract_in,
            "contract_out": contract_out,
            "nodes": [
                {
                    "kind": "state_tap",
                    "node_id": "read-policy",
                    "query": {"kind": "accepted", "role": "query", "target": query},
                    "parameters": {},
                    "as": "policy_rows",
                    "next": "classify",
                },
                {
                    "kind": "transform",
                    "node_id": "classify",
                    "transform_kind": "adapter",
                    "contract_in": contract_in,
                    "contract_out": contract_out,
                    "spec": {
                        "tag": "playbill-transform-adapter-spec-v1",
                        "value": {"input": "$steps.policy_rows"},
                    },
                    "as": "decision",
                    "next": "result",
                },
                {
                    "kind": "project",
                    "node_id": "result",
                    "fields": {"lane": "$steps.decision.lane"},
                    "contract_out": contract_out,
                    "as": "result",
                },
            ],
            "returns": "result",
            "budget": ProcedureBudget(
                wall_clock=CanonicalDuration(microseconds=1_000_000),
                max_provider_calls=0,
                max_capture_bytes=0,
            ).model_dump(mode="json"),
            "hard_caps": ProcedureHardCaps(
                max_wall_clock=CanonicalDuration(microseconds=2_000_000),
                max_provider_calls=0,
                max_capture_bytes=0,
                max_items=200,
                max_repeat_attempts=1,
            ).model_dump(mode="json"),
            "terminal_capability": 1,
        },
        contracts=(
            CarriedContractInput(name="assess-input", fields={}, allow_extra=True),
            CarriedContractInput(name="assess-result", fields={}, allow_extra=True),
        ),
        activation_policy="drain",
    )


def test_sdk_cold_claim_delivers_source_lint_without_refusing_preflight(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    http, instance_id, _private_key_path = playbill_http
    workspace = tmp_path / "lint-world"
    workspace.mkdir()
    _catalog(workspace)
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)
    subject = pb.subject(
        subject="secops.policy/patch-sla",
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )
    claim_type = pb.claim_type(
        predicate="secops.policy.patch_sla",
        subject_kinds=("secops.policy",),
        object_kind=ClaimObjectKind.LITERAL,
        value_schema={"type": "integer"},
        object_subject_kinds=(),
        cardinality=Cardinality.ONE,
        permitted_roles=(ClaimRole.NORMATIVE,),
        referent_sensitivity=ReferentSensitivity.IDENTITY,
        sources=(),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        pins=(),
        evidence_freshness=None,
    )

    intent = pb.claim(
        subject=subject.address,
        predicate=claim_type.predicate,
        value=48,
        role=ClaimRole.NORMATIVE,
        rationale="The runbook fixes the KEV deadline.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("forty-eight hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=subject,
        claim_type_definition=claim_type,
    ).prepare()

    assert not intent.refused
    assert intent.lint is not None
    assert intent.warnings == tuple(intent.lint.warnings)
    assert intent.warnings[0].code == "cruxible.claim_type.anticipated_source_contract_omitted"
    assert intent.warnings[0].source_id == "corpus.vuln-response-runbook"
    assert intent._preflight is not None
    response = intent._preflight.model_dump(mode="json")
    response.pop("lint")
    assert PreflightResult.model_validate(response).verdict == "passed"


def test_sdk_revises_an_existing_claim_using_refs_without_dependency_drafts(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    http, instance_id, private_key_path = playbill_http
    workspace = tmp_path / "supersession-world"
    workspace.mkdir()
    _catalog(workspace)
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)
    subject = pb.subject(
        subject="secops.policy/patch-sla",
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )
    claim_type = pb.claim_type(
        predicate="secops.policy.patch_sla",
        subject_kinds=("secops.policy",),
        object_kind=ClaimObjectKind.LITERAL,
        value_schema={"type": "integer"},
        object_subject_kinds=(),
        cardinality=Cardinality.ONE,
        permitted_roles=(ClaimRole.NORMATIVE,),
        referent_sensitivity=ReferentSensitivity.IDENTITY,
        sources=("corpus.vuln-response-runbook",),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        pins=(),
        evidence_freshness=None,
    )
    initial = pb.claim(
        subject=subject.address,
        predicate=claim_type.predicate,
        value=48,
        role=ClaimRole.NORMATIVE,
        rationale="The original runbook records a forty-eight-hour deadline.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("forty-eight hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=subject,
        claim_type_definition=claim_type,
    ).prepare()
    assert not initial.refused, initial.diagnostics
    initial.submit()
    first_proposal = initial.status().proposal_id
    assert first_proposal is not None
    original_coordinate = pb.coordinate
    _approve_and_activate(http, instance_id, private_key_path, first_proposal)
    claim_id = str(initial._raw["semantic_identity"])
    assert pb.coordinate == original_coordinate

    unrefreshed = pb.claim(
        subject="secops.policy/patch-sla.yaml",
        predicate=claim_type.predicate,
        value=72,
        role=ClaimRole.NORMATIVE,
        rationale="Resolve accepted dependencies at the daemon's current coordinate.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("seventy-two hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=claim_id,
        dispositions={claim_id: Disposition.CONTRADICT},
        subject_definition=None,
        claim_type_definition=None,
    )
    assert unrefreshed.reference_expectations == ()
    prepared_revision = unrefreshed.prepare()
    assert not prepared_revision.refused
    assert pb.coordinate == original_coordinate

    # A `revises` submit amends one Claim identity in place; the result says so
    # rather than reading like an ordinary create.
    revision_submit = transport.submit_authoring_intent(instance_id, prepared_revision.intent_id)
    assert revision_submit.identity_stable is True
    assert revision_submit.claim_revision == 2

    missing_revision_id = "CLM-" + "f" * 32
    missing_revision = pb.claim(
        subject="secops.policy/patch-sla.yaml",
        predicate=claim_type.predicate,
        value=72,
        role=ClaimRole.NORMATIVE,
        rationale="A missing revision remains a located authoring refusal.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("seventy-two hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=missing_revision_id,
        dispositions={claim_id: Disposition.CONTRADICT},
        subject_definition=None,
        claim_type_definition=None,
    ).prepare()
    diagnostic = next(
        item
        for item in missing_revision.diagnostics
        if item.code == "cruxible.authoring.claim_predecessor_not_found"
    )
    assert diagnostic.offending_element == "revises"
    assert diagnostic.call_site is not None
    assert diagnostic.call_site.expression == "missing_revision_id"

    pb.refresh()
    predecessor = _claim_proof(transport, instance_id, claim_id)

    current = pb.next(expiring_within=Duration.days(count=7))
    assert "workspace_sources" in current.observed_domains
    assert not any(item.reason == "citation_drifted" for item in current.items)
    runbook = workspace / "corpus" / "vuln-response-runbook.md"
    original_runbook = runbook.read_text(encoding="utf-8")
    runbook.write_text(original_runbook + "\nAn ungoverned source edit.\n", encoding="utf-8")
    unrelated = pb.next(expiring_within=Duration.days(count=7))
    assert not any(item.reason == "citation_drifted" for item in unrelated.items)
    runbook.write_text(
        original_runbook.replace("forty-eight hours", "forty-nine hours"),
        encoding="utf-8",
    )
    drifted = pb.next(expiring_within=Duration.days(count=7))
    drift = next(item for item in drifted.items if item.reason == "citation_drifted")
    assert drift.subject_identity == f"Claim:{claim_id}"
    assert drift.detail["drift_state"] == "changed"
    assert drift.detail["logical_source"] == {
        "tag": "playbill-logical-source-identity-v1",
        "plane": "external",
        "identity": "corpus.vuln-response-runbook",
    }
    # The repair is the set verb that restates the Claim on the redrifted source:
    # the door on every profile (full is a superset), and the tool its tier gate names.
    assert drift.repair.operation == "cruxible.set"
    assert drift.repair.required_change == "adjudicate_citation_drift"
    assert drift.repair.arguments["source_id"] == "corpus.vuln-response-runbook"
    assert drift.repair.arguments["claim_id"] == claim_id
    assert drift.repair.command is not None
    assert drift.repair.command.startswith("cx.set(")
    runbook.write_text(original_runbook, encoding="utf-8")
    reverted = pb.next(expiring_within=Duration.days(count=7))
    assert not any(item.reason == "citation_drifted" for item in reverted.items)

    catalog = workspace / ".cruxible" / "sources.yaml"
    original_catalog = catalog.read_text(encoding="utf-8")
    catalog.write_text(
        original_catalog.split("  - name: corpus.vuln-response-runbook\n", maxsplit=1)[0],
        encoding="utf-8",
    )
    missing = pb.next(expiring_within=Duration.days(count=7))
    note = next(item for item in missing.items if item.reason == "citation_source_unobserved")
    assert note.subject_identity == f"Claim:{claim_id}"
    assert note.detail["source_id"] == "corpus.vuln-response-runbook"
    assert note.repair.required_change == "observe_cited_source"
    catalog.write_text(original_catalog, encoding="utf-8")

    runbook.rename(workspace / "temporarily-unavailable-runbook.md")
    unavailable = pb.next(expiring_within=Duration.days(count=7))
    unavailable_note = next(
        item for item in unavailable.items if item.reason == "citation_source_unobserved"
    )
    assert unavailable_note.detail["source_id"] == "corpus.vuln-response-runbook"
    (workspace / "temporarily-unavailable-runbook.md").rename(runbook)

    incomplete = pb.claim(
        subject="secops.policy/patch-sla.yaml",
        predicate=claim_type.predicate,
        value=72,
        role=ClaimRole.NORMATIVE,
        rationale="A revised Claim must disposition its accepted predecessor.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("seventy-two hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=claim_id,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )
    response = http.post(
        f"/api/v1/{instance_id}/authoring/compile",
        json={
            "tag": "playbill-authoring-intent-compile-request-v3",
            "payload": incomplete.payload.model_dump(mode="json"),
            "reference_expectations": [
                item.model_dump(mode="json") for item in incomplete.reference_expectations
            ],
            "program_stamp": incomplete.program_stamp.model_dump(mode="json"),
            "intent_id": None,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "passed"
    assert not any(
        item["code"] == "cruxible.authoring.existing_claim_dispositions_incomplete"
        for item in response.json()["frontier"]["diagnostics"]
    )

    revision = pb.claim(
        subject="secops.policy/patch-sla.yaml",
        predicate=claim_type.predicate,
        value=72,
        role=ClaimRole.NORMATIVE,
        rationale="The existing governed policy now uses its seventy-two-hour boundary.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("seventy-two hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=claim_id,
        dispositions={claim_id: Disposition.CONTRADICT},
        subject_definition=None,
        claim_type_definition=None,
    )
    assert revision.payload.dependency_drafts.subject is None
    assert revision.payload.dependency_drafts.claim_type is None
    assert revision.reference_expectations == ()
    intent = revision.prepare()
    assert not intent.refused, intent.diagnostics
    intent.submit()
    proposal_id = intent.status().proposal_id
    assert proposal_id is not None
    _approve_and_activate(http, instance_id, private_key_path, proposal_id)

    successor = _claim_proof(transport, instance_id, claim_id)
    facts = {fact["schema_id"]: fact["value"] for fact in successor.facts}
    assert facts["cruxible.claim.statement"]["object"]["value"] == 72
    assert successor.envelope["predecessor_digest"] is not None
    assert successor.envelope["identity"] == predecessor.envelope["identity"]

    # The successor retains the predecessor's historical citation, but only the
    # new revision's admitted verdict Capture belongs in the repair inventory.
    pb.refresh()
    runbook.write_text(
        original_runbook.replace("forty-eight hours", "forty-nine hours"),
        encoding="utf-8",
    )
    after_successor = pb.next(expiring_within=Duration.days(count=7))
    assert all(item.reason != "citation_drifted" for item in after_successor.items)


def test_shipped_claim_type_and_flow_a_examples_compose_to_a_supported_claim(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    http, instance_id, private_key_path = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    workspace = tmp_path / "example-workspace"
    workspace.mkdir()
    _catalog(workspace)
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)

    subject = pb.subject(
        subject="project.work_item/replace-me",
        pins=(),
        lifecycle=ArtifactLifecycle(),
    ).prepare()
    assert not subject.refused, subject.diagnostics
    subject.submit()
    subject_proposal_id = subject.status().proposal_id
    assert subject_proposal_id is not None
    _approve_and_activate(
        http,
        instance_id,
        private_key_path,
        subject_proposal_id,
    )
    pb.refresh()

    claim_type_input = defaulted_claim_type_input_example()
    claim_type_proposal = transport.propose_claim_type_input(
        instance_id,
        input=claim_type_input.model_dump(mode="json"),
        proposal_name="example-claim-type",
    )
    _approve_and_activate(
        http,
        instance_id,
        private_key_path,
        claim_type_proposal.proposal.proposal["admission"]["proposal_id"],
    )

    claim_input = ClaimInput.model_validate(
        authoring_example("claim-flow-a").model_dump(mode="json")
    )
    assert claim_input.predicate == claim_type_input.predicate
    bound = bind_working_selection_input(
        claim_input,
        content=b"status: replace-me\n",
        anchor="replace-me",
    )
    compiled = transport.compile_authoring(
        instance_id,
        payload=bound.model_dump(mode="json"),
    )
    assert compiled.verdict == "passed", compiled.frontier
    intent_id = str(compiled.certificate.intent_id)
    submitted = transport.submit_authoring_intent(instance_id, intent_id)
    assert submitted.status.proposal_id is not None
    _approve_and_activate(
        http,
        instance_id,
        private_key_path,
        submitted.status.proposal_id,
    )

    claim_id = str(submitted.intent["semantic_identity"])
    explained = transport.get(instance_id, request=GetRequest(ref=claim_id, detail="why")).why
    assert explained is not None and explained["verdict"]["verdict"] == "supported"


def test_demo_world_beat_one_converts_corpus_through_one_sdk_program(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    http, instance_id, private_key_path = playbill_http
    workspace = tmp_path / "demo-world"
    workspace.mkdir()
    _catalog(workspace)
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)

    policy_subject = pb.subject(
        subject="secops.policy/patch-sla",
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )
    triage_type = pb.claim_type(
        predicate="secops.vuln.triage_decision",
        subject_kinds=("secops.policy", "secops.service"),
        object_kind=ClaimObjectKind.LITERAL,
        value_schema={"type": "object"},
        object_subject_kinds=(),
        cardinality=Cardinality.MANY,
        permitted_roles=(ClaimRole.NORMATIVE, ClaimRole.OBSERVATION),
        referent_sensitivity=ReferentSensitivity.IDENTITY,
        sources=(
            "corpus.escalation-policy",
            "corpus.infra-inventory",
            "corpus.vuln-response-runbook",
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many",
            eligible_verdicts=("supported",),
            selector="all",
        ),
        pins=(),
        evidence_freshness=Duration.days(count=90),
    )
    kev = pb.claim(
        subject=policy_subject.address,
        predicate=triage_type.predicate,
        value={"fact": "kev_listed_deadline", "hours": 48},
        role=ClaimRole.NORMATIVE,
        rationale="The runbook fixes the KEV deadline independently of severity.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor(
            "tightens to forty-eight hours"
        ),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=policy_subject,
        claim_type_definition=triage_type,
    ).prepare()
    assert not kev.refused
    kev.submit()
    kev_proposal = kev.status().proposal_id
    assert kev_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, kev_proposal)
    assert kev.status().state == "accepted"
    pb.refresh()
    kev_identity = _accepted_claims(pb, triage_type.predicate)[0]

    critical = pb.claim(
        subject=SubjectRef(policy_subject.address, pb.coordinate),
        predicate=ClaimTypeRef(triage_type.predicate, pb.coordinate),
        value={"fact": "exposed_critical_deadline", "hours": 72},
        role=ClaimRole.NORMATIVE,
        rationale="The runbook fixes the exposed critical deadline.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("within seventy-two hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={ClaimRef(kev_identity, pb.coordinate): Disposition.SUPPORT},
        subject_definition=None,
        claim_type_definition=None,
    ).prepare()
    assert not critical.refused, critical.diagnostics
    critical.submit()
    critical_proposal = critical.status().proposal_id
    assert critical_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, critical_proposal)
    pb.refresh()

    remaining_facts = (
        (
            "secops.policy/exposure",
            ClaimRole.NORMATIVE,
            {"fact": "similar_version_is_verification", "value": False},
            "corpus/vuln-response-runbook.md",
            "Similarity is not verification",
        ),
        (
            "secops.service/payments-edge",
            ClaimRole.OBSERVATION,
            {
                "nginx": "1.24.0",
                "lua_gateway": "request_path",
                "reachable": "internet",
            },
            "corpus/infra-inventory.md",
            "Runs nginx 1.24.0 with the lua-gateway module in the request path",
        ),
        (
            "secops.service/partner-api",
            ClaimRole.OBSERVATION,
            {"nginx": "1.24.0", "lua_gateway": "disabled", "reachable": "vpn_only"},
            "corpus/infra-inventory.md",
            "The lua-gateway module is installed but disabled",
        ),
        (
            "secops.service/batch-ingest",
            ClaimRole.OBSERVATION,
            {"nginx": "1.22.1", "lua_gateway": "absent", "reachable": "internal"},
            "corpus/infra-inventory.md",
            "Runs nginx 1.22.1 without lua-gateway",
        ),
    )
    for subject_name, role, value, source_path, anchor in remaining_facts:
        subject = pb.subject(
            subject=subject_name,
            pins=(),
            lifecycle=ArtifactLifecycle(),
        )
        intent = pb.claim(
            subject=subject.address,
            predicate=ClaimTypeRef(triage_type.predicate, pb.coordinate),
            value=value,
            role=role,
            rationale="Compile one explicit demo-world fact from its corpus sentence.",
            supported_by=pb.file(source_path).anchor(anchor),
            copied_from=None,
            self_source=None,
            qualifier=None,
            effective_period=None,
            revises=None,
            dispositions={},
            subject_definition=subject,
            claim_type_definition=None,
        ).prepare()
        assert not intent.refused, intent.diagnostics
        intent.submit()
        proposal_id = intent.status().proposal_id
        assert proposal_id is not None
        _approve_and_activate(http, instance_id, private_key_path, proposal_id)
        pb.refresh()

    assert len(_accepted_claims(pb, triage_type.predicate)) == 6
    guidance_subject = pb.subject(
        subject="secops.policy/response-guidance",
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )
    guidance = pb.claim(
        subject=guidance_subject.address,
        predicate=ClaimTypeRef(triage_type.predicate, pb.coordinate),
        value={"deadline_hours": 48, "fact": "governed_emergency_deadline"},
        role=ClaimRole.NORMATIVE,
        rationale="Expose the governed emergency deadline as an ordinary sourced Claim.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("forty-eight hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=guidance_subject,
        claim_type_definition=None,
    ).prepare()
    assert not guidance.refused, guidance.diagnostics
    guidance.submit()
    guidance_proposal = guidance.status().proposal_id
    assert guidance_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, guidance_proposal)
    pb.refresh()

    query = QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name="secops.policy.guidance"),
        description="List policy subjects that carry governed response decisions.",
        entry=QueryEntry(binding="policy", subject_kinds=("secops.policy",)),
        where=QueryClaimPresenceFilter(binding="policy", predicate=triage_type.predicate),
        result_binding="policy",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="policy_id",
                    value=QuerySubjectFieldRef(binding="policy", field="subject_id"),
                ),
            )
        ),
        evaluation_policy=QueryEvaluationPolicy(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        ),
        default_budgets=QueryBudgets(max_results=10, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=50, max_traversal_depth=0),
        pins=(
            ArtifactPin(
                role="claim-type",
                target=triage_type.definition.identity,
                artifact_digest=claim_type_digest(triage_type.definition).tagged,
            ),
        ),
    )
    query_preflight = transport.compile_authoring_input(
        instance_id,
        input=QueryDefinitionInput(
            kind="query_definition",
            query_definition=query,
        ).model_dump(mode="json"),
    )
    assert query_preflight.verdict == "passed", query_preflight.frontier
    query_intent_id = query_preflight.certificate.intent_id
    assert isinstance(query_intent_id, str)
    submitted_query = transport.submit_authoring_intent(
        instance_id,
        query_intent_id,
    )
    query_proposal_id = submitted_query.status.proposal_id
    assert query_proposal_id is not None
    _approve_and_activate(http, instance_id, private_key_path, query_proposal_id)
    pb.refresh()
    queried = pb.query(name=query.identity.name, receipt="full").page.receipt.replay
    assert queried is not None
    assert queried.result.verdict == "completed"
    assert "response-guidance" in {
        field.value
        for row in queried.result.rows
        for field in row.fields
        if field.name == "policy_id"
    }

    procedure = pb.procedure(
        definition=_assess_procedure(query.identity.qualified),
    ).prepare()
    assert not procedure.refused, procedure.diagnostics
    procedure.submit()
    procedure_proposal = procedure.status().proposal_id
    assert procedure_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, procedure_proposal)
    pb.refresh()

    assert pb.get(SubjectRef(policy_subject.address, pb.coordinate)).ref.address == (
        policy_subject.address
    )
    accepted = pb.accepted_procedure("secops.vuln.assess")
    card = pb.get(accepted.ref).value
    assert isinstance(card, GetProcedureCard)
    assert card.runnable == "direct"
    assert card.unsupported_nodes == ()
