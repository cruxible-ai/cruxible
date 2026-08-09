"""Internal HTTP parity routes used by procedure CLI/MCP server mode."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import cruxible_core.service.procedures as procedure_service
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from tests.test_procedures.conftest import CONFIG_YAML, actor


@pytest.fixture
def app_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    with TestClient(create_app()) as client:
        yield client
    get_manager().clear()
    reset_registry()


def _init_procedure_instance(client: TestClient, root: Path) -> str:
    root.mkdir()
    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": CONFIG_YAML},
    )
    assert response.status_code == 200, response.text
    instance_id = str(response.json()["instance_id"])
    locked = client.post(f"/api/v1/{instance_id}/workflows/lock", json={})
    assert locked.status_code == 200, locked.text
    return instance_id


def _definition() -> dict[str, object]:
    return {
        "name": "http_procedure",
        "contract_in": "ProcedureInput",
        "steps": [
            {
                "id": "shape",
                "shape_items": {
                    "items": [{"value": "$input.value"}],
                    "fields": {"value": "$item.value"},
                },
                "as": "result",
            }
        ],
        "returns": "result",
        "precondition": {},
        "budget": {"wall_clock_s": 10, "max_provider_calls": 0},
    }


def test_procedure_routes_cover_lifecycle_run_and_read_envelopes(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init_procedure_instance(app_client, tmp_path / "workspace")
    proposed = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={
            "definition": _definition(),
            "actor_context": actor("http-proposer").model_dump(mode="json"),
        },
    )
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["warnings"] == []
    procedure_id = proposed.json()["procedure"]["procedure_id"]

    listed = app_client.get(
        f"/api/v1/{instance_id}/procedures",
        params={"status": "pending"},
    )
    shown = app_client.get(f"/api/v1/{instance_id}/procedures/{procedure_id}")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["procedure_id"] == procedure_id
    empty_track_record = {
        "runs": 0,
        "succeeded": 0,
        "failed": 0,
        "refused": 0,
        "budget_exceeded": 0,
        "in_flight": 0,
        "last_succeeded_at": None,
        "top_refusal_reason": None,
        "linked_outcomes": None,
    }
    assert listed.json()["items"][0]["track_record"] == empty_track_record
    assert listed.json()["read_revision"] is not None
    assert shown.status_code == 200, shown.text
    assert shown.json()["procedure"]["procedure_id"] == procedure_id
    assert shown.json()["procedure"]["track_record"] == empty_track_record
    assert shown.json()["contract_in_schema"] == {
        "fields": [{"name": "value", "type": "int", "required": True}],
        "allow_extra": False,
        "input_example": {"value": 1},
    }

    # accept enforces reviewer independence, so an HTTP caller may not name the
    # reviewer itself; the request is attributed to the local operator instead.
    spoofed = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/resolve",
        json={
            "action": "accept",
            "expected_version": 1,
            "actor_context": actor("http-reviewer").model_dump(mode="json"),
        },
    )
    assert spoofed.status_code == 401, spoofed.text
    assert "reviewer independence" in spoofed.json()["message"]

    accepted = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/resolve",
        json={"action": "accept", "expected_version": 1},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["procedure"]["status"] == "live"

    reading = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/readings",
        json={
            "subject_grain": "procedure_unit",
            "grade": "attestation",
            "verdict": "satisfied",
            "observed_at": "2026-07-22T12:00:00Z",
            "actor_context": actor("http-reader").model_dump(mode="json"),
        },
    )
    assert reading.status_code == 200, reading.text
    assert reading.json()["procedure_id"] == procedure_id
    assert reading.json()["grade"] == "attestation"

    executed = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/run",
        json={
            "input_payload": {"value": 1},
            "actor_context": actor("http-runner").model_dump(mode="json"),
        },
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["run"]["status"] == "finalized"
    assert executed.json()["run"]["verdict"] == "succeeded"

    tracked_list = app_client.get(f"/api/v1/{instance_id}/procedures")
    tracked_show = app_client.get(f"/api/v1/{instance_id}/procedures/{procedure_id}")
    expected_track_record = {
        "runs": 1,
        "succeeded": 1,
        "failed": 0,
        "refused": 0,
        "budget_exceeded": 0,
        "in_flight": 0,
        "last_succeeded_at": executed.json()["run"]["finalized_at"],
        "top_refusal_reason": None,
        "linked_outcomes": {
            "contract_grade": {
                "readings": 0,
                "satisfied": 0,
                "contradicted": 0,
                "indeterminate": 0,
            },
            "attestation_grade": {
                "readings": 1,
                "satisfied": 1,
                "contradicted": 0,
                "indeterminate": 0,
            },
        },
    }
    assert tracked_list.json()["items"][0]["track_record"] == expected_track_record
    assert tracked_show.json()["procedure"]["track_record"] == expected_track_record

    runs = app_client.get(f"/api/v1/{instance_id}/procedures/{procedure_id}/runs")
    assert runs.status_code == 200, runs.text
    assert runs.json()["items"][0]["run_id"] == executed.json()["run"]["run_id"]
    assert runs.json()["read_revision"] is not None

    retired = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/retire",
        json={
            "expected_version": 2,
            "reason": "superseded operationally",
            "actor_context": actor("http-retirer").model_dump(mode="json"),
        },
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["procedure"]["status"] == "retired"


def test_withdraw_route_retracts_a_pending_proposal_and_frees_its_name(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init_procedure_instance(app_client, tmp_path / "workspace")
    author = actor("http-proposer").model_dump(mode="json")
    proposed = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={"definition": _definition(), "actor_context": author},
    )
    assert proposed.status_code == 200, proposed.text
    procedure_id = proposed.json()["procedure"]["procedure_id"]

    # Unlike resolve, withdraw accepts the caller-asserted author identity: it
    # asserts the actor IS the proposer rather than that it is independent.
    withdrawn = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/withdraw",
        json={
            "expected_version": 1,
            "reason": "re-proposing with a tighter budget",
            "actor_context": author,
        },
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["action"] == "withdraw"
    assert withdrawn.json()["procedure"]["status"] == "withdrawn"
    assert withdrawn.json()["procedure"]["reason"] == "re-proposing with a tighter budget"
    assert withdrawn.json()["receipt_id"]

    repeated = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/withdraw",
        json={"expected_version": 2, "actor_context": author},
    )
    assert repeated.status_code == 400, repeated.text
    assert "must be pending; found 'withdrawn'" in repeated.json()["message"]

    listed = app_client.get(
        f"/api/v1/{instance_id}/procedures",
        params={"status": "withdrawn"},
    )
    assert listed.status_code == 200, listed.text
    assert [item["procedure_id"] for item in listed.json()["items"]] == [procedure_id]

    # The name is free: a fresh proposal under it accepts normally.
    reproposed = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={"definition": _definition(), "actor_context": author},
    )
    assert reproposed.status_code == 200, reproposed.text
    accepted = app_client.post(
        f"/api/v1/{instance_id}/procedures/"
        f"{reproposed.json()['procedure']['procedure_id']}/resolve",
        json={"action": "accept", "expected_version": 1},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["procedure"]["status"] == "live"


def test_superseding_a_pending_proposal_points_the_author_at_withdraw(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init_procedure_instance(app_client, tmp_path / "workspace")
    author = actor("http-proposer").model_dump(mode="json")
    pending = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={"definition": _definition(), "actor_context": author},
    )
    assert pending.status_code == 200, pending.text
    pending_id = pending.json()["procedure"]["procedure_id"]

    response = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={
            "definition": _definition(),
            "supersedes_procedure_id": pending_id,
            "actor_context": author,
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["message"].startswith(
        f"superseded procedure '{pending_id}' must be live; found 'pending'; "
        "the author may withdraw the pending proposal and re-propose"
    )


def test_invalid_procedure_definition_returns_typed_validation_error(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init_procedure_instance(app_client, tmp_path / "workspace")
    definition = _definition()
    definition["precondition"] = {"identity_verified": True}

    response = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={"definition": definition},
    )

    assert response.status_code == 400
    assert response.json()["error_type"] == "DataValidationError"
    assert response.json()["message"] == "Invalid procedure definition"
    assert response.json()["errors"] == [
        "precondition.identity_verified: Extra inputs are not permitted"
    ]


def test_procedure_proposal_authoring_lint_is_typed_and_surfaces_warnings(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init_procedure_instance(app_client, tmp_path / "workspace")
    invalid = _definition()
    steps = invalid["steps"]
    assert isinstance(steps, list)
    step = steps[0]
    assert isinstance(step, dict)
    step["shape_items"] = {
        "items": [{"value": "$input.undeclared"}],
        "fields": {"value": "$item.value"},
    }

    refused = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={
            "definition": invalid,
            "actor_context": actor("http-proposer").model_dump(mode="json"),
        },
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["error_type"] == "ConfigError"
    assert "step 'shape'" in refused.json()["message"]
    assert "'$input.undeclared'" in refused.json()["message"]
    assert "value (int, required)" in refused.json()["message"]

    warning_definition = _definition()
    warning_definition["budget"] = {"wall_clock_s": 10, "max_provider_calls": 2}
    proposed = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={
            "definition": warning_definition,
            "actor_context": actor("http-proposer").model_dump(mode="json"),
        },
    )

    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["warnings"] == [
        "budget.max_provider_calls (2) exceeds the expanded provider-call count (0); "
        "the extra headroom is unreachable"
    ]


def test_procedure_runtime_reference_failure_is_typed_and_audited(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = _init_procedure_instance(app_client, tmp_path / "workspace")
    definition = _definition()
    steps = definition["steps"]
    assert isinstance(steps, list)
    step = steps[0]
    assert isinstance(step, dict)
    step["as"] = "transactions"
    definition["returns"] = "$steps.transactions.result"
    # Both propose and accept compile through this one seam, so patching it is
    # what lets a definition the current rules refuse reach a live status.
    original_compile = procedure_service._compile_procedure_definition

    def compile_as_legacy_accepted(
        instance: Any,
        candidate: ProcedureDefinition,
        input_payload: dict[str, Any] | None = None,
    ) -> Any:
        valid_candidate = candidate.model_copy(update={"returns": "transactions"})
        plan, warnings = original_compile(instance, valid_candidate, input_payload)
        return plan.model_copy(update={"returns": candidate.returns}), warnings

    with monkeypatch.context() as acceptance_context:
        acceptance_context.setattr(
            procedure_service,
            "_compile_procedure_definition",
            compile_as_legacy_accepted,
        )
        proposed = app_client.post(
            f"/api/v1/{instance_id}/procedures/propose",
            json={
                "definition": definition,
                "actor_context": actor("http-proposer").model_dump(mode="json"),
            },
        )
        assert proposed.status_code == 200, proposed.text
        procedure_id = proposed.json()["procedure"]["procedure_id"]
        accepted = app_client.post(
            f"/api/v1/{instance_id}/procedures/{procedure_id}/resolve",
            json={"action": "accept", "expected_version": 1},
        )
        assert accepted.status_code == 200, accepted.text

    response = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/run",
        json={
            "input_payload": {"value": 1},
            "actor_context": actor("http-runner").model_dump(mode="json"),
        },
    )

    expected_message = (
        "Procedure step 'shape' failed to resolve runtime reference "
        "'$steps.transactions.result' (KeyError)"
    )
    assert response.status_code == 400
    payload = response.json()
    receipt_id = payload["mutation_receipt_id"]
    assert isinstance(receipt_id, str)
    assert payload == {
        "error_type": "QueryExecutionError",
        "message": expected_message,
        "error_code": None,
        "errors": [],
        "context": {},
        "mutation_receipt_id": receipt_id,
    }

    runs = app_client.get(f"/api/v1/{instance_id}/procedures/{procedure_id}/runs")
    assert runs.status_code == 200, runs.text
    run = runs.json()["items"][0]
    assert run["status"] == "finalized"
    assert run["verdict"] == "failed"
    assert run["receipt_id"] == receipt_id

    receipt_response = app_client.get(f"/api/v1/{instance_id}/receipts/{receipt_id}")
    assert receipt_response.status_code == 200, receipt_response.text
    root_detail = receipt_response.json()["nodes"][0]["detail"]
    assert root_detail["error"] == expected_message
    assert root_detail["error_type"] == "QueryExecutionError"
    assert root_detail["verdict"] == "failed"


def test_procedure_routes_are_part_of_the_public_openapi() -> None:
    """Flipped by the 0.3.0 surface commit: exposure is deliberate contract."""
    spec = create_app().openapi()
    assert any("/procedures" in path for path in spec["paths"])


def test_invalid_declared_tier_rejection_lists_the_valid_tiers(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    """A tier typo must not cost a round trip to learn the accepted values."""
    instance_id = _init_procedure_instance(app_client, tmp_path / "workspace")
    definition = _definition()
    definition["declared_tier"] = "read_only"

    response = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={"definition": definition},
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [
        "declared_tier: Input should be 'governed_write', 'graph_write' or 'admin'"
    ]
