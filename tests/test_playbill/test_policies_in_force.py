"""Governed policy inventory is exact, live, coordinate-pinned state."""

from __future__ import annotations

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicyV1,
    ClaimAttestationConsequenceRuleV1,
    ClaimEvidenceFreshnessV1,
    ClaimFreshnessDurationV1,
    render_claim_type,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_path,
    render_document,
)
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.service.playbill_policies import list_playbill_policies_in_force
from tests.test_playbill._adoption_fixture import _claim_type, _claim_type_path, _query_definition
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_acquisition_policies import _policy, _rule
from tests.test_playbill.test_line_specs import _line
from tests.test_playbill.test_procedure_artifacts import _artifact, _definition
from tests.test_playbill.test_resolution_contracts import _accept_tree


def test_policies_in_force_lists_live_standalone_and_embedded_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(tmp_path)
    claim_type = _claim_type(0)
    query = _query_definition(0, claim_type)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name)] = (
        render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
    )
    tree[_claim_type_path(claim_type)] = render_claim_type(claim_type)
    tree[query_definition_path(query.identity.name)] = render_query_definition(query)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-08-30T20:00:00.000000Z",
        proposal_name="seed-policy-carriers",
    )

    result = list_playbill_policies_in_force(instance)

    assert result.coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert [row.policy_kind for row in result.policies] == [
        "approval_policy",
        "capture_retention_erasure_policy",
        "claim_admission_policy",
        "claim_evidence_admission_policy",
        "claim_resolution_policy",
        "procedure_runtime_policy",
        "query_evaluation_policy",
    ]
    assert result.policies[0].placement == "standalone"
    assert result.policies[0].field_path == "/"
    assert result.policies[0].policy["tag"] == "playbill-approval-policy-v1"
    assert all(row.declaring_artifact_digest.startswith("sha256:") for row in result.policies)
    assert [
        (row.declaring_artifact_identity, row.field_path, row.policy_kind)
        for row in result.policies
    ] == sorted(
        (
            row.declaring_artifact_identity,
            row.field_path,
            row.policy_kind,
        )
        for row in result.policies
    )

    genesis = instance.accepted_history()[0]
    historical = list_playbill_policies_in_force(
        instance,
        at=contracts.PlaybillAcceptedCoordinate(
            git_oid=genesis.oid,
            semantic_root=genesis.semantic_root.tagged,
            generation_root=genesis.generation_root.tagged,
            compiler_digest=instance.accepted_coordinate().compiler.rule_digest,
        ),
    )
    assert [row.policy_kind for row in historical.policies] == [
        "approval_policy",
        "procedure_runtime_policy",
    ]


@pytest.fixture(scope="module")
def complete_policy_inventory(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[tuple[contracts.PlaybillPolicyInForce, ...], dict[str, tuple[str, str | None]]]:
    root = tmp_path_factory.mktemp("complete-policy-inventory")
    instance, _owner = initialize_local(root)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)

    live_acquisition = _policy(_rule("live-input"))
    retired_acquisition = _policy(_rule("retired-input")).model_copy(
        update={
            "identity": ArtifactIdentity(
                kind="SourceAcquisitionPolicy",
                name="retired-order-release",
            ),
            "lifecycle": ArtifactLifecycle(state="retired"),
        }
    )
    for policy in (live_acquisition, retired_acquisition):
        tree[acquisition_policy_path(policy.identity.name)] = render_acquisition_policy(policy)

    def governed_claim_type(index: int, *, retired: bool):  # type: ignore[no-untyped-def]
        return _claim_type(index).model_copy(
            update={
                "artifact_format": "playbill-claim-type-v4",
                "evidence_freshness": ClaimEvidenceFreshnessV1(
                    stale_after=ClaimFreshnessDurationV1(microseconds=60_000_000)
                ),
                "attestation_consequence_policy": ClaimAttestationConsequencePolicyV1(
                    rules=(
                        ClaimAttestationConsequenceRuleV1(
                            rule_id="one-contradiction",
                            stance="contradict",
                            minimum_independent_control_components=1,
                        ),
                    )
                ),
                "lifecycle": ArtifactLifecycle(state="retired" if retired else "live"),
            }
        )

    live_claim_type = governed_claim_type(0, retired=False)
    retired_claim_type = governed_claim_type(1, retired=True)
    for claim_type in (live_claim_type, retired_claim_type):
        tree[_claim_type_path(claim_type)] = render_claim_type(claim_type)

    live_capture = DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.model_copy(
        update={"identity": ArtifactIdentity(kind="CaptureContract", name="policy-live-capture")}
    )
    retired_capture = DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.model_copy(
        update={
            "identity": ArtifactIdentity(
                kind="CaptureContract",
                name="policy-retired-capture",
            ),
            "lifecycle": ArtifactLifecycle(state="retired"),
        }
    )
    for capture in (live_capture, retired_capture):
        tree[capture_contract_path(capture.identity.name)] = render_capture_contract(capture)

    live_query = _query_definition(0, live_claim_type)
    retired_query = _query_definition(1, retired_claim_type).model_copy(
        update={"lifecycle": ArtifactLifecycle(state="retired")}
    )
    for query in (live_query, retired_query):
        tree[query_definition_path(query.identity.name)] = render_query_definition(query)

    document = DocumentShell(
        identity="document:policy-carrier",
        document_kind="reference",
        title="Policy carrier",
        media_type="text/plain",
        body_digest="sha256:" + "d" * 64,
        authority=DocumentAuthority(required_tier="governed_write"),
        governance_scope=("project:policy",),
        lifecycle=DocumentLifecycle(revision=1, activation_policy="snapshot"),
    )
    tree[document_path(document.document_id)] = render_document(document)

    live_procedure = _artifact(_definition())
    retired_definition = live_procedure.definition.model_copy(
        update={"name": "retired-policy-procedure"}
    )
    retired_procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=retired_definition.name),
        definition=retired_definition,
        definition_digest=compute_procedure_definition_digest_v3(retired_definition).tagged,
        pins=live_procedure.pins,
        activation_policy=live_procedure.activation_policy,
        lifecycle=ArtifactLifecycle(state="retired"),
    )
    for procedure in (live_procedure, retired_procedure):
        tree[procedure_path(procedure.identity.name)] = render_procedure(procedure)

    live_line, _accepted, _interfaces = _line()
    retired_line = live_line.model_copy(
        update={
            "identity": ArtifactIdentity(kind="Line", name="retired-policy-line"),
            "lifecycle": ArtifactLifecycle(state="retired"),
        }
    )
    for line in (live_line, retired_line):
        tree[line_spec_path(line.identity.name)] = render_line_spec(line)

    instance.tree_at = lambda _oid: dict(tree)  # type: ignore[method-assign]
    rows = tuple(list_playbill_policies_in_force(instance).policies)
    expected = {
        "approval_policy": ("ApprovalPolicy:instance", None),
        "procedure_runtime_policy": ("ProcedureRuntimePolicy:instance", None),
        "source_acquisition_policy": (
            live_acquisition.identity.qualified,
            retired_acquisition.identity.qualified,
        ),
        "claim_evidence_admission_policy": (
            live_claim_type.identity.qualified,
            retired_claim_type.identity.qualified,
        ),
        "claim_admission_policy": (
            live_claim_type.identity.qualified,
            retired_claim_type.identity.qualified,
        ),
        "claim_resolution_policy": (
            live_claim_type.identity.qualified,
            retired_claim_type.identity.qualified,
        ),
        "claim_evidence_freshness_policy": (
            live_claim_type.identity.qualified,
            retired_claim_type.identity.qualified,
        ),
        "claim_attestation_consequence_policy": (
            live_claim_type.identity.qualified,
            retired_claim_type.identity.qualified,
        ),
        "capture_retention_erasure_policy": (
            live_capture.identity.qualified,
            retired_capture.identity.qualified,
        ),
        "query_evaluation_policy": (
            live_query.identity.qualified,
            retired_query.identity.qualified,
        ),
        "document_activation_policy": (document.identity, None),
        "procedure_activation_policy": (
            live_procedure.identity.qualified,
            retired_procedure.identity.qualified,
        ),
        "line_trigger_policy": (
            live_line.identity.qualified,
            retired_line.identity.qualified,
        ),
    }
    assert procedure_artifact_digest(live_procedure).tagged.startswith("sha256:")
    return rows, expected


@pytest.mark.parametrize(
    "policy_kind",
    contracts.PlaybillPolicyKind.__args__,  # type: ignore[attr-defined]
)
def test_each_policy_kind_lists_only_its_live_declaring_carrier(
    complete_policy_inventory: tuple[
        tuple[contracts.PlaybillPolicyInForce, ...],
        dict[str, tuple[str, str | None]],
    ],
    policy_kind: str,
) -> None:
    rows, expected = complete_policy_inventory
    live_identity, retired_identity = expected[policy_kind]
    matching = [row for row in rows if row.policy_kind == policy_kind]

    assert len(matching) == 1
    assert matching[0].declaring_artifact_identity == live_identity
    if retired_identity is not None:
        assert retired_identity not in {
            row.declaring_artifact_identity for row in rows if row.policy_kind == policy_kind
        }
