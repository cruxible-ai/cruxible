"""Claim admission accounts stay compact and actionable on the CLI."""

from __future__ import annotations

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli
from tests.test_cli.test_playbill_documents import COORDINATE

CAPTURE = "sha256:" + "11" * 32
CONTRACT = "sha256:" + "22" * 32
CITATION = "sha256:" + "33" * 32


def _account() -> contracts.PlaybillCaptureAdmissionAccount:
    return contracts.PlaybillCaptureAdmissionAccount(
        tag="playbill-capture-admission-account-v1",
        citation_id=CITATION,
        capture_digest=CAPTURE,
        citation_role="evidence",
        citation_origin="independent",
        capture_contract_identity="CaptureContract:source.demo",
        capture_contract_digest=CONTRACT,
        status="not_admitted",
        decisions=[
            contracts.PlaybillCaptureEvidenceKindAdmission(
                tag="playbill-capture-evidence-kind-admission-v1",
                evidence_kind="source.observation",
                status="not_admitted",
                refusal_code="playbill.evidence.attestation_grade_missing",
                closest_rule_id="verified-source",
            )
        ],
    )


def _claim_v1() -> contracts.PlaybillClaimView:
    return contracts.PlaybillClaimView(
        coordinate=COORDINATE,
        envelope={"identity": "Claim:CLM-" + "a" * 32, "path": "claims/CLM-x.json"},
        facts=[],
    )


def test_claim_get_and_explain_render_one_actionable_line_per_capture(monkeypatch) -> None:
    class StubClient:
        def get_playbill_claim(self, instance_id, identity, *, evaluation_time=None):
            assert (instance_id, identity) == ("inst_cli", "CLM-" + "a" * 32)
            return contracts.PlaybillClaimViewV2(
                tag="playbill-claim-read-v2",
                coordinate_kind="canonical",
                coordinate=COORDINATE,
                envelope=_claim_v1().envelope,
                facts=[],
                admission_evaluation_time="2026-08-22T12:00:00Z",
                admission_accounts=[_account()],
            )

        def explain_playbill_claim(self, instance_id, identity, *, evaluation_time=None):
            assert (instance_id, identity) == ("inst_cli", "CLM-" + "a" * 32)
            return contracts.PlaybillClaimExplanationV2(
                tag="playbill-claim-explanation-v2",
                coordinate=COORDINATE,
                evaluation_time="2026-08-22T12:00:00Z",
                claim=_claim_v1(),
                law_evidence={},
                verdict={"verdict": "uncovered"},
                exact_attestations=[],
                source_handles=[],
                coverage={},
                admission_evaluation_time="2026-08-22T12:00:00Z",
                admission_accounts=[_account()],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    common = [
        "--server-url",
        "https://playbill.invalid",
        "--instance-id",
        "inst_cli",
        "playbill",
        "claim",
    ]
    get_result = CliRunner().invoke(cli, [*common, "get", "CLM-" + "a" * 32])
    assert get_result.exit_code == 0, get_result.output
    assert get_result.output.count(f"Capture {CAPTURE}") == 1
    assert "closest verified-source" in get_result.output

    explain_result = CliRunner().invoke(cli, [*common, "explain", "CLM-" + "a" * 32])
    assert explain_result.exit_code == 0, explain_result.output
    assert explain_result.output.count(f"Capture {CAPTURE}") == 1
    assert "verdict=uncovered" in explain_result.output
