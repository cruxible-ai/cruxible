"""Governed policy inventory is exact, live, coordinate-pinned state."""

from __future__ import annotations

from cruxible_client import contracts
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import render_claim_type
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.service.playbill_policies import list_playbill_policies_in_force
from tests.test_playbill._adoption_fixture import _claim_type, _claim_type_path, _query_definition
from tests.test_playbill._support import initialize_local
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
    assert [row.policy_kind for row in historical.policies] == ["approval_policy"]
