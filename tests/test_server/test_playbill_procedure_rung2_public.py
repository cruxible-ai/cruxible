"""Install a real workspace provider, then author/run/accept/read a Line over HTTP.

Only workspace attachment is an operator setup action. All governed definitions,
provider registration, proposal approval and Claim reads use public surfaces;
no provider invoker or classifier is substituted.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_client import Cruxible
from cruxible_client.authoring.inputs import CarriedContractInput, ProcedureInput
from cruxible_client.contracts import ClaimViewRecord
from cruxible_client.contracts.acquisition_policies import (
    IndependentCoherence,
    InputAcquisitionRule,
    SourceAcquisitionPolicy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.capture_reads import CaptureReadRequest
from cruxible_client.contracts.captures import CanonicalDuration, capture_contract_digest
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.procedures.artifacts import procedure_owned_contract_digest
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_client.contracts.procedures.models import (
    CaptureEgressNodeV3,
    ProcedureBudget,
    ProcedureDefinitionV5,
    ProcedureHardCaps,
    ProjectNode,
    ProposeChangeSetNodeV3,
    SourceNode,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, subject_path
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_INTERFACE_V2_DIGEST,
    WorkspaceFileSourceRequest,
)
from cruxible_client.provider_installation import install_provider_package
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import PROVIDER_RUNTIME_CONFIG_PATH
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from tests.core_support._pc_c_support import capture_contract
from tests.test_procedures.test_procedure_source_runs import _contracts
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate
from tests.test_server.test_provider_installation import interface_entry

SUBJECT_KIND = "security.advisory"
SUBJECT_ID = "osv-2026-0001"
PREDICATE = "security.advisory.severity"
PROCEDURE_NAME = "osv-advisory-severity"
POLICY_NAME = "osv-advisory-reads"
LINE_NAME = "osv-advisory-hourly"
MANDATE_NAME = "osv-advisory-mandate"
RELATIVE_PATH = "data/osv-severity.txt"
ADVISORY = b"high"


@pytest.fixture
def installed_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, Path, Path]]:
    """A real installed provider and an operator-attached workspace."""

    repository = os.environ.get("CRUXIBLE_TEST_PROVIDER_REPOSITORY")
    wheels = os.environ.get("CRUXIBLE_TEST_PROVIDER_WHEELS")
    if not repository or not wheels:
        pytest.skip(
            "requires built provider wheels and repository via CRUXIBLE_TEST_PROVIDER_* variables"
        )
    pytest.importorskip("cruxible_provider_runtime")
    state = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    config = state / PROVIDER_RUNTIME_CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "provider_repository": repository,
                "provider_index_urls": [
                    "https://pypi.org/simple",
                    "https://files.pythonhosted.org/",
                ],
            }
        )
    )
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    registered = get_registry().create_governed_instance_with_id("inst_rung2_public")
    instance_id = registered.record.instance_id
    managed = Path(registered.record.location)
    workspace = tmp_path / "workspace"
    (workspace / "data").mkdir(parents=True)
    (workspace / RELATIVE_PATH).write_bytes(ADVISORY)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    # The operator's `cruxible workspace attach`, recorded before init.
    get_registry().attach_governed_workspace(instance_id, workspace)
    owner = generate_client_principal_key(
        tmp_path / "owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    reviewer = generate_client_principal_key(
        tmp_path / "reviewer-custody",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    with TestClient(create_app()) as client:
        initialized = client.post(
            f"/api/v1/{instance_id}/init",
            json={
                "principals": [
                    owner.principal.model_dump(mode="json"),
                    reviewer.principal.model_dump(mode="json"),
                ],
            },
        )
        assert initialized.status_code == 200, initialized.text
        transport = CruxibleClient(base_url="http://cruxible")
        transport._client = client
        installed = install_provider_package(
            transport,
            instance_id,
            wheel=next(Path(wheels).glob("cruxible_provider_workspace-*.whl")),
            lock=Path(repository) / "packages/cruxible-provider-workspace/uv.lock",
            dependency_wheels=(next(Path(wheels).glob("cruxible_provider_runtime-*.whl")),),
        )
        assert installed.status == "ready", installed
        yield client, instance_id, reviewer.private_key_path, workspace
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def _claim_type(contract_digest: str) -> ClaimType:
    return ClaimType(
        artifact_format="playbill-claim-type-v1",
        identity=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
        predicate=PREDICATE,
        allowed_subject_kinds=(SUBJECT_KIND,),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="workspace-record",
                    claim_roles=("observation",),
                    capture_contract_digests=(contract_digest,),
                    evidence_kinds=("database_record",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _policy() -> SourceAcquisitionPolicy:
    return SourceAcquisitionPolicy(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name=POLICY_NAME),
        inputs=(
            InputAcquisitionRule(
                input_name="advisory",
                requirement="required",
                permitted_replayability=("attested_only", "exact"),
                max_age=CanonicalDuration(microseconds=3_600_000_000),
                on_unavailable="refuse",
                on_stale="refuse",
                on_oversized="refuse",
                on_conflict="preserve",
            ),
        ),
        coherence=IndependentCoherence(),
    )


def _item() -> dict[str, object]:
    return {
        "tag": "playbill-procedure-claim-proposal-item-v1",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": SemanticAddress.whole_artifact(
                subject_path(SUBJECT_KIND, SUBJECT_ID)
            ).model_dump(mode="json"),
            "predicate": PREDICATE,
            "qualifier": None,
            "object": {"kind": "literal", "value": "$steps.result.severity"},
            "role": "observation",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": "Severity as read from the accepted advisory document.",
        "revises": None,
    }


def _procedure_definition(
    *,
    interface: dict[str, Any],
    provider: dict[str, Any],
    contract_pin: ArtifactPin,
    workspace: Path,
    terminal_kind: str = "propose_change_set",
) -> ProcedureDefinitionV5:
    input_contract, output_contract = _contracts()
    contract_in = ArtifactPin(
        role="contract-in",
        target=input_contract.identity,
        artifact_digest=procedure_owned_contract_digest(input_contract).tagged,
    )
    contract_out = ArtifactPin(
        role="contract-out",
        target=output_contract.identity,
        artifact_digest=procedure_owned_contract_digest(output_contract).tagged,
    )
    request = WorkspaceFileSourceRequest(
        logical_source="commerce.production.orders",
        relative_path=RELATIVE_PATH,
        workspace_binding_digest=_binding_digest(workspace),
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "workspace"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
        replayability="exact",
    )
    return ProcedureDefinitionV5(
        name=PROCEDURE_NAME,
        description="Read the advisory document and propose its severity as a Claim.",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            SourceNode(
                node_id="read",
                capture_contract=contract_pin,
                provider=ArtifactPin(
                    role="provider",
                    target=ArtifactIdentity.model_validate(
                        {"kind": "Provider", "name": provider["provider_identity"].split(":", 1)[1]}
                    ),
                    artifact_digest=provider["provider_artifact_digest"],
                ),
                interface=ArtifactPin(
                    role="provider-interface",
                    target=ArtifactIdentity.model_validate(
                        {
                            "kind": "ProviderInterface",
                            "name": interface["identity"].split(":", 1)[1],
                        }
                    ),
                    artifact_digest=interface["artifact_digest"],
                ),
                interface_digest=interface["interface_digest"],
                implementation_digest=provider["implementation_digest"],
                request=request.model_dump(mode="json"),
                as_="advisory",
                next="shape",
            ),
            ProjectNode(
                node_id="shape",
                fields={"severity": "$steps.advisory.content.text"},
                contract_out=contract_out,
                as_="result",
                next="propose",
            ),
            (
                CaptureEgressNodeV3(
                    node_id="propose", capture_contract=contract_pin, input="$steps.result"
                )
                if terminal_kind == "emit_capture"
                else ProposeChangeSetNodeV3(node_id="propose", candidate_templates=(_item(),))
            ),
        ),
        returns="result",
        budget=ProcedureBudget(
            wall_clock=CanonicalDuration(microseconds=5_000_000),
            max_provider_calls=2,
            max_capture_bytes=65_536,
            max_items=None,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=CanonicalDuration(microseconds=10_000_000),
            max_provider_calls=4,
            max_capture_bytes=131_072,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )


def _authored(definition: ProcedureDefinitionV5, *, same_set_kinds: set[str]) -> dict[str, object]:
    """Render the graph the way an author names it: references, never caller digests.

    Owned Contracts are carried by name; artifacts accepted at the base are
    referenced for resolution there; artifacts defined in this same change set
    are referenced as candidates in it.
    """

    def rewrite(value: object) -> object:
        if isinstance(value, dict):
            if set(value) == {"role", "target", "artifact_digest"}:
                target = value["target"]
                if target["kind"] == "Contract":
                    return {
                        "kind": "carried_contract",
                        "name": target["name"],
                        "role": value["role"],
                    }
                if target["kind"] in same_set_kinds:
                    return {
                        "tag": "playbill-authoring-candidate-reference-v1",
                        "role": value["role"],
                        "target": target,
                        "resolution": "candidate_in_change_set",
                    }
                return {
                    "tag": "playbill-authoring-artifact-reference-v1",
                    "role": value["role"],
                    "target": target,
                    "resolution": "accepted_at_intent_base",
                }
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    return rewrite(definition.model_dump(mode="json", by_alias=True))  # type: ignore[return-value]


def _binding_digest(workspace: Path) -> str:
    from cruxible_core.documents.workspace_file import workspace_binding_digest

    return workspace_binding_digest(
        instance_id="inst_rung2_public",
        canonical_root=workspace.resolve(),
    )


def _proposal_proof(transport: CruxibleClient, instance_id: str, proposal_id: str) -> Any:
    """get is the one proposal read: its proof carries the evaluation and candidate."""

    return transport.get(instance_id, request=GetRequest(ref=proposal_id, detail="proof")).proof


def _claim_proof(
    transport: CruxibleClient,
    instance_id: str,
    claim_id: str,
    *,
    evaluation_time: datetime | None = None,
) -> ClaimViewRecord:
    proof = transport.get(
        instance_id,
        request=GetRequest(ref=claim_id, detail="proof", evaluation_time=evaluation_time),
    ).proof
    return ClaimViewRecord.model_validate(proof)


@pytest.mark.parametrize("terminal_kind", ["propose_change_set", "emit_capture"])
def test_the_rung2_loop_runs_over_public_surfaces_only(
    installed_host: tuple[TestClient, str, Path, Path],
    terminal_kind: str,
) -> None:
    http, instance_id, reviewer_key, workspace = installed_host
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]

    # 1. Discover the installed interface and the live Provider implementing it.
    interface = interface_entry(transport, instance_id, "workspace.file")
    assert interface["identity"] == "ProviderInterface:workspace.file"
    assert interface["interface_digest"] == WORKSPACE_FILE_INTERFACE_V2_DIGEST
    assert interface["providers"], interface
    provider = interface["providers"][0]

    # 2. Author definitions through public surfaces, including the typed SDK for capture.
    contract = capture_contract()
    contract_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    definition = _procedure_definition(
        interface=interface,
        provider=provider,
        contract_pin=contract_pin,
        workspace=workspace,
        terminal_kind=terminal_kind,
    )
    input_contract, output_contract = _contracts()
    members: list[dict[str, Any]] = [
        {
            "tag": "playbill-capture-contract-authoring-payload-v1",
            "capture_contract": contract.model_dump(mode="json"),
        },
        {
            "tag": "playbill-source-acquisition-policy-authoring-payload-v1",
            "acquisition_policy": _policy().model_dump(mode="json"),
        },
        {
            "tag": "playbill-claim-type-authoring-payload-v1",
            "claim_type": _claim_type(contract_pin.artifact_digest).model_dump(mode="json"),
        },
        {
            "tag": "playbill-subject-authoring-payload-v1",
            "subject": SubjectShell(
                identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/{SUBJECT_ID}"),
                subject_kind=SUBJECT_KIND,
                subject_id=SUBJECT_ID,
            ).model_dump(mode="json"),
        },
        {
            "tag": "playbill-procedure-authoring-payload-v2",
            "definition": _authored(definition, same_set_kinds={"CaptureContract"}),
            "activation_policy": "drain",
            "owned_contracts": [
                item.model_dump(mode="json", by_alias=True)
                for item in (input_contract, output_contract)
            ],
            "acquisition_policy": POLICY_NAME,
            "retire": False,
        },
        {
            "tag": "playbill-line-authoring-payload-v1",
            "name": LINE_NAME,
            "procedure_name": PROCEDURE_NAME,
            "acquisition_policy_name": POLICY_NAME,
            "requested_terminal_rung": 2,
        },
        {
            "tag": "playbill-procedure-mandate-authoring-payload-v1",
            "name": MANDATE_NAME,
            "procedure_name": PROCEDURE_NAME,
            "rung": 2,
            "authority_ceiling": definition.hard_caps.model_dump(mode="json"),
            "namespace": ["claims"],
            "valid_from": "2026-01-01T00:00:00Z",
            "expires_at": "2027-01-01T00:00:00Z",
        },
    ]
    from pydantic import TypeAdapter

    from cruxible_client.contracts.authoring.models import (
        AuthoringChangeSetMember,
        authoring_member_identity,
    )

    adapter = TypeAdapter(AuthoringChangeSetMember)
    members.sort(
        key=lambda item: authoring_member_identity(adapter.validate_python(item)).encode("utf-8")
    )

    def accept_members(selected: list[dict[str, Any]]) -> None:
        compiled = transport.compile_authoring(
            instance_id,
            payload={
                "tag": "playbill-change-set-authoring-payload-v1",
                "members": selected,
                "rationale": "Stand up the advisory severity Line.",
            },
        )
        assert compiled.verdict == "passed", compiled.frontier
        intent_id = str(compiled.certificate.intent_id)
        submitted = transport.submit_authoring_intent(instance_id, intent_id)
        assert submitted.status.proposal_id is not None, submitted
        _approve_and_activate(http, instance_id, reviewer_key, submitted.status.proposal_id)

    if terminal_kind == "emit_capture":
        # Accept dependencies first, then exercise the SDK authoring door itself
        # instead of bypassing its supported-node checks through raw payloads.
        line_tags = {
            "playbill-line-authoring-payload-v1",
            "playbill-procedure-mandate-authoring-payload-v1",
        }
        accept_members(
            [
                member
                for member in members
                if member["tag"] not in line_tags | {"playbill-procedure-authoring-payload-v2"}
            ]
        )
        pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)
        authored = ProcedureInput(
            kind="procedure",
            definition=_authored(definition, same_set_kinds=set()),
            activation_policy="drain",
            acquisition_policy=POLICY_NAME,
            contracts=tuple(
                CarriedContractInput(
                    name=contract.identity.name,
                    description=contract.contract_schema.description,
                    fields=contract.contract_schema.fields,
                    allow_extra=contract.contract_schema.allow_extra,
                )
                for contract in (input_contract, output_contract)
            ),
        )
        prepared = pb.procedure(definition=authored).prepare()
        assert not prepared.refused, prepared.diagnostics
        submitted_procedure = prepared.submit().status()
        assert submitted_procedure.proposal_id is not None
        _approve_and_activate(http, instance_id, reviewer_key, submitted_procedure.proposal_id)
        accept_members([member for member in members if member["tag"] in line_tags])
    else:
        accept_members(members)

    # 3. Trigger the Line through its public route.
    identity_digest = line_identity_digest(ArtifactIdentity(kind="Line", name=LINE_NAME))
    state = transport.run_line(instance_id, identity_digest, occurrence_id=None)
    assert state.status == "succeeded", state.model_dump_json(indent=2)
    assert state.run_id is not None
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered"
    if terminal_kind == "emit_capture":
        assert egress.proposal_id is None
        (emitted_event,) = tuple(
            item
            for item in state.outcomes
            if item["event_kind"] == "produced_capture" and item["node_id"] == "propose"
        )
        assert emitted_event["capture_event"] is not None
        assert emitted_event["capture_event"]["run_id"] == state.run_id
        assert len(egress.children) == 1
        capture_digest = egress.children[0].egress_digest
        pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)
        captured = pb.capture(capture_digest)
        assert captured.result.status == "verified", captured.result
        assert captured.json() == {"severity": "high"}
        assert captured.result.envelope.run_coordinate.run_id == state.run_id
        assert captured.result.envelope.producer.kind == "Procedure"
        limited = pb.capture(capture_digest, max_bytes=1)
        assert limited.result.material.status == "unavailable"
        assert "resource_budget_exceeded" in limited.result.material.coverage.reason_codes
        with pytest.raises(ValueError, match="content is unavailable"):
            _ = limited.content
        # The Source's retained evidence is independently readable; neither read refetches it.
        source = pb.capture(state.source_observations[0].capture_digest)
        assert source.result.envelope.producer.kind == "Provider"
        assert source.json()["content"]["text"] == "high"
        claim = pb.claim(
            subject=f"{SUBJECT_KIND}/{SUBJECT_ID}",
            predicate=PREDICATE,
            value="high",
            role="observation",
            rationale="Read from the Procedure's retained output.",
            supported_by=captured.ref,
        ).prepare()
        assert not claim.refused, claim.diagnostics
        submitted_claim = claim.submit()
        status = submitted_claim.status()
        assert status.proposal_id is not None, status
        _approve_and_activate(http, instance_id, reviewer_key, status.proposal_id)
        proof = _proposal_proof(transport, instance_id, status.proposal_id)
        member = next(
            item for item in proof["candidate"]["members"] if item["path"].startswith("claims/")
        )
        claim_id = member["path"].rsplit("/", 1)[1].removesuffix(".json")
        view = _claim_proof(transport, instance_id, claim_id)
        assert view.admission_accounts[0].capture_digest == capture_digest
        assert view.admission_accounts[0].status == "admitted"
        missing = transport.read_capture(
            instance_id, CaptureReadRequest(capture_digest="sha256:" + "f" * 64)
        )
        assert missing.status == "unavailable"
        get_playbill_manager().clear()
        assert pb.capture(capture_digest).json() == {"severity": "high"}
        again = transport.get_procedure_run(instance_id, state.run_id)
        assert again.outcomes == state.outcomes
        assert again.terminal_egress == state.terminal_egress
        return
    assert egress.proposal_id is not None and egress.candidate_digest is not None
    (child,) = egress.children
    assert child.path is not None and child.path.startswith("claims/")
    (observation,) = state.source_observations
    assert observation.capture_digest is not None
    assert observation.source_read_receipt is not None
    assert observation.source_read_receipt.relative_path == RELATIVE_PATH

    # 4. The manager retrieves the exact candidate, reviews it, and activates it.
    proof = _proposal_proof(transport, instance_id, egress.proposal_id)
    candidate = proof["candidate"]
    assert candidate is not None, proof["evaluation"]["diagnostics"]
    assert candidate["candidate_digest"] == egress.candidate_digest
    member = next(item for item in candidate["members"] if item["path"] == child.path)
    assert member["candidate_artifact_digest"] == child.egress_digest
    evidence = next(item for item in candidate["law_evidence"] if item["path"] == child.path)
    (verdict_capture,) = evidence["result"]["claim_evidence"]["verdict_captures"]
    assert verdict_capture["capture_digest"] == observation.capture_digest
    assert verdict_capture["provenance_grade"] == "daemon-fetched"
    assert verdict_capture["epistemic_grade"] == "observed"
    _approve_and_activate(http, instance_id, reviewer_key, egress.proposal_id)

    # 5. The accepted Claim reads back through the Claim route to its Capture.
    claim_id = child.path.rsplit("/", 1)[1].removesuffix(".json")
    view = _claim_proof(
        transport,
        instance_id,
        claim_id,
        evaluation_time=observation.source_read_receipt.read_at,
    )
    assert view.statement.predicate == PREDICATE
    assert view.statement.object.model_dump(mode="json")["value"] == "high"
    # The served admission account binds the accepted Claim to the run's Capture.
    (account,) = view.admission_accounts
    assert account.capture_digest == observation.capture_digest
    assert account.citation_role == "evidence"
    assert account.citation_origin == "independent"
    assert account.status == "admitted"
    # And the run reads back the same receipt after acceptance.
    again = transport.get_procedure_run(instance_id, state.run_id)
    assert again.terminal_egress == state.terminal_egress
