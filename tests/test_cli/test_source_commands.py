"""CLI tests for source artifact commands."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.errors import (
    SourceArtifactNotFoundError as ClientSourceArtifactNotFoundError,
)
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_source_register_outputs_registered_artifact_json(
    runner: CliRunner,
    initialized_project: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs_dir = initialized_project.root / "docs"
    docs_dir.mkdir()
    evidence_path = docs_dir / "evidence.md"
    evidence_path.write_text("# Evidence\n\nWorkspace-local source text.\n")
    monkeypatch.chdir(initialized_project.root)

    result = runner.invoke(
        cli,
        [
            "source",
            "register",
            "--path",
            "docs/evidence.md",
            "--original-uri",
            "https://example.test/evidence.md",
            "--label",
            "workspace evidence",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source_artifact_id"].startswith("SRC-")
    assert payload["source_kind"] == "markdown"
    assert payload["source_retention"] == "manifest_only"
    assert payload["original_uri"] == "https://example.test/evidence.md"
    assert payload["label"] == "workspace evidence"
    assert payload["archived"] is False
    assert any(
        chunk["heading_path"] == ["Evidence"] and chunk["block_selector"] == "paragraph:1"
        for chunk in payload["chunks"]
    )


def test_source_dereference_returns_registered_source_text(
    runner: CliRunner,
    initialized_project: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs_dir = initialized_project.root / "docs"
    docs_dir.mkdir()
    evidence_path = docs_dir / "evidence.md"
    evidence_path.write_text("# Evidence\n\nWorkspace-local source text.\n")
    monkeypatch.chdir(initialized_project.root)

    register = runner.invoke(
        cli,
        [
            "source",
            "register",
            "--path",
            "docs/evidence.md",
            "--json",
        ],
    )
    assert register.exit_code == 0, register.output
    registered: dict[str, Any] = json.loads(register.stdout)
    paragraph_chunk = next(
        chunk for chunk in registered["chunks"] if chunk["block_selector"] == "paragraph:1"
    )

    by_chunk = runner.invoke(
        cli,
        [
            "source",
            "dereference",
            "--artifact",
            registered["source_artifact_id"],
            "--chunk",
            paragraph_chunk["chunk_id"],
            "--json",
        ],
    )

    assert by_chunk.exit_code == 0, by_chunk.output
    by_chunk_payload = json.loads(by_chunk.output)
    assert by_chunk_payload["status"] == "available"
    assert by_chunk_payload["body_origin"] == "local_path"
    assert by_chunk_payload["body"] == "Workspace-local source text."
    assert by_chunk_payload["chunk"]["block_selector"] == "paragraph:1"

    by_heading = runner.invoke(
        cli,
        [
            "source",
            "dereference",
            "--artifact",
            registered["source_artifact_id"],
            "--heading",
            "Evidence",
            "--block-selector",
            "paragraph:1",
        ],
    )

    assert by_heading.exit_code == 0, by_heading.output
    assert "Status: available" in by_heading.output
    assert "Workspace-local source text." in by_heading.output


def _compact_rendered_table(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in "_-")


def _source_artifact_list_result() -> contracts.SourceArtifactListResult:
    return contracts.SourceArtifactListResult(
        items=[
            contracts.SourceArtifactListItem(
                source_artifact_id="SRC-1",
                kind="markdown",
                retention="manifest_only",
                original_uri="https://example.test/source-1.md",
                label="Vendor evidence",
                content_hash="sha256:one",
                registered_at="2026-01-02T03:04:05Z",
                chunk_count=2,
                byte_count=123,
            ),
        ],
        total=2,
        limit=1,
        offset=1,
        truncated=True,
    )


def _source_artifact_read_result() -> contracts.SourceArtifactReadResult:
    return contracts.SourceArtifactReadResult(
        source_artifact_id="SRC-1",
        kind="markdown",
        retention="archive",
        original_uri="https://example.test/source-1.md",
        label="Vendor evidence",
        content_hash="sha256:one",
        registered_at="2026-01-02T03:04:05Z",
        chunk_count=1,
        byte_count=123,
        parser_version="markdown-chunks-v1",
        archived=True,
        archive_content_hash="sha256:one",
        content_available=True,
        body_origin="archive",
        chunks=[
            contracts.SourceArtifactReadChunk(
                chunk_id="CHK-1",
                heading_path=["Intro", "Evidence"],
                block_selector="paragraph:1",
                block_type="paragraph",
                line_start=10,
                line_end=12,
                content_hash="sha256:chunk",
                text="This full text must stay out of human output.",
            )
        ],
    )


def test_source_list_server_mode_renders_rows_and_json_shape(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    calls: list[tuple[str, int | None, int]] = []

    class StubClient:
        def list_source_artifacts(
            self,
            instance_id: str,
            *,
            limit: int | None = None,
            offset: int = 0,
        ) -> contracts.SourceArtifactListResult:
            calls.append((instance_id, limit, offset))
            return _source_artifact_list_result()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    rendered = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "source",
            "list",
            "--limit",
            "1",
            "--offset",
            "1",
        ],
    )

    assert rendered.exit_code == 0, rendered.output
    assert calls[-1] == ("inst_123", 1, 1)
    compact_output = _compact_rendered_table(rendered.output)
    assert "SRC-1" in rendered.output
    assert "Vendor" in compact_output
    assert "evidence" in compact_output
    assert "manifest_only" in compact_output
    assert "Total: 2  Truncated: True" in rendered.output

    as_json = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "source",
            "list",
            "--limit",
            "1",
            "--offset",
            "1",
            "--json",
        ],
    )

    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["total"] == 2
    assert payload["limit"] == 1
    assert payload["offset"] == 1
    assert payload["truncated"] is True
    assert payload["items"][0]["source_artifact_id"] == "SRC-1"
    assert payload["items"][0]["kind"] == "markdown"


def test_source_get_server_mode_renders_header_chunks_and_no_chunks(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    calls: list[tuple[str, str]] = []

    class StubClient:
        def get_source_artifact(
            self,
            instance_id: str,
            source_artifact_id: str,
        ) -> contracts.SourceArtifactReadResult:
            calls.append((instance_id, source_artifact_id))
            return _source_artifact_read_result()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    rendered = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "source",
            "get",
            "SRC-1",
        ],
    )

    assert rendered.exit_code == 0, rendered.output
    assert calls[-1] == ("inst_123", "SRC-1")
    assert "Source artifact: SRC-1" in rendered.output
    assert "Kind: markdown" in rendered.output
    assert "Label: Vendor evidence" in rendered.output
    assert "Original URI: https://example.test/source-1.md" in rendered.output
    assert "Retention: archive" in rendered.output
    assert "Content available: true" in rendered.output
    assert "CHK-1" in rendered.output
    assert "Intro > Evidence" in rendered.output
    assert "paragraph" in rendered.output
    assert "10-12" in rendered.output
    assert "This full text must stay out of human output." not in rendered.output

    no_chunks = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "source",
            "get",
            "SRC-1",
            "--no-chunks",
        ],
    )

    assert no_chunks.exit_code == 0, no_chunks.output
    assert "Source artifact: SRC-1" in no_chunks.output
    assert "CHK-1" not in no_chunks.output
    assert "Source Artifact Chunks" not in no_chunks.output

    as_json = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "source",
            "get",
            "SRC-1",
            "--json",
        ],
    )

    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["source_artifact_id"] == "SRC-1"
    assert payload["chunks"][0]["text"] == "This full text must stay out of human output."


def test_source_get_server_mode_unknown_id_renders_clean_error(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))

    class StubClient:
        def get_source_artifact(self, _instance_id: str, source_artifact_id: str) -> None:
            raise ClientSourceArtifactNotFoundError(source_artifact_id)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "source",
            "get",
            "SRC-MISSING",
        ],
    )

    assert result.exit_code == 1
    assert (
        "Error: SourceArtifactNotFoundError: Source artifact 'SRC-MISSING' not found"
        in result.output
    )
    assert "Traceback" not in result.output
