"""Common providers for document-to-record preparation."""

from __future__ import annotations

import hashlib
import html
import importlib
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from cruxible_core.primitives import canonical_json
from cruxible_core.provider.types import ProviderContext


def document_to_markdown(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Convert lightweight local text-like documents into markdown.

    This provider handles dependency-free formats. Use ``pdf_to_markdown`` for
    PDFs and heavier OCR/layout backends.
    """
    path = _artifact_path(context)
    if path is None:
        raw_text = _string_value(input_payload.get("text"))
        source = "input.text"
        extension = ".txt"
    else:
        raw_text = path.read_text(encoding=str(input_payload.get("encoding", "utf-8")))
        source = path.name
        extension = path.suffix.lower()

    if extension in {".md", ".markdown"}:
        markdown = raw_text
    elif extension in {".txt", ""}:
        markdown = raw_text
    elif extension in {".html", ".htm"}:
        markdown = _html_to_markdown(raw_text)
    else:
        raise ValueError(
            "document_to_markdown supports .md, .markdown, .txt, .html, and .htm. "
            "Use pdf_to_markdown for PDFs."
        )

    return {
        "markdown": markdown,
        "source": {
            "name": source,
            "media_type": _media_type_for_extension(extension),
            "digest": _sha256_text(raw_text),
            **_artifact_summary(context),
        },
        "diagnostics": [],
    }


def pdf_to_markdown(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Convert a PDF to markdown through an explicit backend.

    Supported backends:
    - ``docling``: local document converter for layout-aware PDF parsing.
    - ``pypdf``: local simple page text extraction.
    - ``firecrawl``: hosted Firecrawl document parser, requires a URL.
    """
    backend = str(
        input_payload.get("backend") or context.provider_config.get("backend") or "docling"
    )
    if backend == "docling":
        return _pdf_to_markdown_docling(input_payload, context)
    if backend == "pypdf":
        return _pdf_to_markdown_pypdf(input_payload, context)
    if backend == "firecrawl":
        return _pdf_to_markdown_firecrawl(input_payload, context)
    raise ValueError(f"Unsupported PDF markdown backend: {backend}")


def extract_document_tables(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Extract GitHub-Flavored Markdown pipe tables into row records."""
    markdown = _markdown_input(input_payload, context)
    tables: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    lines = markdown.splitlines()
    index = 0
    table_index = 1
    while index < len(lines) - 1:
        header_line = lines[index]
        separator_line = lines[index + 1]
        if not (_looks_like_table_row(header_line) and _looks_like_separator(separator_line)):
            index += 1
            continue

        headers = [_normalize_header(cell) for cell in _split_table_row(header_line)]
        headers = _deduplicate(headers)
        row_index = index + 2
        rows: list[dict[str, Any]] = []
        while row_index < len(lines) and _looks_like_table_row(lines[row_index]):
            cells = _split_table_row(lines[row_index])
            row = {
                header: cells[column_index].strip() if column_index < len(cells) else ""
                for column_index, header in enumerate(headers)
            }
            rows.append(
                {
                    "_row_id": f"table_{table_index}:{row_index + 1}",
                    "_row_hash": _sha256_json(row),
                    "_source_line": row_index + 1,
                    "_table_index": table_index,
                    **row,
                }
            )
            row_index += 1

        table_name = f"table_{table_index}"
        tables[table_name] = {
            "columns": headers,
            "rows": rows,
            "row_count": len(rows),
            "source": {"start_line": index + 1, "end_line": row_index},
        }
        table_index += 1
        index = row_index

    if not tables:
        diagnostics.append(
            {
                "level": "info",
                "code": "no_markdown_tables_found",
                "message": "No markdown pipe tables were found",
            }
        )

    return {"tables": tables, "diagnostics": diagnostics}


def _pdf_to_markdown_docling(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    path = _require_artifact_path(context, "pdf_to_markdown")
    module = _optional_import(
        "docling.document_converter",
        "docling backend requires installing the docling package",
    )
    converter = module.DocumentConverter()
    conversion_options: dict[str, Any] = {}
    max_pages = input_payload.get("max_pages") or context.provider_config.get("max_pages")
    if isinstance(max_pages, int):
        conversion_options["max_num_pages"] = max_pages
    max_file_size = input_payload.get("max_file_size") or context.provider_config.get(
        "max_file_size"
    )
    if isinstance(max_file_size, int):
        conversion_options["max_file_size"] = max_file_size

    result = converter.convert(path, **conversion_options)
    document = getattr(result, "document", None)
    if document is None:
        raise ValueError("Docling PDF conversion did not return a document")
    markdown = document.export_to_markdown()
    if not isinstance(markdown, str):
        raise ValueError("Docling PDF conversion did not return markdown text")
    return _document_result(markdown, path, backend="docling", context=context)


def _pdf_to_markdown_pypdf(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    path = _require_artifact_path(context, "pdf_to_markdown")
    module = _optional_import(
        "pypdf",
        "pypdf backend requires installing the pypdf package",
    )
    reader = module.PdfReader(str(path))
    pages: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(f"<!-- page:{index} -->\n\n{text.strip()}")
    markdown = "\n\n".join(pages).strip()
    return _document_result(markdown, path, backend="pypdf", context=context)


def _pdf_to_markdown_firecrawl(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    source_url = _string_or_none(input_payload.get("source_url")) or _http_artifact_uri(context)
    if source_url is None:
        raise ValueError(
            "Firecrawl PDF parsing requires input.source_url or an http(s) artifact URI"
        )

    api_key = (
        _string_or_none(input_payload.get("api_key"))
        or _string_or_none(context.provider_config.get("api_key"))
        or os.environ.get("FIRECRAWL_API_KEY")
    )
    if not api_key:
        raise ValueError(
            "Firecrawl PDF parsing requires FIRECRAWL_API_KEY or provider config api_key"
        )

    mode = str(input_payload.get("mode") or context.provider_config.get("mode") or "auto")
    max_pages = input_payload.get("max_pages") or context.provider_config.get("max_pages")
    parser: dict[str, Any] = {"type": "pdf", "mode": mode}
    if isinstance(max_pages, int):
        parser["maxPages"] = max_pages

    base_url = str(context.provider_config.get("base_url") or "https://api.firecrawl.dev")
    timeout_s = float(context.provider_config.get("timeout_s", 120))
    response = httpx.post(
        f"{base_url.rstrip('/')}/v1/scrape",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"url": source_url, "formats": ["markdown"], "parsers": [parser]},
        timeout=timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    markdown = _firecrawl_markdown(payload)
    return {
        "markdown": markdown,
        "source": {
            "name": source_url,
            "media_type": "application/pdf",
            **_artifact_summary(context),
        },
        "backend": {"name": "firecrawl", "mode": mode},
        "diagnostics": [],
    }


def _document_result(
    markdown: str,
    path: Path,
    *,
    backend: str,
    context: ProviderContext,
) -> dict[str, Any]:
    return {
        "markdown": markdown,
        "source": {
            "name": path.name,
            "media_type": "application/pdf",
            "digest": _sha256_file(path),
            **_artifact_summary(context),
        },
        "backend": {"name": backend},
        "diagnostics": [],
    }


def _markdown_input(input_payload: dict[str, Any], context: ProviderContext) -> str:
    markdown = _string_or_none(input_payload.get("markdown"))
    if markdown is not None:
        return markdown
    path = _artifact_path(context)
    if path is not None:
        return path.read_text(encoding=str(input_payload.get("encoding", "utf-8")))
    raise ValueError("extract_document_tables requires input.markdown or a local text artifact")


class _MarkdownHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._list_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "div", "section", "article", "br"}:
            self.parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            self.parts.append("\n" + "#" * level + " ")
        elif tag == "li":
            self.parts.append("\n" + "  " * self._list_depth + "- ")
        elif tag in {"ul", "ol"}:
            self._list_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "section", "article", "li"}:
            self.parts.append("\n")
        elif tag in {"ul", "ol"}:
            self._list_depth = max(0, self._list_depth - 1)

    def handle_data(self, data: str) -> None:
        text = html.unescape(data)
        if text.strip():
            self.parts.append(text)

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _html_to_markdown(raw_text: str) -> str:
    parser = _MarkdownHTMLParser()
    parser.feed(raw_text)
    return parser.markdown()


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and bool(stripped.strip("|").strip())


def _looks_like_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _normalize_header(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or "column"


def _deduplicate(values: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for value in values:
        count = seen.get(value, 0)
        seen[value] = count + 1
        result.append(value if count == 0 else f"{value}_{count + 1}")
    return result


def _optional_import(module_name: str, message: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(message) from exc


def _artifact_path(context: ProviderContext) -> Path | None:
    if context.artifact is None or context.artifact.local_path is None:
        return None
    return Path(context.artifact.local_path)


def _require_artifact_path(context: ProviderContext, provider_name: str) -> Path:
    path = _artifact_path(context)
    if path is None:
        raise ValueError(f"{provider_name} requires a local artifact")
    if not path.exists():
        raise ValueError(f"{provider_name} artifact path does not exist: {path}")
    return path


def _http_artifact_uri(context: ProviderContext) -> str | None:
    if context.artifact is None:
        return None
    uri = context.artifact.uri
    return uri if uri.startswith(("http://", "https://")) else None


def _string_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected string input")
    return value


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _media_type_for_extension(extension: str) -> str:
    if extension in {".md", ".markdown"}:
        return "text/markdown"
    if extension in {".html", ".htm"}:
        return "text/html"
    return "text/plain"


def _artifact_summary(context: ProviderContext) -> dict[str, Any]:
    if context.artifact is None:
        return {}
    return {
        "artifact_name": context.artifact.name,
        "artifact_uri": context.artifact.uri,
        "artifact_digest": context.artifact.digest,
    }


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _sha256_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _sha256_json(value: Any) -> str:
    blob = canonical_json(value)
    return f"sha256:{hashlib.sha256(blob.encode()).hexdigest()}"


def _firecrawl_markdown(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Firecrawl response must be a JSON object")
    candidates = [
        payload.get("markdown"),
        payload.get("data", {}).get("markdown") if isinstance(payload.get("data"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str):
            return candidate
    raise ValueError("Firecrawl response did not include markdown")
