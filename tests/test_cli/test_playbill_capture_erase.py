"""`cruxible capture erase`: a preview that names what goes, committed only with its digest."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.capture_erasure import (
    CaptureEraseRequest,
    CaptureEraseResult,
    CaptureErasureRecord,
)
from cruxible_client.contracts.change_control import StateCoordinate
from cruxible_core.cli.main import cli

PREFIX = ["--server-url", "http://server", "--instance-id", "inst_erase"]
CAPTURE = "sha256:" + "5f31df955084" + "0" * 52
BODY = "sha256:" + "b" * 64
RULE = "sha256:" + "c" * 64
BECAUSE = "Scan output printed the deploy token; rotated."


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))


def _result(status: str) -> CaptureEraseResult:
    return CaptureEraseResult(
        status=status,  # type: ignore[arg-type]
        erasure=CaptureErasureRecord(
            capture_digest=CAPTURE,
            capture_contract="CaptureContract:security.scan-output",
            capture_contract_digest="sha256:" + "d" * 64,
            erasure_rule_digest=RULE,
            body_digests=(BODY,),
            erased_by="alice",
            erased_at=datetime(2026, 10, 21, 18, 0, tzinfo=UTC),
            because=BECAUSE,
        ),
        citing_claims=("CLM-" + "1" * 32,),
        citing_claim_count=1,
        left_without_evidence=("CLM-" + "1" * 32,),
        coordinate=StateCoordinate.of(f"capture_erasure:{CAPTURE}", {"bodies": {BODY: True}}),
    )


class _StubClient:
    def __init__(self) -> None:
        self.requests: list[CaptureEraseRequest] = []

    def erase_capture(self, instance_id: str, request: CaptureEraseRequest) -> Any:
        assert instance_id == "inst_erase"
        self.requests.append(request)
        committing = request.dry_run is False
        return _result("erased" if committing else "would_erase")


def _stub(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    client = _StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def test_the_default_previews_and_names_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stub(monkeypatch)

    result = CliRunner().invoke(
        cli, [*PREFIX, "capture", "erase", "CAP-5f31df955084", "--because", BECAUSE]
    )

    assert result.exit_code == 0, result.output
    (request,) = stub.requests
    assert (request.capture_digest, request.because, request.dry_run) == (
        "CAP-5f31df955084",
        BECAUSE,
        None,
    )
    digest = _result("would_erase").coordinate.digest
    assert result.output.splitlines() == [
        "Would erase the retained body of CAP-5f31df955084 (1 object) under "
        "CaptureContract:security.scan-output",
        f"Because: {BECAUSE}",
        f"Cited by 1 live Claim: CLM-{'1' * 32}",
        "Left with no retained evidence (next lists them as claim_evidence_erased): "
        f"CLM-{'1' * 32}",
        "Verdicts will read it as no longer replayable; each field's rules decide the rest.",
        f"Preview at {digest}; nothing was written.",
        f"Commit it: rerun the same command with --commit --at {digest}",
    ]


def test_a_commit_carries_the_previews_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stub(monkeypatch)
    digest = _result("would_erase").coordinate.digest

    result = CliRunner().invoke(
        cli,
        [*PREFIX, "capture", "erase", CAPTURE, "--because", BECAUSE, "--commit", "--at", digest],
    )

    assert result.exit_code == 0, result.output
    (request,) = stub.requests
    assert (request.dry_run, request.at) == (False, digest)
    assert result.output.splitlines()[:3] == [
        "Erased the retained body of CAP-5f31df955084 (1 object) under "
        "CaptureContract:security.scan-output",
        "By: alice at 2026-10-21T18:00:00+00:00",
        f"Because: {BECAUSE}",
    ]


def test_a_because_with_a_control_character_never_leaves_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _stub(monkeypatch)

    result = CliRunner().invoke(
        cli, [*PREFIX, "capture", "erase", CAPTURE, "--because", "rotated\nError: forged"]
    )

    assert result.exit_code != 0
    assert stub.requests == []


def test_an_erase_refusal_prints_its_repair_line_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.capture_erasure import CaptureEraseRefusedError
    from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
    from cruxible_client.errors import response_to_error
    from cruxible_core.server.errors import error_to_response

    repair_line = "Author a successor contract built with authorize_erasure(contract)"
    refusal = CaptureEraseRefusedError(
        "cruxible.capture.erasure_prohibited",
        "CaptureContract:security.scan-output does not authorize erasing its Captures",
        capture_digest=CAPTURE,
        repair=HandEditRepair(
            hand_edit=HandEditInstruction(
                target="CaptureContract:security.scan-output", required_change=repair_line
            )
        ),
        repair_line=repair_line,
    )
    wire = response_to_error(*error_to_response(refusal))

    class _Refusing:
        def erase_capture(self, instance_id: str, request: CaptureEraseRequest) -> Any:
            raise wire

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: _Refusing())
    result = CliRunner().invoke(
        cli, [*PREFIX, "capture", "erase", "CAP-5f31df955084", "--because", BECAUSE]
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines()[-2:] == [
        "Error: cruxible.capture.erasure_prohibited: CaptureContract:security.scan-output "
        "does not authorize erasing its Captures.",
        f"Repair: {repair_line}",
    ]
