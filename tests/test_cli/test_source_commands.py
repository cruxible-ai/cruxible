"""CLI tests for source artifact commands."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

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
    payload = json.loads(result.output)
    assert payload["source_artifact_id"].startswith("SRC-")
    assert payload["source_kind"] == "markdown"
    assert payload["source_retention"] == "manifest_only"
    assert payload["original_uri"] == "https://example.test/evidence.md"
    assert payload["label"] == "workspace evidence"
    assert payload["archived"] is False
    assert any(
        chunk["heading_path"] == ["Evidence"]
        and chunk["block_selector"] == "paragraph:1"
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
    registered: dict[str, Any] = json.loads(register.output)
    paragraph_chunk = next(
        chunk
        for chunk in registered["chunks"]
        if chunk["block_selector"] == "paragraph:1"
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
