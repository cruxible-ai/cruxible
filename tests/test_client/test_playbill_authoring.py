"""Client request-tag and response-model parity for ergonomic authoring."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

import cruxible_client
from cruxible_client import AccessProfile, ClaimRef, CruxibleClient, Playbill
from cruxible_client.authoring.inputs import AuthoringInputError, AuthoringInputV1
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.projection import AcceptedCoordinate

COORDINATE = {
    "tag": "playbill-accepted-coordinate-v1",
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}
INTENT_ID = "AIT-" + "5" * 32


def _client(handler: Any) -> CruxibleClient:
    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    return client


def _status() -> dict[str, Any]:
    return {
        "tag": "playbill-candidate-status-v1",
        "state": "draft",
        "proposal_id": None,
        "candidate_digest": None,
        "current_accepted_coordinate": COORDINATE,
        "path_to_acceptance": [],
        "accepted_generation": None,
    }


def _claim_payload() -> dict[str, Any]:
    return {
        "tag": "playbill-claim-authoring-payload-v1",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": {
                "tag": "playbill-semantic-address-v1",
                "artifact_path": "subjects/work_item/wi-42.json",
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


def test_authoring_input_error_preserves_published_exception_compatibility() -> None:
    error = AuthoringInputError(
        code="playbill.authoring.input_invalid",
        field_path="$.statement.subject",
        message="subject is invalid",
        repair="choose a listed subject",
    )

    assert isinstance(error, PlaybillFormatError)
    assert isinstance(error, ValueError)
    assert isinstance(hash(error), int)


def test_client_speaks_frozen_compile_and_submit_requests() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/compile"):
            return httpx.Response(
                200,
                json={
                    "tag": "playbill-authoring-preflight-result-v1",
                    "verdict": "refused",
                    "certificate": {"certificate_digest": "sha256:" + "6" * 64},
                    "frontier": {"diagnostics": [{"code": "example"}]},
                },
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-submit-result-v1",
                "intent": {"intent_id": INTENT_ID},
                "status": _status(),
            },
        )

    client = _client(handler)
    payload = _claim_payload()
    compiled = client.compile_playbill_authoring("inst", payload=payload)
    submitted = client.submit_playbill_authoring_intent("inst", INTENT_ID)

    assert compiled.verdict == "refused"
    assert submitted.status.state == "draft"
    assert json.loads(captured[0].content) == {
        "tag": "playbill-authoring-intent-compile-request-v1",
        "payload": payload,
        "intent_id": None,
    }
    assert json.loads(captured[1].content) == {"tag": "playbill-authoring-intent-submit-request-v1"}
    compiled_request = json.loads(captured[0].content)
    assert "base" not in compiled_request
    assert "claim_id" not in compiled_request["payload"]


def test_client_preserves_advisory_lint_outside_the_preflight_certificate() -> None:
    warning = {
        "code": "playbill.claim_type.anticipated_source_contract_omitted",
        "field_path": "$.evidence_admission_policy.rules",
        "source_id": "corpus.runbook",
        "contract_identity": "CaptureContract:playbill.foreign-source.corpus.runbook",
        "contract_digest": "sha256:" + "7" * 64,
        "replacement_rule_fragment": {"capture_contract_digests": ["sha256:" + "7" * 64]},
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-preflight-result-v1",
                "verdict": "passed",
                "certificate": {"certificate_digest": "sha256:" + "6" * 64},
                "frontier": {"diagnostics": []},
                "lint": {"tag": "playbill-claim-type-proposal-lint-v1", "warnings": [warning]},
            },
        )

    result = _client(handler).compile_playbill_authoring("inst", payload=_claim_payload())

    assert result.verdict == "passed"
    assert result.lint is not None
    assert result.lint.warnings == [warning]
    assert "lint" not in result.certificate
    assert "lint" not in result.frontier


def test_client_speaks_program_stamped_v3_request() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-intent-view-v1",
                "intent": {"intent_id": INTENT_ID},
            },
        )

    payload = _claim_payload()
    stamp = {
        "tag": "playbill-authoring-program-stamp-v1",
        "program_digest": "sha256:" + "7" * 64,
        "sdk_version": "0.4.0",
        "sdk_contract_snapshot_digest": "sha256:" + "8" * 64,
    }
    _client(handler).create_playbill_authoring_intent(
        "inst",
        payload=payload,
        reference_expectations=(),
        program_stamp=stamp,
    )

    assert json.loads(captured[0].content) == {
        "tag": "playbill-authoring-intent-create-request-v3",
        "payload": payload,
        "reference_expectations": [],
        "program_stamp": stamp,
    }


def test_client_speaks_tagless_input_request_variants() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/compile"):
            return httpx.Response(
                200,
                json={
                    "tag": "playbill-authoring-preflight-result-v1",
                    "verdict": "refused",
                    "certificate": {"certificate_digest": "sha256:" + "6" * 64},
                    "frontier": {"diagnostics": []},
                },
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-intent-view-v1",
                "intent": {"intent_id": INTENT_ID},
            },
        )

    input_value = {
        "kind": "claim",
        "subject": "project.work_item/wi-42",
        "predicate": "project.work_item.status",
    }
    client = _client(handler)
    client.create_playbill_authoring_input("inst", input=input_value)
    client.compile_playbill_authoring_input("inst", input=input_value)

    assert [json.loads(item.content)["tag"] for item in captured] == [
        "playbill-authoring-input-create-request-v1",
        "playbill-authoring-input-compile-request-v1",
    ]
    assert all(json.loads(item.content)["input"] == input_value for item in captured)


def test_client_get_resume_list_and_status_are_path_only_reads() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=_status())
        if request.url.path.endswith("/authoring/intents"):
            return httpx.Response(
                200,
                json={"tag": "playbill-authoring-intent-list-v1", "intents": []},
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-intent-view-v1",
                "intent": {"intent_id": INTENT_ID},
            },
        )

    client = _client(handler)
    client.get_playbill_authoring_intent("inst", INTENT_ID)
    client.resume_playbill_authoring_intent("inst", INTENT_ID)
    client.list_pending_playbill_authoring_intents("inst")
    status = client.playbill_authoring_intent_status("inst", INTENT_ID)

    assert status.state == "draft"
    assert [item.method for item in captured] == ["GET", "GET", "GET", "GET"]
    assert all(not item.content for item in captured)


def test_client_speaks_the_frozen_authoring_rebase_request() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-intent-view-v1",
                "intent": {"intent_id": INTENT_ID},
            },
        )

    result = _client(handler).rebase_playbill_authoring_intent("inst", INTENT_ID)

    assert result.intent["intent_id"] == INTENT_ID
    assert captured[0].url.path.endswith(f"/{INTENT_ID}/rebase")
    assert json.loads(captured[0].content) == {"tag": "playbill-authoring-intent-rebase-request-v1"}


def test_client_whoami_and_proposal_list_use_read_routes_and_status_query() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/whoami"):
            return httpx.Response(
                200,
                json={
                    "tag": "playbill-whoami-v1",
                    "actor_id": "owner",
                    "credential_label": "owner",
                    "actor_id_source": "runtime_credential_label",
                    "credential_permission_mode": "governed_write",
                    "principal_registration_status": "active",
                    "active_principal_ids": ["daemon", "owner"],
                    "coordinate": COORDINATE,
                },
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-proposal-list-v1",
                "coordinate": COORDINATE,
                "status_filter": "open",
                "entries": [],
            },
        )

    client = _client(handler)
    identity = client.playbill_whoami("inst")
    proposals = client.list_playbill_proposals("inst", status="open")

    assert identity.actor_id_source == "runtime_credential_label"
    assert proposals.status_filter == "open"
    assert [item.method for item in captured] == ["GET", "GET"]
    assert dict(captured[1].url.params) == {"status": "open"}


def test_removed_brief_has_no_sdk_export_builder_or_authoring_union_arm() -> None:
    for name in (
        "BriefClaimExpectation",
        "BriefKind",
        "BriefQueryRender",
        "ClaimSlotPolicyV1",
        "prepare_playbill_brief",
    ):
        assert not hasattr(cruxible_client, name)
        assert name not in cruxible_client.__all__
    assert not hasattr(Playbill, "brief")
    with pytest.raises(ValidationError):
        TypeAdapter(AuthoringInputV1).validate_python({"kind": "brief"})


RETIRED_CLAIM_ID = "CLM-" + "a" * 32
RETIREMENT_RATIONALE = "Retire the superseded Claim."


def _authored_change_set(captured: list[httpx.Request]) -> dict[str, Any]:
    """The change-set payload one prepared draft actually put on the wire."""

    request = next(item for item in captured if item.url.path.endswith("/compile"))
    payload = json.loads(request.content)["payload"]
    assert isinstance(payload, dict)
    return payload


def _workspace(path: Path) -> Path:
    """The smallest workspace a `Playbill` will open: one source catalog."""

    catalog = path / ".playbill"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: corpus.runbook
    locator: corpus/runbook.md
    document_id: runbook
    document_kind: runbook
    title: Runbook
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:runbook]
""",
        encoding="utf-8",
    )
    return path


def _retirement_playbill(workspace: Path) -> tuple[Playbill, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/compile"):
            return httpx.Response(
                200,
                json={
                    "tag": "playbill-authoring-preflight-result-v1",
                    "verdict": "passed",
                    "certificate": {
                        "intent_id": INTENT_ID,
                        "certificate_digest": "sha256:" + "6" * 64,
                    },
                    "frontier": {"diagnostics": []},
                },
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-intent-view-v1",
                "intent": {"intent_id": INTENT_ID, "intent_revision": 1},
            },
        )

    pb = Playbill(
        client=_client(handler),
        instance_id="inst",
        workspace=_workspace(workspace),
        access_profile=AccessProfile(
            profile_id="changeset-retire",
            permitted_access_classes=("instance", "public"),
            disclose_restricted_existence=True,
        ),
        clock=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
    )
    return pb, captured


@pytest.mark.parametrize("spelling", ["bare", "prefixed", "ref"])
def test_change_set_retire_takes_every_spelling_the_standalone_route_takes(
    tmp_path: Path,
    spelling: str,
) -> None:
    """`pb.changes().retire(...)` accepts exactly what `pb.retire_claim(...)` does.

    The SDK hands a Claim identity back as `Claim:CLM-...` -- off a search row,
    off a `KnowledgeCard`, on a `ClaimRef` -- and the standalone retirement
    route takes that spelling. The builder used to raise `ClaimFormatError` on
    it, so one string worked on one route and not the other. It is normalized
    at the boundary now, and the member on the wire is the one canonical bare
    Claim ID whichever spelling the caller had to hand.
    """

    pb, captured = _retirement_playbill(tmp_path / "workspace")
    claim: str | ClaimRef = RETIRED_CLAIM_ID
    if spelling == "prefixed":
        claim = f"Claim:{RETIRED_CLAIM_ID}"
    elif spelling == "ref":
        claim = ClaimRef(
            address=f"Claim:{RETIRED_CLAIM_ID}",
            coordinate=AcceptedCoordinate(**COORDINATE),
        )

    draft = pb.changes(rationale=RETIREMENT_RATIONALE).retire(claim, reason="was-wrong")

    assert draft.prepare().intent_id == INTENT_ID
    payload = _authored_change_set(captured)
    assert payload["tag"] == "playbill-change-set-authoring-payload-v1"
    assert [member["claim_ref"] for member in payload["members"]] == [RETIRED_CLAIM_ID]
    assert [member["tag"] for member in payload["members"]] == [
        "playbill-claim-retirement-authoring-payload-v1"
    ]


def test_change_set_retire_dedups_the_prefixed_and_bare_spellings(tmp_path: Path) -> None:
    """One retirement is one member however the caller spelled the Claim.

    Create-dedup keys on the payload digest, so a spelling that survived into
    the member would have opened two live intents for one retirement. Both
    drafts must put byte-identical change-set payloads on the wire, and a draft
    naming one Claim under both spellings must refuse as the duplicate member
    it is rather than authoring two.
    """

    payloads = []
    for name, spelling in (("bare", RETIRED_CLAIM_ID), ("prefixed", f"Claim:{RETIRED_CLAIM_ID}")):
        pb, captured = _retirement_playbill(tmp_path / name)
        pb.changes(rationale=RETIREMENT_RATIONALE).retire(spelling, reason="was-wrong").prepare()
        payloads.append(_authored_change_set(captured))

    assert payloads[0] == payloads[1]

    pb, _captured = _retirement_playbill(tmp_path / "duplicate")
    duplicated = (
        pb.changes(rationale=RETIREMENT_RATIONALE)
        .retire(RETIRED_CLAIM_ID, reason="was-wrong")
        .retire(f"Claim:{RETIRED_CLAIM_ID}", reason="was-wrong")
    )
    with pytest.raises(ValidationError, match="member identities must be unique"):
        duplicated.prepare()


def test_publication_warning_client_mirror_refuses_non_sha256_citation_ids() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        cruxible_client.contracts.PlaybillPublicationPrepareWarning(
            tag="playbill-publication-prepare-warning-v1",
            code="playbill.authoring.publication_citation_anchor_collision",
            source_id="repo.work-items",
            citation_ids=["not-a-digest"],
        )
