"""The SDK checks every assembled backlinks page against the workspace (review F-004)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.doc_sections import parse_sections, section_digest
from cruxible_client.contracts.get_reads import Backlink, GetBacklinks, GetRequest, GetResult

_CATALOG = """\
catalog_kind: portable
entries:
  - name: repo.notes
    locator: docs/notes.md
"""
_COORDINATE = {
    "git_oid": "a" * 40,
    "semantic_root": "sha256:" + "1" * 64,
    "generation_root": "sha256:" + "2" * 64,
    "compiler_digest": "sha256:" + "3" * 64,
}


def _note(name: str, section: str, cited: str) -> Backlink:
    return Backlink(
        relation="about",
        source_kind="Note",
        source=name,
        file="repo.notes",
        section=(section,),
        cited_digest=cited,
        drift="unknown",
    )


class _TwoPages:
    def __init__(self, first: Backlink, second: Backlink) -> None:
        self.pages = [(first, "next"), (second, None)]
        self.requests: list[GetRequest] = []

    def get(self, instance_id: str, *, request: GetRequest) -> GetResult:
        link, cursor = self.pages[len(self.requests)]
        self.requests.append(request)
        return GetResult(
            ref="inv.package/wi-1",
            kind="subject",
            detail="backlinks",
            backlinks=GetBacklinks(target="inv.package/wi-1", links=(link,)),
            truncated=cursor is not None,
            next_cursor=cursor,
            coordinate={"git_oid": "a" * 12, "generation": 3},
            accepted_coordinate=_COORDINATE,
            evaluation_time=datetime(2026, 11, 17, tzinfo=UTC),
        )


def _sdk(client: Any, workspace: Path | None) -> Cruxible:
    cx = object.__new__(Cruxible)
    cx._client = client
    cx._instance_id = "inst_x"
    cx._workspace = workspace
    cx._pinned = False
    cx._coordinate = None
    cx._clock = lambda: datetime(2026, 11, 17, tzinfo=UTC)
    return cx


def test_a_changed_section_past_the_first_page_reads_changed(tmp_path: Path) -> None:
    (tmp_path / ".cruxible").mkdir()
    (tmp_path / "docs").mkdir()
    text = b"# One\n\nkept\n\n# Two\n\nedited since cited\n"
    (tmp_path / "docs" / "notes.md").write_bytes(text)
    (tmp_path / ".cruxible" / "files.yaml").write_text(_CATALOG)
    kept = section_digest(parse_sections(text, file="notes.md").own_body(("One",)))
    first = _note("NOTE-" + "1" * 32, "One", kept)
    second = _note("NOTE-" + "2" * 32, "Two", "sha256:" + "9" * 64)
    client = _TwoPages(first, second)

    result = _sdk(client, tmp_path)._get("inv.package/wi-1", "backlinks", None, None)

    assert len(client.requests) == 2
    assert result.backlinks is not None and not result.truncated
    assert [link.drift for link in result.backlinks.links] == ["unchanged", "changed_since_cited"]
    # A connection without a workspace keeps the daemon's answer instead of refusing.
    bare = _sdk(_TwoPages(first, second), None)
    answered = bare._get("inv.package/wi-1", "backlinks", None, None)
    assert answered.backlinks is not None
    assert [link.drift for link in answered.backlinks.links] == ["unknown", "unknown"]
