"""Query filters on `now`: compile refuses what submit refuses (coldkit-03).

A cold kit author wrote `opened_at < now - P3D`, read `authoring compile` as
passing and met the refusal only at `authoring submit`. Both run one preflight;
these tests pin that the served compile verdict carries the refusal submit
reports, diagnostic for diagnostic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from cruxible_client.authoring.examples import subject_kind_example
from cruxible_client.transport.http import CruxibleClient


def _transport(http: TestClient) -> CruxibleClient:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    return transport


def _work_items_with_opened_at(transport: CruxibleClient, instance_id: str) -> None:
    """Work items keyed by slug, with a one-valued datetime `opened_at`."""

    change_set = subject_kind_example().model_dump(mode="json")
    opened = json.loads(json.dumps(change_set["members"][0]))
    opened["claim_type"]["identity"]["name"] = "project.work_item.opened_at"
    opened["claim_type"]["predicate"] = "project.work_item.opened_at"
    opened["claim_type"]["value"] = {"kind": "datetime"}
    opened["claim_type"]["description"] = "When the work item was opened."
    del opened["claim_type"]["canonical_form"]
    change_set["members"].append(opened)
    landed = transport.submit_authoring_input(instance_id, input=change_set)
    assert landed.model_dump(mode="json")["outcome"]["status"] == "landed", landed


def _query(where: str) -> dict[str, Any]:
    return {
        "kind": "query_definition",
        "query_definition": {
            "query": "project.stale_work",
            "from": {"item": "project.work_item"},
            "where": where,
            "fields": ["item.opened_at"],
        },
    }


def _codes(preflight: Any) -> list[str]:
    return [item["code"] for item in preflight["frontier"]["diagnostics"]]


def test_compile_refuses_the_filter_submit_refuses(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, _ = playbill_http
    transport = _transport(http)
    _work_items_with_opened_at(transport, instance_id)
    payload = _query("item.opened_at < now - P3D")

    compiled = transport.compile_authoring_input(instance_id, input=payload)
    submitted = transport.submit_authoring_input(instance_id, input=payload).model_dump(mode="json")

    assert compiled.verdict == "refused"
    compiled_codes = _codes(compiled.model_dump(mode="json"))
    assert "cruxible.query.filter_invalid" in compiled_codes
    assert submitted["outcome"]["status"] == "refused"
    assert submitted["outcome"]["reason"] == "preflight_refused"
    assert _codes(submitted["intent"]["last_preflight"]) == compiled_codes
