#!/usr/bin/env python3
"""Acquisition tool: fetch opinion clusters from the CourtListener REST API.

This script lives OUTSIDE the kit's providers on purpose. Acquisition is not
workflow logic: fetching is nondeterministic, so it happens out here, and the
result crosses into Cruxible at the artifact seam — write the JSON, review it,
register opinion texts as source artifacts, then feed the corpus rows to the
``sync_corpus_update`` workflow, whose contract this output matches.

Usage:
    python scripts/fetch_courtlistener.py --cluster-id 10600041 -o update.json
    python scripts/fetch_courtlistener.py --docket-id 68041951 -o update.json

Auth: set COURTLISTENER_API_TOKEN, or place the key in
``~/.cruxible/courtlistener-api-key``. Anonymous access works at a low rate
limit.

Recipe (agent or operator):
    1. Run this script; inspect the JSON it wrote.
    2. Register each opinion text for evidence dereferencing:
       cruxible source register <text-file> --id opinion_text_<opinion_id>
    3. Apply the reviewed rows:
       cruxible workflow run sync_corpus_update --input-file update.json
    4. Run the treatment/impact proposal chain to see the law change propagate.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

CORPUS_FIELDS = (
    "opinions",
    "courts",
    "judges",
    "statutes",
    "legal_issues",
    "clients",
    "matters",
    "arguments",
    "opinion_texts",
    "opinion_from_court_edges",
    "opinion_decided_by_judge_edges",
    "opinion_cites_opinion_edges",
    "matter_for_client_edges",
    "matter_in_jurisdiction_edges",
    "argument_in_matter_edges",
    "argument_raises_issue_edges",
    "argument_cites_opinion_edges",
    "statute_governs_issue_edges",
)


class _PlainTextHTMLParser(HTMLParser):
    """Small HTML-to-text fallback for CourtListener opinion HTML."""

    _BLOCK_TAGS = {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
        "tr",
    }
    _SKIP_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self._parts))
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        raw = re.sub(r"[ \t\f\v]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip() + "\n"


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "unknown"


def _sorted_rows(rows: Iterable[Mapping[str, Any]], *keys: str) -> list[dict[str, Any]]:
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: tuple(str(row.get(key) or "") for key in keys),
    )


def _corpus_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in CORPUS_FIELDS:
        value = data.get(field, [])
        if not isinstance(value, list):
            raise ValueError(f"corpus seed field '{field}' must be a list")
        payload[field] = [dict(row) for row in value]
    return payload


def fetch_courtlistener_corpus(
    cluster_ids: list[str],
    base_url: str,
    *,
    docket_ids: list[str],
) -> dict[str, Any]:
    base = base_url.rstrip("/")
    cluster_ids = list(dict.fromkeys(cluster_ids))
    for docket_id in docket_ids:
        cluster_ids.extend(_fetch_docket_cluster_ids(docket_id, base))
    cluster_ids = list(dict.fromkeys(cluster_ids))
    opinions: list[dict[str, Any]] = []
    courts: dict[str, dict[str, Any]] = {}
    judges: dict[str, dict[str, Any]] = {}
    opinion_court_edges: list[dict[str, Any]] = []
    opinion_judge_edges: list[dict[str, Any]] = []
    opinion_texts: list[dict[str, Any]] = []
    for cluster_id in cluster_ids:
        data = _fetch_json(f"{base}/clusters/{cluster_id}/")
        opinion_id = f"cl_cluster_{cluster_id}"
        citation = _cluster_citation(data)
        court_id = _first_non_empty(data.get("court"), data.get("court_id")) or "court_unknown"
        court_id = _slugify(court_id)
        courts[court_id] = {
            "court_id": court_id,
            "name": _first_non_empty(data.get("court_name"), data.get("court")) or "Unknown Court",
            "jurisdiction": _first_non_empty(data.get("jurisdiction")) or "unknown",
            "level": None,
        }
        opinions.append({
            "opinion_id": opinion_id,
            "case_name": _first_non_empty(data.get("case_name"), data.get("caseName")) or opinion_id,
            "citation": citation,
            "docket_number": _first_non_empty(data.get("docket_number")),
            "date_filed": _first_non_empty(data.get("date_filed"), data.get("dateFiled")),
            "jurisdiction": courts[court_id]["jurisdiction"],
            "precedential_status": "published",
            "source_url": _absolute_courtlistener_url(data.get("absolute_url")),
        })
        text_row = _fetch_cluster_opinion_text(base, data, opinion_id=opinion_id)
        if text_row is not None:
            opinion_texts.append(text_row)
        opinion_court_edges.append({"opinion_id": opinion_id, "court_id": court_id})
        for judge_name in _cluster_judges(data):
            judge_id = f"judge_{_slugify(judge_name)}"
            judges[judge_id] = {"judge_id": judge_id, "name": judge_name, "court_hint": court_id}
            opinion_judge_edges.append({
                "opinion_id": opinion_id,
                "judge_id": judge_id,
                "role": "author",
            })
    return _corpus_payload({
        "opinions": _sorted_rows(opinions, "opinion_id"),
        "courts": _sorted_rows(courts.values(), "court_id"),
        "judges": _sorted_rows(judges.values(), "judge_id"),
        "opinion_texts": _sorted_rows(opinion_texts, "opinion_id"),
        "opinion_from_court_edges": _sorted_rows(opinion_court_edges, "opinion_id", "court_id"),
        "opinion_decided_by_judge_edges": _sorted_rows(
            opinion_judge_edges,
            "opinion_id",
            "judge_id",
        ),
    })


def _fetch_docket_cluster_ids(docket_id: str, base: str) -> list[str]:
    data = _fetch_json(f"{base}/clusters/?docket={docket_id}")
    candidates = data.get("results") if isinstance(data.get("results"), list) else data.get("clusters", [])
    if not isinstance(candidates, list):
        raise ValueError(f"CourtListener docket cluster response was not a list for docket {docket_id}")
    cluster_ids: list[str] = []
    for candidate in candidates:
        cluster_id = _cluster_id_from_candidate(candidate)
        if cluster_id:
            cluster_ids.append(cluster_id)
    if not cluster_ids:
        raise ValueError(f"CourtListener docket {docket_id} did not include opinion clusters")
    return cluster_ids


def _cluster_id_from_candidate(candidate: Any) -> str | None:
    if isinstance(candidate, Mapping):
        text = _first_non_empty(candidate.get("id"), candidate.get("cluster_id"), candidate.get("resource_uri"))
    else:
        text = _first_non_empty(candidate)
    if not text:
        return None
    return text.rstrip("/").rsplit("/", 1)[-1]


def _fetch_cluster_opinion_text(
    base: str,
    cluster: Mapping[str, Any],
    *,
    opinion_id: str,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any], str, str]] = []
    for url in _cluster_opinion_urls(base, cluster):
        opinion = _fetch_json(url)
        text, source_field = _opinion_text(opinion)
        if text.strip():
            candidates.append((len(text), opinion, text, source_field))
    if not candidates:
        return None
    _size, opinion, text, source_field = max(candidates, key=lambda item: item[0])
    encoded = text.encode("utf-8")
    cluster_id = _first_non_empty(cluster.get("id"))
    source_url = _absolute_courtlistener_url(cluster.get("absolute_url"))
    return {
        "opinion_id": opinion_id,
        "courtlistener_opinion_id": _first_non_empty(opinion.get("id")),
        "cluster_id": cluster_id,
        "source_url": source_url,
        "plain_text": text,
        "text_sha256": _sha256_text(text),
        "byte_count": len(encoded),
        "text_size": len(text),
        "text_source_field": source_field,
        "source_artifact_id": f"opinion_text_{opinion_id}",
    }


def _cluster_opinion_urls(base: str, cluster: Mapping[str, Any]) -> list[str]:
    urls = cluster.get("sub_opinions")
    if isinstance(urls, list) and urls:
        return [str(url) for url in urls if _first_non_empty(url)]
    cluster_id = _first_non_empty(cluster.get("id"))
    if not cluster_id:
        return []
    data = _fetch_json(f"{base}/opinions/?cluster={cluster_id}")
    results = data.get("results") if isinstance(data.get("results"), list) else []
    opinion_urls: list[str] = []
    for row in results:
        if not isinstance(row, Mapping):
            continue
        url = _first_non_empty(row.get("resource_uri"))
        if not url and (opinion_id := _first_non_empty(row.get("id"))):
            url = f"{base}/opinions/{opinion_id}/"
        if url:
            opinion_urls.append(url)
    return opinion_urls


def _opinion_text(opinion: Mapping[str, Any]) -> tuple[str, str]:
    plain_text = _first_non_empty(opinion.get("plain_text"))
    if plain_text:
        return _normalize_line_endings(plain_text).strip() + "\n", "plain_text"
    for field in ("html", "html_lawbox", "html_columbia", "html_anon_2020", "html_with_citations"):
        value = opinion.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        parser = _PlainTextHTMLParser()
        parser.feed(value)
        text = parser.text()
        if text.strip():
            return text, field
    return "", "missing"


def _normalize_line_endings(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_courtlistener_headers())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except TimeoutError as exc:
        raise TimeoutError(f"CourtListener request timed out after 10s: {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CourtListener request failed for {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"CourtListener response was not valid JSON for {url}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"CourtListener response was not a JSON object for {url}")
    return data


def _courtlistener_headers(token_path: Path | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = _first_non_empty(os.environ.get("COURTLISTENER_API_TOKEN"))
    if token is None:
        if token_path is None:
            token_path = Path.home() / ".cruxible" / "courtlistener-api-key"
        try:
            token = _first_non_empty(token_path.read_text())
        except OSError:
            token = None
    if token is not None:
        headers["Authorization"] = f"Token {token}"
    return headers


def _cluster_citation(data: Mapping[str, Any]) -> str | None:
    citations = data.get("citations")
    if isinstance(citations, list):
        for row in citations:
            if not isinstance(row, Mapping):
                continue
            volume = _first_non_empty(row.get("volume"))
            reporter = _first_non_empty(row.get("reporter"))
            page = _first_non_empty(row.get("page"))
            if volume and reporter and page:
                return f"{volume} {reporter} {page}"
    return _first_non_empty(data.get("citation"), data.get("federal_cite_one"))


def _cluster_judges(data: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("judges", "panel_names", "panel"):
        value = data.get(key)
        if isinstance(value, str):
            names.extend(_split_judge_text(value))
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, str):
                    names.extend(_split_judge_text(row))
                elif isinstance(row, Mapping):
                    name = _first_non_empty(row.get("name_full"), row.get("name"))
                    if name:
                        names.append(name)
        if names:
            break
    return list(dict.fromkeys(name for name in names if name))


def _split_judge_text(value: str) -> list[str]:
    parts = re.split(r"[;,]| and ", value)
    return [part.strip() for part in parts if part.strip()]


def _absolute_courtlistener_url(value: Any) -> str | None:
    text = _first_non_empty(value)
    if text is None:
        return None
    if text.startswith("http"):
        return text
    return f"https://www.courtlistener.com{text}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cluster-id", action="append", default=[], dest="cluster_ids",
                        help="CourtListener opinion cluster id (repeatable)")
    parser.add_argument("--docket-id", action="append", default=[], dest="docket_ids",
                        help="CourtListener docket id; all its clusters are fetched (repeatable)")
    parser.add_argument("--base-url", default="https://www.courtlistener.com/api/rest/v4")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Write corpus JSON here (default: stdout)")
    args = parser.parse_args()
    if not args.cluster_ids and not args.docket_ids:
        parser.error("provide at least one --cluster-id or --docket-id")
    corpus = fetch_courtlistener_corpus(
        [str(c) for c in args.cluster_ids],
        args.base_url,
        docket_ids=[str(d) for d in args.docket_ids],
    )
    rendered = json.dumps(corpus, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered)
        opinion_count = len(corpus.get("opinions", []))
        text_count = len(corpus.get("opinion_texts", []))
        sys.stderr.write(
            f"wrote {args.output} ({opinion_count} opinions, {text_count} opinion texts)\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
