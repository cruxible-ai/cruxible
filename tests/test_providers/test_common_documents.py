"""Tests for common document providers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import cruxible_core.providers.common.documents as documents
from cruxible_core.provider.types import ProviderContext, ResolvedArtifact
from cruxible_core.providers.common.documents import (
    document_to_markdown,
    extract_document_tables,
    pdf_to_markdown,
)


def _context(path: Path | None = None) -> ProviderContext:
    artifact = None
    if path is not None:
        artifact = ResolvedArtifact(
            name="doc",
            kind="file",
            uri=str(path),
            local_path=str(path),
            digest="sha256:test",
        )
    return ProviderContext(
        workflow_name="wf",
        step_id="step",
        provider_name="provider",
        provider_version="1.0.0",
        artifact=artifact,
    )


def test_document_to_markdown_converts_simple_html(tmp_path: Path) -> None:
    source = tmp_path / "brief.html"
    source.write_text("<h1>Title</h1><p>Hello <strong>world</strong>.</p>")

    payload = document_to_markdown({}, _context(source))

    assert "# Title" in payload["markdown"]
    assert "Hello world." in payload["markdown"]
    assert payload["source"]["media_type"] == "text/html"


def test_extract_document_tables_parses_markdown_tables() -> None:
    markdown = """
Intro

| Product | Price |
| --- | ---: |
| Widget | 12.00 |
| Gizmo | 9.50 |
"""

    payload = extract_document_tables({"markdown": markdown}, _context())

    table = payload["tables"]["table_1"]
    assert table["columns"] == ["product", "price"]
    assert table["rows"][0]["product"] == "Widget"
    assert table["rows"][0]["_source_line"] == 6
    assert payload["diagnostics"] == []


def test_pdf_to_markdown_pypdf_extracts_local_pdf(tmp_path: Path) -> None:
    source = tmp_path / "incident.pdf"
    _write_minimal_text_pdf(source, "Incident Report")

    payload = pdf_to_markdown({"backend": "pypdf"}, _context(source))

    assert "Incident Report" in payload["markdown"]
    assert payload["backend"]["name"] == "pypdf"
    assert payload["source"]["media_type"] == "application/pdf"


def test_pdf_to_markdown_docling_uses_document_converter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "incident.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    class FakeDocument:
        def export_to_markdown(self) -> str:
            return "# Parsed Incident"

    class FakeDocumentConverter:
        def convert(self, source_path: Path, **kwargs: object) -> object:
            assert source_path == source
            assert kwargs == {"max_num_pages": 3}
            return SimpleNamespace(document=FakeDocument())

    def fake_optional_import(module_name: str, message: str) -> object:
        assert module_name == "docling.document_converter"
        assert "docling" in message
        return SimpleNamespace(DocumentConverter=FakeDocumentConverter)

    monkeypatch.setattr(documents, "_optional_import", fake_optional_import)

    payload = pdf_to_markdown({"backend": "docling", "max_pages": 3}, _context(source))

    assert payload["markdown"] == "# Parsed Incident"
    assert payload["backend"]["name"] == "docling"


def _write_minimal_text_pdf(path: Path, text: str) -> None:
    escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 18 Tf 72 720 Td ({escaped_text}) Tj ET"
    objects = [
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            "3 0 obj\n"
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
            "endobj\n"
        ),
        (
            "4 0 obj\n"
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n"
            "endobj\n"
        ),
        f"5 0 obj\n<< /Length {len(stream.encode())} >>\nstream\n{stream}\nendstream\nendobj\n",
    ]
    header = "%PDF-1.4\n"
    offsets: list[int] = []
    body = header
    for item in objects:
        offsets.append(len(body.encode()))
        body += item
    xref_start = len(body.encode())
    body += "xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n"
    body += "trailer\n<< /Size 6 /Root 1 0 R >>\n"
    body += f"startxref\n{xref_start}\n%%EOF\n"
    path.write_bytes(body.encode())
