"""CLI authoring adapters keep payloads local and machine identity opaque."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
INTENT_ID = "AIT-" + "5" * 32


def test_cli_compile_reads_payload_and_submit_uses_only_opaque_intent(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    payload = tmp_path / "claim.json"
    payload.write_text(
        json.dumps({"tag": "playbill-claim-authoring-payload-v1", "example": "value"})
    )
    calls: list[tuple[str, object]] = []

    class StubClient:
        def compile_playbill_authoring(
            self,
            instance_id: str,
            *,
            payload: dict[str, object],
            intent_id: str | None,
        ) -> contracts.PlaybillAuthoringPreflightResult:
            calls.append((instance_id, payload))
            assert intent_id is None
            return contracts.PlaybillAuthoringPreflightResult(
                verdict="refused",
                certificate={"certificate_digest": "sha256:" + "6" * 64},
                frontier={"diagnostics": []},
            )

        def submit_playbill_authoring_intent(
            self, instance_id: str, intent_id: str
        ) -> contracts.PlaybillAuthoringSubmitResult:
            calls.append((instance_id, intent_id))
            status = contracts.PlaybillCandidateStatus(
                state="draft",
                current_accepted_coordinate=COORDINATE,
            )
            return contracts.PlaybillAuthoringSubmitResult(
                intent={"intent_id": intent_id},
                status=status,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    runner = CliRunner()
    common = [
        "--server-url",
        "https://authoring.example.test",
        "--instance-id",
        "inst_authoring",
        "playbill",
        "authoring",
    ]
    compiled = runner.invoke(cli, [*common, "compile", str(payload), "--json"])
    submitted = runner.invoke(cli, [*common, "submit", INTENT_ID, "--json"])

    assert compiled.exit_code == 0, compiled.output
    assert submitted.exit_code == 0, submitted.output
    assert calls == [
        ("inst_authoring", {"tag": "playbill-claim-authoring-payload-v1", "example": "value"}),
        ("inst_authoring", INTENT_ID),
    ]
    assert "target: inst_authoring @ https://authoring.example.test (explicit)" in compiled.stderr
    assert INTENT_ID in submitted.output


def test_cli_status_is_a_read_and_emits_no_write_target(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class StubClient:
        def playbill_authoring_intent_status(
            self, instance_id: str, intent_id: str
        ) -> contracts.PlaybillCandidateStatus:
            assert (instance_id, intent_id) == ("inst_authoring", INTENT_ID)
            return contracts.PlaybillCandidateStatus(
                state="draft",
                current_accepted_coordinate=COORDINATE,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "playbill",
            "authoring",
            "status",
            INTENT_ID,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
