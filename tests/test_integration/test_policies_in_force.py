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
    ClaimAttestationConsequencePolicy,
    ClaimAttestationConsequenceRule,
    ClaimEvidenceFreshness,
    ClaimFreshnessDuration,
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
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.service.claims.policies import service_playbill_policies_in_force
from tests.core_support._adoption_fixture import _claim_type, _claim_type_path, _query_definition
from tests.core_support._support import initialize_local
from tests.support.lines import action_trigger, trigger_members
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_integration.test_acquisition_policies import _policy, _rule
from tests.test_procedures.test_procedure_artifacts import _artifact, _definition
from tests.test_service.test_typed_source_catalogs import _published_sources


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

    result = service_playbill_policies_in_force(instance)

    assert result.coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert [row.policy_kind for row in result.policies] == [
        "approval_policy",
        "capture_retention_erasure_policy",
        "claim_admission_policy",
        "claim_evidence_admission_policy",
        "claim_resolution_policy",
        "procedure_runtime_policy",
        "query_evaluation_policy",
        # The three Triggers every new instance is seeded with.
        "trigger_schedule",
        "trigger_schedule",
        "trigger_schedule",
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
    historical = service_playbill_policies_in_force(
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
        "trigger_schedule",
        "trigger_schedule",
        "trigger_schedule",
    ]

    assert historical.coordinate.git_oid == genesis.oid
    assert historical.coordinate.git_oid != result.coordinate.git_oid
    seeded_schedules = {
        "Trigger:evidence-sweep": {"kind": "cadence", "interval_seconds": 86400},
        "Trigger:floor-refresh": {"kind": "generation_accepted"},
        "Trigger:prediction-anchor-retry": {"kind": "cadence", "interval_seconds": 3600},
    }
    for inventory in (result, historical):
        schedules = [row for row in inventory.policies if row.policy_kind == "trigger_schedule"]
        assert {
            row.declaring_artifact_identity: row.policy for row in schedules
        } == seeded_schedules
        for row in schedules:
            assert row.placement == "embedded"
            assert row.declaring_artifact_kind == "Trigger"
            assert row.field_path == "/schedule"


def test_policy_inventory_skips_the_cards_an_accepted_change_leaves(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Policy inventory excludes derivative card sidecars."""

    instance, owner = initialize_local(tmp_path)
    claim_type = _claim_type(0)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name)] = (
        render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
    )
    tree[_claim_type_path(claim_type)] = render_claim_type(claim_type)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-08-30T20:00:00.000000Z",
        proposal_name="seed-card-bearing-change",
    )
    accepted = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert [path for path in accepted if path.startswith("cards/")]

    result = service_playbill_policies_in_force(instance)

    assert result.policies
    assert not any(row.path.startswith("cards/") for row in result.policies)


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
                "evidence_freshness": ClaimEvidenceFreshness(
                    stale_after=ClaimFreshnessDuration(microseconds=60_000_000)
                ),
                "attestation_consequence_policy": ClaimAttestationConsequencePolicy(
                    rules=(
                        ClaimAttestationConsequenceRule(
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
        body_digest=instance.body_store().store(b"Policy carrier\n").digest,
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

    # The seeded default Triggers are three live carriers; this inventory keeps one.
    for path in [path for path in tree if path.startswith("triggers/")]:
        del tree[path]
    live_trigger = action_trigger("policy-sweep", action="evidence.sweep", interval_seconds=60)
    retired_trigger = action_trigger(
        "retired-policy-sweep",
        action="evidence.sweep",
        interval_seconds=60,
        lifecycle=ArtifactLifecycle(state="retired"),
    )
    tree.update(trigger_members(live_trigger, retired_trigger))

    (root / "indexed").mkdir()
    with pytest.MonkeyPatch.context() as patch:
        indexed, coordinate, _reads = _published_sources(
            root / "indexed", tree, patch, instance=instance
        )
        patch.setattr(indexed, "accepted_coordinate", lambda: coordinate)
        rows = tuple(service_playbill_policies_in_force(indexed).policies)
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
        "trigger_schedule": (
            live_trigger.identity.qualified,
            retired_trigger.identity.qualified,
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
