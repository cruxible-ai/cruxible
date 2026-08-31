"""HTTP route parity for the frozen AuthoringIntent verbs."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import contracts
from cruxible_client.authoring.examples import claim_flow_a_example
from cruxible_core.playbill.authoring.insertions import PublicationClaimNotAccepted
from cruxible_core.playbill.claim_type_inputs import (
    lower_claim_type_input,
)
from tests.test_client.test_playbill_authoring import OBSERVATION
from tests.test_playbill._claim_type_support import claim_type_input_example

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
INTENT_ID = "AIT-" + "5" * 32

EMPTY_PUBLICATION_OBSERVATION = {
    "tag": "playbill-publication-source-observation-v2",
    "source_id": "repo.work-items",
    "content_base64": "",
    "content_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "byte_length": 0,
}


def test_http_compile_and_submit_keep_the_frozen_request_boundary(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    seen: list[tuple[str, object]] = []

    def compile_stub(selected: str, *, payload: object, intent_id: str | None = None):
        seen.append((selected, payload))
        assert intent_id is None
        return contracts.PlaybillAuthoringPreflightResult(
            verdict="refused",
            certificate={"certificate_digest": "sha256:" + "6" * 64},
            frontier={"diagnostics": [{"code": "example"}]},
        )

    def submit_stub(selected: str, intent_id: str):
        seen.append((selected, intent_id))
        status = contracts.PlaybillCandidateStatus(
            state="draft",
            current_accepted_coordinate=COORDINATE,
        )
        return contracts.PlaybillAuthoringSubmitResult(
            intent={"intent_id": intent_id},
            status=status,
        )

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_authoring_compile", compile_stub
    )
    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_authoring_submit", submit_stub)
    payload = {
        "tag": "playbill-claim-authoring-payload-v1",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": {
                "tag": "playbill-semantic-address-v1",
                "artifact_path": "subjects/work_item/wi-42.yaml",
                "selector": {"scheme": "artifact-v1", "value": ""},
            },
            "predicate": "work.status",
            "qualifier": None,
            "object": {"kind": "literal", "value": "ready"},
            "role": "observation",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": "Observed ready.",
        "source": {
            "tag": "playbill-self-source-body-v1",
            "content_base64": "cmVhZHk=",
        },
        "citation_role": None,
        "claim_ref": None,
        "existing_claim_dispositions": [],
        "insertion_target": None,
    }
    compiled = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/compile",
        json={
            "tag": "playbill-authoring-intent-compile-request-v1",
            "payload": payload,
            "intent_id": None,
        },
    )
    submitted = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents/{INTENT_ID}/submit",
        json={"tag": "playbill-authoring-intent-submit-request-v1"},
    )

    assert compiled.status_code == 200, compiled.text
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["tag"] == "playbill-authoring-submit-result-v1"
    assert seen[0][0] == instance_id
    assert seen[1] == (instance_id, INTENT_ID)


def test_http_input_variants_delegate_without_exposing_a_base(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    input_value = {
        "kind": "claim",
        "subject": "project.work_item/wi-42",
        "predicate": "project.work_item.status",
        "object": {"kind": "literal", "value": "ready"},
        "role": "observation",
        "rationale": "Observed ready.",
        "source": {"kind": "self_source", "body": "status: ready\n"},
    }
    seen: list[object] = []

    def create_stub(selected: str, *, input: object):
        seen.append((selected, input))
        return contracts.PlaybillAuthoringIntentView(intent={"intent_id": INTENT_ID})

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_authoring_create_input",
        create_stub,
    )
    response = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents",
        json={
            "tag": "playbill-authoring-input-create-request-v1",
            "input": input_value,
        },
    )

    assert response.status_code == 200, response.text
    assert seen and seen[0][0] == instance_id


def test_http_claim_retire_route_delegates_the_typed_request(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    seen: list[object] = []

    def retire_stub(selected: str, claim_id: str, *, request: object):
        seen.append((selected, claim_id, request))
        return contracts.PlaybillClaimRetirePreflight(
            operation_digest="sha256:" + "1" * 64,
            coordinate=COORDINATE,
            root_identity={"kind": "Claim", "name": claim_id},
            root_predecessor_digest="sha256:" + "2" * 64,
            reason="was-rescinded",
            effective_until=None,
            required_dependents=[],
            diagnostics=[],
            submit_ready=True,
        )

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_retire_claim",
        retire_stub,
    )
    claim_id = "CLM-0123456789abcdef0123456789abcdef"
    response = client.post(
        f"/api/v1/{instance_id}/playbill/claims/{claim_id}/retire",
        json={
            "tag": "playbill-claim-retire-request-v1",
            "mode": "preflight",
            "claim_ref": f"Claim:{claim_id}",
            "reason": "was-rescinded",
            "effective_until": None,
            "expected_coordinate": COORDINATE.model_dump(mode="json"),
            "dependents": [],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["submit_ready"] is True
    assert seen[0][0:2] == (instance_id, claim_id)  # type: ignore[index]
    assert getattr(seen[0][2], "tag") == "playbill-claim-retire-request-v1"  # type: ignore[index]
    assert "base" not in response.request.content.decode()


def test_http_prepare_route_maps_insertion_protocol_refusal_to_typed_400(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http

    def prepare_refusal(selected: str, intent_id: str, *, observation: object):
        assert (selected, intent_id) == (instance_id, INTENT_ID)
        raise PublicationClaimNotAccepted(
            "playbill.authoring.publication_claim_not_accepted: Claim is not accepted"
        )

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_authoring_prepare_publication",
        prepare_refusal,
    )
    response = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents/{INTENT_ID}/insertion/prepare",
        json={
            "tag": "playbill-insertion-prepare-request-v2",
            "observation": EMPTY_PUBLICATION_OBSERVATION,
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_type"] == "PublicationClaimNotAccepted"
    assert response.json()["error_code"] == "playbill.authoring.publication_claim_not_accepted"


def test_http_create_flow_a_stub_surfaces_the_bind_refusal(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents",
        json={
            "tag": "playbill-authoring-input-create-request-v1",
            "input": claim_flow_a_example().model_dump(mode="json"),
        },
    )

    assert response.status_code == 400
    assert response.json()["message"] == (
        "playbill.authoring.working_selection_requires_bind at input.source: "
        "create and compile cannot observe local working-source bytes. "
        "Repair: Run playbill authoring bind with this input and the selected local file."
    )


def test_http_authoring_openapi_and_runtime_reject_removed_brief_input(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    schemas = client.app.openapi()["components"]["schemas"]

    for name in ("PlaybillAuthoringInputCreateRequest", "PlaybillAuthoringInputCompileRequest"):
        mapping = schemas[name]["properties"]["input"]["discriminator"]["mapping"]
        assert set(mapping) == {"claim", "procedure"}
    assert "BriefInput" not in schemas
    assert "ClaimSlotPolicyV1" not in schemas
    assert schemas["ClaimType"]["properties"]["artifact_format"]["enum"] == [
        "playbill-claim-type-v1",
        "playbill-claim-type-v3",
        "playbill-claim-type-v4",
    ]

    response = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents",
        json={
            "tag": "playbill-authoring-input-create-request-v1",
            "input": {"kind": "brief"},
        },
    )

    assert response.status_code == 422


def test_http_migration_route_delegates_the_typed_request(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    seen: list[object] = []

    def migrate_stub(selected: str, *, request: object):
        seen.append((selected, request))
        return contracts.PlaybillClaimTypeMigrationResult(
            operation_digest="sha256:" + "1" * 64,
            dependents=[],
            proposal=contracts.PlaybillProposalInspection(
                proposal={},
                accepted_coordinate=COORDINATE,
            ),
        )

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_migrate_claim_type",
        migrate_stub,
    )
    response = client.post(
        f"/api/v1/{instance_id}/playbill/claim-types/migrations",
        json={
            "tag": "playbill-claim-type-migration-request-v1",
            "successor": {
                "predicate": "project.work_item.status",
                "allowed_subject_kinds": ["project.work_item"],
                "object_kind": "literal",
                "cardinality": "one",
                "permitted_roles": ["observation"],
                "evidence_admission_policy": {"rules": []},
                "admission_policy": {
                    "corroboration_requirements": [],
                    "freeze_requirements": [],
                },
                "resolution_policy": {
                    "cardinality": "one",
                    "eligible_verdicts": ["supported"],
                    "selector": "only_contender",
                },
            },
            "dependents": [],
        },
    )

    assert response.status_code == 200, response.text
    assert seen and seen[0][0] == instance_id


@pytest.mark.parametrize("operation", ["migrate", "propose"])
def test_http_claim_type_lowering_returns_typed_nested_validation_refusal(
    playbill_http: tuple[TestClient, str, Path],
    operation: str,
) -> None:
    client, instance_id, _private_key = playbill_http
    claim_type_input = claim_type_input_example().model_dump(mode="json")
    claim_type_input["evidence_admission_policy"] = {
        "rules": [
            {
                "rule_id": "derivational-without-reducer",
                "claim_roles": ["observation"],
                "capture_contract_digests": ["sha256:" + "a" * 64],
                "evidence_kinds": ["self_asserted"],
                "admission": "derivational",
                "subject_binding": "exact_claim_subject",
            }
        ]
    }
    if operation == "migrate":
        path = f"/api/v1/{instance_id}/playbill/claim-types/migrations"
        request = {
            "tag": "playbill-claim-type-migration-request-v2",
            "mode": "preflight",
            "successor": claim_type_input,
        }
    else:
        path = f"/api/v1/{instance_id}/playbill/claim-types/proposals"
        request = {
            "tag": "playbill-claim-type-input-propose-request-v1",
            "input": claim_type_input,
            "proposal_name": "invalid-derivational-policy",
        }

    response = client.post(path, json=request)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error_type"] == "ClaimTypeInputValidationError"
    assert body["error_code"] == "playbill.claim_type.input_invalid"
    assert "$.evidence_admission_policy.rules[0]" in body["message"]
    assert "derivational evidence requires at least one allowed reducer" in body["message"]


def test_http_migration_domain_refusal_is_a_bad_request(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/claim-types/migrations",
        json={
            "tag": "playbill-claim-type-migration-request-v2",
            "mode": "preflight",
            "successor": claim_type_input_example().model_dump(mode="json"),
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_type"] == "ClaimTypeMigrationError"
    assert "migration requires an accepted predecessor" in response.json()["message"]


def test_http_claim_type_input_proposal_delivers_actionable_source_lint(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    claim_type_input = {
        **claim_type_input_example().model_dump(mode="json"),
        "anticipated_source_ids": ["corpus.runbook"],
    }

    response = client.post(
        f"/api/v1/{instance_id}/playbill/claim-types/proposals",
        json={
            "tag": "playbill-claim-type-input-propose-request-v1",
            "input": claim_type_input,
            "proposal_name": "lint-delivery",
        },
    )

    assert response.status_code == 200, response.text
    warnings = response.json()["lint"]["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["code"] == "playbill.claim_type.anticipated_source_contract_omitted"
    assert warnings[0]["source_id"] == "corpus.runbook"


@pytest.mark.parametrize("operation", ["expert_propose", "migrate"])
def test_http_claim_type_routes_preserve_optional_lint_payload(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
    operation: str,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    warning = {
        "code": "playbill.claim_type.evidence_policy_admits_no_accepted_contract",
        "field_path": "$.evidence_admission_policy.rules",
        "source_id": None,
        "contract_identity": "CaptureContract:available",
        "contract_digest": "sha256:" + "7" * 64,
        "replacement_rule_fragment": {"capture_contract_digests": ["sha256:" + "7" * 64]},
    }
    lint = contracts.PlaybillClaimTypeProposalLint(warnings=[warning])
    if operation == "expert_propose":
        monkeypatch.setattr(
            "cruxible_core.runtime.playbill_api.playbill_propose_claim_type",
            lambda _selected, **_values: contracts.PlaybillProposalInspection(
                proposal={"proposal_id": "sha256:" + "8" * 64},
                accepted_coordinate=COORDINATE,
                lint=lint,
            ),
        )
        path = f"/api/v1/{instance_id}/playbill/claim-types/proposals"
        request = {
            "claim_type": lower_claim_type_input(claim_type_input_example(), tree={}).model_dump(
                mode="json"
            ),
            "proposal_name": "warn",
        }
    else:
        monkeypatch.setattr(
            "cruxible_core.runtime.playbill_api.playbill_migrate_claim_type",
            lambda _selected, **_values: contracts.PlaybillClaimTypeMigrationPreflight(
                coordinate=COORDINATE,
                successor_artifact_digest="sha256:" + "9" * 64,
                dependents=[],
                lint=lint,
            ),
        )
        path = f"/api/v1/{instance_id}/playbill/claim-types/migrations"
        request = {
            "tag": "playbill-claim-type-migration-request-v2",
            "mode": "preflight",
            "successor": claim_type_input_example().model_dump(mode="json"),
        }

    response = client.post(path, json=request)

    assert response.status_code == 200, response.text
    assert response.json()["lint"]["warnings"] == [warning]


def test_http_refuses_digest_and_base_smuggling_in_request_models(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents",
        json={
            "tag": "playbill-authoring-intent-create-request-v1",
            "payload": {
                "tag": "playbill-claim-authoring-payload-v1",
                "claim_id": "CLM-" + "0" * 32,
                "base": COORDINATE.model_dump(mode="json"),
            },
        },
    )
    assert response.status_code == 422
    assert "claim_id" in response.text or "statement" in response.text


def test_http_whoami_and_proposal_inventory_are_typed_reads(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    seen: list[tuple[str, str | None]] = []
    who = contracts.PlaybillWhoAmI(
        actor_id="operator",
        credential_label="operator",
        actor_id_source="local_operator",
        credential_permission_mode="admin",
        principal_registration_status="active",
        active_principal_ids=["daemon", "operator"],
        coordinate=COORDINATE,
    )
    proposals = contracts.PlaybillProposalList(
        coordinate=COORDINATE,
        status_filter="open",
        entries=[],
    )

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_whoami",
        lambda selected: (seen.append((selected, None)), who)[1],
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_list_proposals",
        lambda selected, *, status=None: (seen.append((selected, status)), proposals)[1],
    )

    identity = client.get(f"/api/v1/{instance_id}/playbill/whoami")
    listing = client.get(f"/api/v1/{instance_id}/playbill/proposals?status=open")

    assert identity.status_code == listing.status_code == 200
    assert identity.json()["tag"] == "playbill-whoami-v1"
    assert listing.json()["tag"] == "playbill-proposal-list-v1"
    assert seen == [(instance_id, None), (instance_id, "open")]


def test_http_insertion_confirm_and_abandon_are_typed(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    seen: list[str] = []

    def confirm_stub(selected: str, intent_id: str, *, observation: object):
        assert selected == instance_id
        assert intent_id == INTENT_ID
        seen.append("confirm")
        return contracts.PlaybillInsertionConfirmResultV2(
            tag="playbill-insertion-confirm-result-v2",
            outcome="bound",
            intent={"intent_id": intent_id},
            expectation={"state": "bound"},
        )

    def abandon_stub(selected: str, intent_id: str):
        assert (selected, intent_id) == (instance_id, INTENT_ID)
        seen.append("abandon")
        return contracts.PlaybillInsertionAbandonResult(
            intent={"intent_id": intent_id},
            expectation={"state": "abandoned"},
        )

    def prepare_stub(selected: str, intent_id: str, *, observation: object):
        assert selected == instance_id
        assert intent_id == INTENT_ID
        seen.append("prepare")
        return contracts.PlaybillInsertionPrepareResult(
            tag="playbill-insertion-prepare-result-v2",
            outcome="prepared",
            intent={"intent_id": intent_id},
            expectation={"state": "prepared"},
            preparation={"preparation_digest": "sha256:" + "7" * 64},
            warnings=[
                contracts.PlaybillPublicationPrepareWarning(
                    tag="playbill-publication-prepare-warning-v1",
                    code="playbill.authoring.publication_citation_anchor_collision",
                    source_id="repo.work-items",
                    citation_ids=["sha256:" + "8" * 64],
                )
            ],
        )

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_authoring_confirm_insertion",
        confirm_stub,
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_authoring_abandon_insertion",
        abandon_stub,
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_authoring_prepare_publication",
        prepare_stub,
    )
    confirmed = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents/{INTENT_ID}/insertion/confirm",
        json={"tag": "playbill-insertion-confirm-request-v2", "observation": OBSERVATION},
    )
    abandoned = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents/{INTENT_ID}/insertion/abandon",
        json={"tag": "playbill-insertion-abandon-request-v1"},
    )
    prepared = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents/{INTENT_ID}/insertion/prepare",
        json={
            "tag": "playbill-insertion-prepare-request-v2",
            "observation": {
                "tag": "playbill-publication-source-observation-v2",
                "source_id": "repo.work-items",
                "content_base64": "",
                "content_digest": (
                    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
                ),
                "byte_length": 0,
            },
        },
    )

    assert confirmed.status_code == prepared.status_code == abandoned.status_code == 200
    assert confirmed.json()["outcome"] == "bound"
    assert prepared.json()["outcome"] == "prepared"
    assert prepared.json()["warnings"][0]["citation_ids"] == ["sha256:" + "8" * 64]
    assert seen == ["confirm", "abandon", "prepare"]
