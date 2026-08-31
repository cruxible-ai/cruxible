"""PB-E CLI explanation passes an exact accepted coordinate."""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli
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
