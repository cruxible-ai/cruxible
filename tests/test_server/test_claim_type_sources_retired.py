"""A retired source list refuses typed on every surface (ruling 2026-11-16)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import Cruxible
from cruxible_client.contracts.claim_type_inputs import ClaimTypeSourcesRetired
from cruxible_client.transport.http import CruxibleClient
from tests.core_support._claim_type_support import claim_type_input_example

CODE = "cruxible.claim_type.sources_retired"


def test_http_refuses_anticipated_source_ids_with_its_code_and_the_evidence_repair(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/claim-types/proposals",
        json={
            "tag": "playbill-claim-type-input-propose-request-v1",
            "input": {
                **claim_type_input_example().model_dump(mode="json"),
                "anticipated_source_ids": ["corpus.runbook"],
            },
            "proposal_name": "retired-sources",
        },
    )
    assert 400 <= response.status_code < 500, response.text
    body = response.json()
    assert body["error_code"] == CODE, body
    assert "evidence: [<contract name>, ...]" in body["message"]
    assert "evidence: [<contract name>, ...]" in str(body["repair"]), body


def test_the_sdk_refuses_sources_with_the_same_typed_error(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, instance_id, _private_key = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = client  # type: ignore[assignment]
    (tmp_path / ".cruxible").mkdir()
    cx = Cruxible._from_client(transport, instance_id=instance_id, workspace=tmp_path)
    with pytest.raises(ClaimTypeSourcesRetired) as caught:
        cx.changes(rationale="A field.").field(
            "dev.item", "seen", "string{max_len: 64}", sources=["osv.dev"]
        )
    assert caught.value.error_code == CODE
    assert "dev.item.seen: sources= is retired" in str(caught.value)
    assert "evidence: [<contract name>, ...]" in str(caught.value)
