"""S1 acceptance: demo-world beat 1 through the public SDK and HTTP daemon."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client import (
    ActivationPolicy,
    Audience,
    BriefClaimExpectation,
    BriefKind,
    Cardinality,
    ClaimObjectKind,
    ClaimRef,
    ClaimRole,
    ClaimTypeRef,
    Playbill,
    ReferentSensitivity,
    SubjectRef,
)
from cruxible_client.contracts.artifacts import ArtifactAuthority, ArtifactLifecycle
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProjectNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner

AUTHORITY = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-sdk-demo-v1", {"label": label}).tagged


def _catalog(workspace: Path) -> None:
    (workspace / ".playbill").mkdir()
    (workspace / "corpus").mkdir()
    (workspace / ".playbill" / "sources.yaml").write_text(
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
        "Emergency changes require one platform-lead approver.\n", encoding="utf-8"
    )
    (workspace / "corpus" / "infra-inventory.md").write_text(
        "payments-edge runs lua-gateway in the request path and is internet-reachable.\n",
        encoding="utf-8",
    )
    (workspace / "corpus" / "vuln-response-runbook.md").write_text(
        "The KEV patch deadline tightens to forty-eight hours.\n"
        "Critical internet-facing systems must patch within seventy-two hours.\n",
        encoding="utf-8",
    )


def _approve_and_activate(
    client: TestClient,
    instance_id: str,
    private_key_path: Path,
    proposal_id: str,
) -> None:
    challenge_response = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "operator"},
    )
    assert challenge_response.status_code == 200, challenge_response.text
    challenge = challenge_response.json()
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="operator",
        private_key_path=private_key_path,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    approved = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    activated = client.post(f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "accepted"


def _abstract_assess_procedure() -> ProcedureDefinitionV3:
    contract_in = ProcedurePinSlotRefV1(slot_name="contract-in")
    contract_out = ProcedurePinSlotRefV1(slot_name="contract-out")
    query = ProcedurePinSlotRefV1(slot_name="policy-query")
    return ProcedureDefinitionV3(
        name="secops.vuln.assess",
        description="Classify a vulnerability from governed policy and service facts.",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read-policy",
                query=query,
                parameters={},
                as_="policy_rows",
                next="classify",
            ),
            TransformNodeV3(
                node_id="classify",
                transform_kind="adapter",
                contract_in=contract_in,
                contract_out=contract_out,
                spec={"input": "$steps.policy_rows"},
                as_="decision",
                next="result",
            ),
            ProjectNodeV3(
                node_id="result",
                fields={"lane": "$steps.decision.lane"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        pin_slots=(
            ProcedurePinSlotV1(
                slot_name="contract-in",
                pin_role="contract-in",
                artifact_kind="Contract",
                interface_digest=_digest("contract-in"),
            ),
            ProcedurePinSlotV1(
                slot_name="contract-out",
                pin_role="contract-out",
                artifact_kind="Contract",
                interface_digest=_digest("contract-out"),
            ),
            ProcedurePinSlotV1(
                slot_name="policy-query",
                pin_role="query",
                artifact_kind="QueryDefinition",
                interface_digest=_digest("policy-query"),
            ),
        ),
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=1_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )


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
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=workspace)

    policy_subject = pb.subject(
        subject="secops.policy/patch-sla",
        authority=AUTHORITY,
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )
    patch_sla = pb.claim_type(
        predicate="secops.policy.patch_sla",
        subject_kinds=("secops.policy",),
        object_kind=ClaimObjectKind.LITERAL,
        value_schema={"type": "object"},
        object_subject_kinds=(),
        cardinality=Cardinality.ONE,
        permitted_roles=(ClaimRole.NORMATIVE,),
        referent_sensitivity=ReferentSensitivity.IDENTITY,
        sources=(
            "corpus.escalation-policy",
            "corpus.infra-inventory",
            "corpus.vuln-response-runbook",
        ),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        authority=AUTHORITY,
        pins=(),
        slot_policy=None,
        evidence_freshness=None,
    )
    kev = pb.claim(
        subject=policy_subject.address,
        predicate=patch_sla.predicate,
        value={"deadline_hours": 48, "trigger": "kev_listed"},
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
        publish_to=None,
        subject_definition=policy_subject,
        claim_type_definition=patch_sla,
    ).prepare()
    assert not kev.refused
    kev.submit()
    kev_proposal = kev.status().proposal_id
    assert kev_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, kev_proposal)
    assert kev.status().state == "accepted"
    pb.refresh()

    critical_subject = pb.subject(
        subject="secops.policy/critical-patch-sla",
        authority=AUTHORITY,
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )
    critical = pb.claim(
        subject=critical_subject.address,
        predicate=ClaimTypeRef(patch_sla.predicate, pb.coordinate),
        value={"deadline_hours": 72, "trigger": "internet_facing_critical"},
        role=ClaimRole.NORMATIVE,
        rationale="The runbook fixes the exposed critical deadline.",
        supported_by=pb.file("corpus/vuln-response-runbook.md").anchor("within seventy-two hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=critical_subject,
        claim_type_definition=None,
    ).prepare()
    assert not critical.refused, critical.diagnostics
    critical.submit()
    critical_proposal = critical.status().proposal_id
    assert critical_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, critical_proposal)
    pb.refresh()

    claim_rows = pb.list(kinds=("claim",), statuses=("accepted",)).rows
    kev_identity = next(
        str(row["identity"])
        for row in claim_rows
        if row.get("predicate") == patch_sla.predicate
        and row.get("subject", {}).get("artifact_path") == "subjects/secops.policy/patch-sla.yaml"
    ).removeprefix("Claim:")
    brief = pb.brief(
        subject=SubjectRef(policy_subject.address, pb.coordinate),
        purpose="Summarize the governed vulnerability response deadlines.",
        kind=BriefKind.GUIDANCE,
        prose="KEV findings use the governed emergency deadline.",
        rationale="Give agents a concise governed entry point to the policy.",
        audience=Audience.AGENT,
        claims={
            ClaimRef(kev_identity, pb.coordinate): BriefClaimExpectation(
                subject=SubjectRef(policy_subject.address, pb.coordinate),
                claim_type=ClaimTypeRef(patch_sla.predicate, pb.coordinate),
            )
        },
        queries={},
        revises=None,
        dispositions={},
    ).prepare()
    assert not brief.refused, brief.diagnostics
    brief.submit()
    brief_proposal = brief.status().proposal_id
    assert brief_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, brief_proposal)
    pb.refresh()

    publication_subject = pb.subject(
        subject="secops.policy/published-guidance",
        authority=AUTHORITY,
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )
    published_text = "\nGoverned guidance: retain the seventy-two-hour boundary.\n"
    publication_intent = pb.claim(
        subject=publication_subject.address,
        predicate=ClaimTypeRef(patch_sla.predicate, pb.coordinate),
        value={"deadline_hours": 72, "trigger": "published_guidance"},
        role=ClaimRole.NORMATIVE,
        rationale="Publish the accepted guidance back into its declared source.",
        supported_by=None,
        copied_from=None,
        self_source=published_text,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=pb.file("corpus/vuln-response-runbook.md").append(),
        subject_definition=publication_subject,
        claim_type_definition=None,
    ).prepare()
    assert not publication_intent.refused, publication_intent.diagnostics
    publication_intent.submit()
    publication_proposal = publication_intent.status().proposal_id
    assert publication_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, publication_proposal)
    publication = publication_intent.publication
    assert publication is not None
    publication_path = workspace / "corpus" / "vuln-response-runbook.md"
    publication_path.chmod(0o640)
    publication.apply()
    assert published_text in publication_path.read_text(encoding="utf-8")
    assert publication_path.stat().st_mode & 0o777 == 0o640
    assert publication.state in {"confirming", "bound"}
    pb.refresh()

    procedure = pb.procedure(
        definition=_abstract_assess_procedure(),
        authority=AUTHORITY,
        activation_policy=ActivationPolicy.DRAIN,
        retire=False,
    ).prepare()
    assert not procedure.refused
    procedure.submit()
    procedure_proposal = procedure.status().proposal_id
    assert procedure_proposal is not None
    _approve_and_activate(http, instance_id, private_key_path, procedure_proposal)
    pb.refresh()

    assert pb.get(SubjectRef(policy_subject.address, pb.coordinate)).ref.address == (
        policy_subject.address
    )
    accepted = pb.accepted_procedure("secops.vuln.assess")
    assert accepted.readiness().state == "binding_required"
    assert accepted.readiness().required_slots == [
        "contract-in",
        "contract-out",
        "policy-query",
    ]
