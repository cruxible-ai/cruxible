"""`cruxible line grant LINE --settle-observations` over a served daemon (D-R50).

The grant is one CLI command; it lowers to an exact per-ClaimType v4 Mandate,
lands through the ordinary approval policy, and lets a real Line run settle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.authoring.models import (
    AuthoringChangeSetMember,
    SubjectKindAuthoringPayload,
    authoring_member_identity,
)
from cruxible_client.contracts.captures import capture_contract_digest
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli
from tests.core_support._light_capture_support import procedure_contract
from tests.core_support._subject_kind_support import bare_slug_claim_type, create_by_key
from tests.test_server.test_playbill_procedure_rung2_public import (  # noqa: F401
    DOCUMENT_ROW,
    LINE_NAME,
    PREDICATE,
    PROCEDURE_NAME,
    SUBJECT_ID,
    SUBJECT_KIND,
    _authored,
    _claim_type,
    _document_kind,
    _policy,
    _procedure_definition,
    _workspace_file_members,
    installed_host,
)
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

pytestmark = pytest.mark.executor_only


def _stand_up_line(
    transport: CruxibleClient,
    http: TestClient,
    instance_id: str,
    reviewer_key: Path,
    workspace: Path,
    *,
    roles: tuple[str, ...],
) -> None:
    """The rung-2 advisory Line, able to settle, with no Mandate yet."""

    contract = _document_kind()
    contract_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    emission = procedure_contract()
    definition = _procedure_definition(
        contract_pin=contract_pin,
        output_contract_pin=ArtifactPin(
            role="capture-contract",
            target=emission.identity,
            artifact_digest=capture_contract_digest(emission).tagged,
        ),
        workspace=workspace,
    )
    claim_type = _claim_type(contract_pin.target.name).model_dump(mode="json")
    claim_type["permitted_roles"] = list(roles)
    for rule in claim_type["evidence_admission_policy"]["rules"]:
        rule["claim_roles"] = list(roles)
    members: list[dict[str, Any]] = [
        {
            "tag": "playbill-capture-contract-authoring-payload-v1",
            "capture_contract": contract.model_dump(mode="json"),
        },
        {"tag": "cruxible-contract-authoring-payload-v1", **DOCUMENT_ROW},
        {"tag": "playbill-claim-type-authoring-payload-v1", "claim_type": claim_type},
        {
            "tag": "playbill-claim-type-authoring-payload-v1",
            "claim_type": bare_slug_claim_type(SUBJECT_KIND).model_dump(mode="json"),  # type: ignore[attr-defined]
        },
        SubjectKindAuthoringPayload(
            name=SUBJECT_KIND, keys=(f"{SUBJECT_KIND}.slug",), uniqueness="unique"
        ).model_dump(mode="json"),
        create_by_key(SUBJECT_KIND, SUBJECT_ID).model_dump(mode="json"),  # type: ignore[attr-defined]
        *_workspace_file_members(instance_id),
        lower_authoring_input(_authored(definition)).model_dump(mode="json", by_alias=True),
        {
            "tag": "cruxible-line-authoring-payload-v2",
            "name": LINE_NAME,
            "procedure_name": PROCEDURE_NAME,
            "acquisition": {"advisory.capture": _policy().model_dump(mode="json")},
            "max_authority": "settle",
        },
    ]
    adapter = TypeAdapter(AuthoringChangeSetMember)
    members.sort(
        key=lambda item: authoring_member_identity(adapter.validate_python(item)).encode("utf-8")
    )
    compiled = transport.compile_authoring(
        instance_id,
        payload={
            "tag": "playbill-change-set-authoring-payload-v1",
            "members": members,
            "rationale": "Stand up the advisory severity Line.",
        },
    )
    assert compiled.verdict == "passed", compiled.frontier
    submitted = transport.submit_authoring_intent(
        instance_id, str(compiled.certificate.intent_id), accept="never"
    )
    assert submitted.status.proposal_id is not None, submitted
    _approve_and_activate(http, instance_id, reviewer_key, submitted.status.proposal_id)


def _grant(
    monkeypatch: pytest.MonkeyPatch, transport: CruxibleClient, instance_id: str, *args: str
) -> Any:
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    return CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://cruxible",
            "--instance-id",
            instance_id,
            "line",
            "grant",
            LINE_NAME,
            *args,
        ],
    )


def test_line_grant_settles_a_real_observation_line_run(
    installed_host: tuple[TestClient, str, Path, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, instance_id, reviewer_key, workspace = installed_host
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    _stand_up_line(transport, http, instance_id, reviewer_key, workspace, roles=("observation",))
    line_digest = line_identity_digest(ArtifactIdentity(kind="Line", name=LINE_NAME))

    # Without a Mandate a settle Line cannot deliver its Claims.
    dry = _grant(monkeypatch, transport, instance_id, "--settle-observations", "--dry-run")
    assert dry.exit_code == 0, dry.output
    lines = dry.stdout.splitlines()
    assert lines[0] == f"grant  Mandate:{LINE_NAME}-settle  settle  Line:{LINE_NAME}"
    assert "namespace  claims" in lines
    assert f"ClaimType:{PREDICATE}  create=extracted,measured  revise=extracted,measured" in lines
    assert lines[-1] == "would pass"

    granted = _grant(monkeypatch, transport, instance_id, "--settle-observations", "--json")
    assert granted.exit_code == 0, granted.output
    record = json.loads(granted.stdout)
    assert record["mandate"]["scope"] == "observations"
    assert record["scope"]["claim_types"][0]["claim_type"] == PREDICATE
    outcome = record["result"]["outcome"]
    # The instance's policy lets this operator land a Mandate directly.
    assert outcome["status"] == "landed", outcome

    proof = transport.get(
        instance_id, request=GetRequest(ref=f"Mandate:{LINE_NAME}-settle", detail="proof")
    ).proof
    assert proof is not None
    envelope = proof["envelope"]
    assert envelope["artifact_format"] == "cruxible-procedure-mandate-v4"
    assert envelope["namespace"] == ["claims"]
    (scope,) = envelope["scope"]
    assert scope["claim_type"]["target"]["name"] == PREDICATE
    assert scope["changes"] == {
        "create": {"basis": ["extracted", "measured"]},
        "revise": {"basis": ["extracted", "measured"]},
    }

    state = transport.run_line(instance_id, line_digest, occurrence_id=None)
    assert state.status == "succeeded", state.model_dump_json(indent=2)
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "settled", egress.model_dump_json(indent=2)
    assert egress.outside_scope == ()


def test_line_grant_refuses_a_procedure_that_writes_a_normative_claim_type(
    installed_host: tuple[TestClient, str, Path, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, instance_id, reviewer_key, workspace = installed_host
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    _stand_up_line(
        transport, http, instance_id, reviewer_key, workspace, roles=("normative", "observation")
    )

    refused = _grant(monkeypatch, transport, instance_id, "--settle-observations")
    assert refused.exit_code == 1, refused.output
    assert "cruxible.authoring.procedure_mandate_observations_not_observation" in refused.stderr
    assert f"{PREDICATE} permits normative, observation" in refused.stderr

    # The daemon's own lowering refuses the same payload, whoever sends it.
    from cruxible_client.authoring.inputs import ChangeSetInput
    from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput

    preview = transport.preview_authoring_input(
        instance_id,
        input=ChangeSetInput(
            kind="change_set",
            members=(
                ProcedureMandateInput(
                    kind="procedure_mandate",
                    name=f"{LINE_NAME}-settle",
                    line_name=LINE_NAME,
                    grants="settle",
                    valid_from="2026-01-01T00:00:00Z",  # type: ignore[arg-type]
                    expires_at="2027-01-01T00:00:00Z",  # type: ignore[arg-type]
                    scope="observations",
                ),
            ),
        ).model_dump(mode="json"),
    )
    assert preview.verdict == "refused"
    codes = {item.code for item in preview.frontier.diagnostics}
    assert "cruxible.authoring.procedure_mandate_observations_not_observation" in codes, codes


def test_line_grant_requires_the_flag_and_parses_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_client() -> Any:
        raise AssertionError("no request is sent for a usage error")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", no_client)
    common = ["--server-url", "http://cruxible", "--instance-id", "inst_x", "line", "grant"]
    missing = CliRunner().invoke(cli, [*common, "some.line"])
    past = CliRunner().invoke(
        cli, [*common, "some.line", "--settle-observations", "--expires", "2020-01-01T00:00:00Z"]
    )
    garbage = CliRunner().invoke(
        cli, [*common, "some.line", "--settle-observations", "--expires", "soon"]
    )
    bad_name = CliRunner().invoke(cli, [*common, "Some.Line", "--settle-observations"])
    assert missing.exit_code == 2 and "--settle-observations" in missing.output
    assert past.exit_code == 2 and "expire after now" in past.output
    assert garbage.exit_code == 2 and "30d" in garbage.output
    assert bad_name.exit_code == 2 and "--name" in bad_name.output


def test_grant_expiry_spans_and_instants() -> None:
    from datetime import UTC, datetime, timedelta

    from cruxible_core.cli.commands.playbill import _grant_expiry

    now = datetime(2026, 11, 23, tzinfo=UTC)
    assert _grant_expiry("90d", now=now) == now + timedelta(days=90)
    assert _grant_expiry("12w", now=now) == now + timedelta(weeks=12)
    assert _grant_expiry("1y", now=now) == now + timedelta(days=365)
    assert _grant_expiry("36h", now=now) == now + timedelta(hours=36)
    assert _grant_expiry("P30D", now=now) == now + timedelta(days=30)
    assert _grant_expiry("2027-01-01T00:00:00Z", now=now) == datetime(2027, 1, 1, tzinfo=UTC)
