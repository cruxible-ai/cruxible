"""Query filters on `now`: compile refuses what submit refuses (coldkit-03).

A cold kit author wrote `opened_at < now - P3D`, read `authoring compile` as
passing and met the refusal only at `authoring submit`. Both run one preflight;
these tests pin that the served compile verdict carries the refusal submit
reports, diagnostic for diagnostic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from cruxible_client.authoring.examples import subject_kind_example
from cruxible_client.contracts.captures import COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.write import (
    CreateChange,
    CreatedSubject,
    SetChange,
    WriteRequest,
)
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
    # The author's own words support it, so a written value is visible to a query.
    opened["claim_type"]["evidence_admission_policy"] = ClaimEvidenceAdmissionPolicy(
        rules=(
            ClaimEvidenceAdmissionRule(
                rule_id="own-words",
                claim_roles=("normative",),
                capture_contracts=(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name,),
                admission="direct",
                subject_binding="exact_claim_subject",
            ),
        )
    ).model_dump(mode="json")
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


#: Reads evaluate after the writes land (a Claim is visible from its acceptance on).
EVALUATION_TIME = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)


def _open_items(transport: CruxibleClient, instance_id: str, **opened: datetime) -> None:
    changes: list[Any] = []
    for slug, moment in opened.items():
        label = slug.replace("-", "_")
        changes.append(CreateChange(label=label, subject_kind="project.work_item", key=slug))
        changes.append(
            SetChange(
                subject=CreatedSubject(created=label),
                field="opened_at",
                value=moment.isoformat().replace("+00:00", "Z"),
            )
        )
    outcome = transport.write(
        instance_id, request=WriteRequest(because="Opened.", changes=tuple(changes))
    )
    assert not outcome.refused, outcome


def test_a_query_on_now_minus_three_days_lands_as_v4_and_reads_the_evaluation_time(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, _ = playbill_http
    transport = _transport(http)
    _work_items_with_opened_at(transport, instance_id)
    _open_items(
        transport,
        instance_id,
        **{
            "wi-old": EVALUATION_TIME - timedelta(days=4),
            "wi-new": EVALUATION_TIME - timedelta(days=1),
        },
    )
    payload = _query("item.opened_at < now - 72h")

    compiled = transport.compile_authoring_input(instance_id, input=payload)
    assert compiled.verdict == "passed", _codes(compiled.model_dump(mode="json"))
    landed = transport.submit_authoring_input(instance_id, input=payload).model_dump(mode="json")
    assert landed["outcome"]["status"] == "landed", landed

    def slugs(at: datetime) -> list[str]:
        result = transport.query(
            instance_id,
            request=QueryRequest(name="project.stale_work", evaluation_time=at),
        ).model_dump(mode="json")
        return sorted(row["subject_id"] for row in result["rows"])

    assert slugs(EVALUATION_TIME) == ["wi-old"]
    assert slugs(EVALUATION_TIME + timedelta(days=3)) == ["wi-new", "wi-old"]
    # Stored in its one canonical form: whole seconds, read back in days.
    card = transport.get(
        instance_id, request=GetRequest(ref="query:project.stale_work")
    ).model_dump(mode="json")
    assert "now - 3d" in json.dumps(card)
