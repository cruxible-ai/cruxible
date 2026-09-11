"""The rung-2 loop through public surfaces only: author, run, review, accept, read.

Nothing here reaches into the daemon's managed root to seed a world. Every
governed artifact the loop needs -- the CaptureContract, the acquisition
policy, the ClaimType, the Subject, the graph-v4 Source Procedure with its
`propose_change_set` terminal, the Line, and the ProcedureMandate -- is authored
as one change set over the HTTP authoring intent routes, reviewed and
activated through the proposal routes, triggered through the Line run route,
and read back through the proposal and Claim routes. The one operator action
is attaching the workspace root before init, which is the daemon operator's
`playbill workspace attach`.

The Provider pins the Source node carries come from the served interface
inventory, which now names the live Providers implementing each interface.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_client.contracts.acquisition_policies import (
    IndependentCoherenceV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import CanonicalDurationV1, capture_contract_digest
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.procedures.artifacts import procedure_owned_contract_digest
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV4,
    ProcedureHardCapsV3,
    ProjectNodeV3,
    ProposeChangeSetNodeV3,
    SourceNodeV4,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, subject_path
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_INTERFACE_DIGEST,
    WorkspaceFileSourceRequestV1,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.governance.seed_artifacts.workspace_file import WORKSPACE_FILE_INTERFACE_ID
from cruxible_core.providers.provider_classifiers import (
    install_compiler_owned_provider_classifier,
)
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from tests.core_support._pc_c_support import capture_contract
from tests.support.provider_seed import workspace_provider_checkout, write_workspace_seed_config
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_source_runs import _contracts
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

SUBJECT_KIND = "security.advisory"
SUBJECT_ID = "osv-2026-0001"
PREDICATE = "security.advisory.severity"
PROCEDURE_NAME = "osv-advisory-severity"
POLICY_NAME = "osv-advisory-reads"
LINE_NAME = "osv-advisory-hourly"
MANDATE_NAME = "osv-advisory-mandate"
RELATIVE_PATH = "data/osv-advisory.json"
ADVISORY = {"advisory_id": "OSV-2026-0001", "severity": "high"}


@pytest.fixture
def attached_seeded_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, Path, Path]]:
    """A seeded host whose operator attached a workspace before init."""

    workspace_provider_checkout()
    state = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    write_workspace_seed_config(state)
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
    (workspace / RELATIVE_PATH).write_bytes(canonical_bytes(ADVISORY))
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    # The operator's `playbill workspace attach`, recorded before init.
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
            f"/api/v1/{instance_id}/playbill/init",
            json={
                "principals": [
                    owner.principal.model_dump(mode="json"),
                    reviewer.principal.model_dump(mode="json"),
                ],
                "seed": True,
            },
        )
        assert initialized.status_code == 200, initialized.text
        yield client, instance_id, reviewer.private_key_path, workspace
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def _claim_type(contract_digest: str) -> ClaimType:
    return ClaimType(
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
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _policy() -> SourceAcquisitionPolicyV1:
    return SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name=POLICY_NAME),
        inputs=(
            InputAcquisitionRuleV1(
                input_name="advisory",
                requirement="required",
                permitted_replayability=("attested_only", "exact"),
                max_age=CanonicalDurationV1(microseconds=3_600_000_000),
                on_unavailable="refuse",
                on_stale="refuse",
                on_oversized="refuse",
                on_conflict="preserve",
            ),
        ),
        coherence=IndependentCoherenceV1(),
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
) -> ProcedureDefinitionV4:
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
    request = WorkspaceFileSourceRequestV1(
        logical_source="commerce.production.orders",
        relative_path=RELATIVE_PATH,
        workspace_binding_digest=_binding_digest(workspace),
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "workspace"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
        replayability="exact",
    )
    return ProcedureDefinitionV4(
        name=PROCEDURE_NAME,
        description="Read the advisory document and propose its severity as a Claim.",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            SourceNodeV4(
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
                interface_digest=WORKSPACE_FILE_INTERFACE_DIGEST,
                implementation_digest=provider["implementation_digest"],
                request=request.model_dump(mode="json"),
                as_="advisory",
                next="shape",
            ),
            ProjectNodeV3(
                node_id="shape",
                fields={"severity": "$steps.advisory.content.json.severity"},
                contract_out=contract_out,
                as_="result",
                next="propose",
            ),
            ProposeChangeSetNodeV3(node_id="propose", candidate_templates=(_item(),)),
        ),
        returns="result",
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=5_000_000),
            max_provider_calls=2,
            max_capture_bytes=65_536,
            max_items=None,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=10_000_000),
            max_provider_calls=4,
            max_capture_bytes=131_072,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )


def _authored(definition: ProcedureDefinitionV4, *, same_set_kinds: set[str]) -> dict[str, object]:
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


class _StubProviderLane:
    """The daemon's Provider lane with the adapter subprocess stood in for.

    Everything the operator answers -- lane status, workspace roots, config --
    is the real operator's. Only the two calls that would spawn the
    `workspace.file` adapter subprocess are answered by the byte-faithful stub
    the Source tests use, because the test host has no materialized adapter
    environment to spawn. The bytes the stub returns are exactly the bytes the
    daemon read; nothing about the governed world is seeded here.
    """

    def __init__(self, real: object) -> None:
        self._real = real
        self._invoker = fixtures._WorkspaceInvoker()
        self._stub = fixtures._Operator(self._invoker)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)

    def invoker_for(self, instance: object, *, accepted_oid: str) -> object:
        return self._stub.invoker_for(instance, accepted_oid=accepted_oid)

    def admit_line_provider(
        self,
        accepted_provider,
        accepted_interface,
        implementation_digest,
        *,
        eligible_environment_pin_keys,
    ):  # type: ignore[no-untyped-def]
        install_compiler_owned_provider_classifier(accepted_interface)
        return self._stub.admit_line_provider(
            accepted_provider,
            accepted_interface,
            implementation_digest,
            eligible_environment_pin_keys=eligible_environment_pin_keys,
        )


def test_the_rung2_loop_runs_over_public_surfaces_only(
    attached_seeded_host: tuple[TestClient, str, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, instance_id, reviewer_key, workspace = attached_seeded_host
    manager = get_playbill_manager()
    lane = _StubProviderLane(manager.provider_runtime_operator())
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: lane)
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]

    # 1. Discover the seeded interface and the live Provider implementing it.
    inventory = transport.discover_playbill(instance_id, profile="interfaces")
    assert inventory.tag == "playbill-interface-inventory-v1"
    interface = next(
        item
        for item in inventory.interfaces
        if item.identity == f"ProviderInterface:{WORKSPACE_FILE_INTERFACE_ID}"
    )
    assert interface.providers, interface
    provider = interface.providers[0]

    # 2. Author the whole world as one change set over the authoring routes.
    contract = capture_contract()
    contract_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    definition = _procedure_definition(
        interface=interface.model_dump(mode="json"),
        provider=provider.model_dump(mode="json"),
        contract_pin=contract_pin,
        workspace=workspace,
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
            "trigger_policy": {"tag": "playbill-manual-trigger-v1", "kind": "manual"},
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
        AuthoringChangeSetMemberV1,
        authoring_member_identity,
    )

    adapter = TypeAdapter(AuthoringChangeSetMemberV1)
    members.sort(
        key=lambda item: authoring_member_identity(adapter.validate_python(item)).encode("utf-8")
    )
    compiled = transport.compile_playbill_authoring(
        instance_id,
        payload={
            "tag": "playbill-change-set-authoring-payload-v1",
            "members": members,
            "rationale": "Stand up the advisory severity Line.",
        },
    )
    assert compiled.verdict == "passed", compiled.frontier
    intent_id = str(compiled.certificate["intent_id"])
    submitted = transport.submit_playbill_authoring_intent(instance_id, intent_id)
    assert submitted.status.proposal_id is not None, submitted
    _approve_and_activate(http, instance_id, reviewer_key, submitted.status.proposal_id)

    # 3. Trigger the Line through its public route.
    identity_digest = line_identity_digest(ArtifactIdentity(kind="Line", name=LINE_NAME))
    state = transport.run_playbill_line(instance_id, identity_digest, occurrence_id=None)
    assert state.status == "succeeded", state.terminal
    assert state.run_id is not None
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered"
    assert egress.proposal_id is not None and egress.candidate_digest is not None
    (child,) = egress.children
    assert child.path is not None and child.path.startswith("claims/")
    (observation,) = state.source_observations
    assert observation.capture_digest is not None

    # 4. The manager retrieves the exact candidate, reviews it, and activates it.
    inspection = transport.inspect_playbill_proposal(instance_id, egress.proposal_id)
    candidate = inspection.proposal["candidate"]
    assert candidate is not None, inspection.proposal["evaluation"]["diagnostics"]
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
    view = transport.get_playbill_claim(
        instance_id,
        claim_id,
        evaluation_time=datetime(2026, 9, 13, tzinfo=UTC).isoformat(),
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
    again = transport.get_playbill_procedure_run(instance_id, state.run_id)
    assert again.terminal_egress == state.terminal_egress
