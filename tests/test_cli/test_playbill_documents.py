"""PB-E CLI canonical Document read parity."""

from __future__ import annotations

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def test_cli_allocates_and_remembers_a_playbill_host(monkeypatch, tmp_path) -> None:
    class StubClient:
        def create_playbill_host(
            self, *, instance_id: str | None = None
        ) -> contracts.PlaybillHostResult:
            assert instance_id == "inst_cli_host"
            return contracts.PlaybillHostResult(instance_id=instance_id, status="created")

    context_path = tmp_path / "context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_path))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "playbill",
            "host",
            "create",
            "--instance-id",
            "inst_cli_host",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"instance_id": "inst_cli_host"' in result.stdout
    assert '"instance_id": "inst_cli_host"' in context_path.read_text()


def test_cli_lists_documents_with_their_canonical_coordinate(monkeypatch) -> None:
    class StubClient:
        def list_playbill_documents(self, instance_id: str) -> contracts.PlaybillDocumentList:
            assert instance_id == "inst_cli"
            return contracts.PlaybillDocumentList(
                coordinate=COORDINATE,
                documents=[
                    contracts.PlaybillDocumentView(
                        coordinate=COORDINATE,
                        envelope={
                            "identity": "document:design",
                            "path": "documents/design.yaml",
                        },
                        facts=[],
                    )
                ],
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
            "document",
            "list",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "document:design  documents/design.yaml" in result.stdout
    assert f"Coordinate: {COORDINATE.git_oid}" in result.stdout
