"""PB-E CLI explanation passes an exact accepted coordinate."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli
from cruxible_core.deprecation import DEFAULT_REMOVAL_VERSION
from tests.test_cli.test_playbill_documents import COORDINATE


def test_cli_explain_binds_the_document_subject_to_its_coordinate(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubClient:
        def get_playbill_document(
            self, instance_id: str, identity: str
        ) -> contracts.PlaybillDocumentView:
            assert (instance_id, identity) == ("inst_cli", "document:design")
            return contracts.PlaybillDocumentView(
                coordinate=COORDINATE,
                envelope={
                    "identity": identity,
                    "path": "documents/design.json",
                },
                facts=[],
            )

        def explain_playbill_subject(
            self,
            instance_id: str,
            *,
            subject: dict[str, Any],
            at: contracts.PlaybillAcceptedCoordinate,
            detail: str,
            include_body: bool,
        ) -> contracts.PlaybillExplainResult:
            calls.append(
                {
                    "instance_id": instance_id,
                    "subject": subject,
                    "at": at.model_dump(mode="json"),
                    "detail": detail,
                    "include_body": include_body,
                }
            )
            return contracts.PlaybillExplainResult(
                subject=subject,
                coordinate=COORDINATE,
                detail="evidence",
                governance={},
                provenance={},
                attestation_coverage={},
                history={},
                source_mapping=None,
                proof_references=[],
                redactions=["body"],
                supported_details=["summary", "evidence"],
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "playbill",
            "explain",
            "document:design",
            "--detail",
            "evidence",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["coordinate"] == COORDINATE.model_dump(mode="json")
    assert calls == [
        {
            "instance_id": "inst_cli",
            "subject": {
                "tag": "playbill-semantic-address-v1",
                "artifact_path": "documents/design.json",
                "selector": {"scheme": "artifact-v1", "value": ""},
            },
            "at": COORDINATE.model_dump(mode="json"),
            "detail": "evidence",
            "include_body": False,
        }
    ]


def _subject_view(path: str) -> contracts.PlaybillSubjectView:
    return contracts.PlaybillSubjectView(
        coordinate=COORDINATE,
        envelope={"identity": "Subject:sec.package/click", "path": path},
        facts=[],
        incoming=[
            contracts.PlaybillSubjectIncomingGroupV1(
                predicate="sec.vuln.affects_package",
                claims=[
                    contracts.PlaybillSubjectIncomingClaimV1(
                        claim_identity="Claim:CLM-" + "a" * 32,
                        subject_identity="subjects/sec.vuln/cve-2026-69247.json",
                    )
                ],
            )
        ],
    )


class _SubjectStubClient:
    def __init__(self) -> None:
        self.explained: list[dict[str, Any]] = []

    def get_playbill_document(
        self, instance_id: str, identity: str
    ) -> contracts.PlaybillDocumentView:
        raise AssertionError("a Subject address must never take the Document route")

    def get_playbill_subject(
        self, instance_id: str, subject_kind: str, subject_id: str
    ) -> contracts.PlaybillSubjectView:
        assert (instance_id, subject_kind, subject_id) == (
            "inst_cli",
            "sec.package",
            "click",
        )
        return _subject_view("subjects/sec.package/click.json")

    def explain_playbill_subject(
        self,
        instance_id: str,
        *,
        subject: dict[str, Any],
        at: contracts.PlaybillAcceptedCoordinate,
        detail: str,
        include_body: bool,
    ) -> contracts.PlaybillExplainResult:
        self.explained.append({"subject": subject, "detail": detail})
        return contracts.PlaybillExplainResult(
            subject=subject,
            coordinate=COORDINATE,
            detail="summary",
            governance={},
            provenance={},
            attestation_coverage={},
            history={},
            source_mapping=None,
            proof_references=[],
            redactions=[],
            supported_details=["summary"],
        )


@pytest.mark.parametrize("identity", ("sec.package/click", "Subject:sec.package/click"))
def test_cli_explain_resolves_a_subject_address_instead_of_a_document_404(
    monkeypatch,
    identity: str,
) -> None:
    """`explain sec.package/click` used to 404 on a Document lookup that could not match."""

    client = _SubjectStubClient()
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: client,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "playbill",
            "explain",
            identity,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert client.explained == [
        {
            "subject": {
                "tag": "playbill-semantic-address-v1",
                "artifact_path": "subjects/sec.package/click.json",
                "selector": {"scheme": "artifact-v1", "value": ""},
            },
            "detail": "summary",
        }
    ]


def test_cli_subject_get_takes_the_address_and_deprecates_the_two_argument_form(
    monkeypatch,
) -> None:
    client = _SubjectStubClient()
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: client,
    )
    common = [
        "--server-url",
        "https://playbill.invalid",
        "--instance-id",
        "inst_cli",
        "playbill",
        "subject",
        "get",
    ]

    address = CliRunner().invoke(cli, [*common, "sec.package/click"])
    legacy = CliRunner().invoke(cli, [*common, "sec.package", "click"])
    refused = CliRunner().invoke(cli, [*common, "sec.package"])

    assert address.exit_code == 0, address.output
    assert legacy.exit_code == 0, legacy.output
    assert json.loads(address.output) == json.loads(legacy.stdout)
    # The profile carries the incoming edges the object side could not see before.
    assert json.loads(address.output)["incoming"] == [
        {
            "tag": "playbill-subject-incoming-group-v1",
            "predicate": "sec.vuln.affects_package",
            "claims": [
                {
                    "tag": "playbill-subject-incoming-claim-v1",
                    "claim_identity": "Claim:CLM-" + "a" * 32,
                    "subject_identity": "subjects/sec.vuln/cve-2026-69247.json",
                }
            ],
        }
    ]
    warning = json.loads(
        next(
            line for line in legacy.stderr.splitlines() if line.startswith("Deprecation: ")
        ).removeprefix("Deprecation: ")
    )
    assert warning == {
        "surface": "playbill subject get KIND ID two-argument form",
        "replacement": "one `kind/name` Subject address argument",
        "removal_version": DEFAULT_REMOVAL_VERSION,
    }
    assert refused.exit_code != 0
    assert "is not a Subject address" in refused.output
