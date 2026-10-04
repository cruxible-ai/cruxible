"""sources/LEDGER and sources/INDEX: one line per evidence source, wherever it lives.

The daemon renders ``sources/LEDGER`` from accepted state alone (a Document ref,
an external selector, or ``-``); the client joins its own catalog's workspace
paths in and writes ``sources/INDEX`` outside the daemon manifest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cruxible_core.service.floor.floor import service_export_playbill_floor
from tests.core_support._write_support import KIND, report_evidence, seed_write_surface
from tests.test_floor.test_floor_current import NOTE, _add_document, _set, _write

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"


def _rows(content: bytes) -> tuple[str, list[list[str]]]:
    header, *rows = content.decode().splitlines()
    return header, [row.split("\t") for row in rows]


def test_the_ledger_lists_evidence_sources_but_not_self_sources(tmp_path: Any) -> None:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    _write(instance, _set(WI1, "title", "Self-evidenced"))
    files = service_export_playbill_floor(instance)
    assert "sources/INDEX" not in files  # the client's own file, never the daemon's
    header, rows = _rows(files["sources/LEDGER"])
    # A value the writer stated on its own say is no evidence source.
    assert rows == []
    assert header.startswith("# sources LEDGER  0 sources  columns: source, contracts")

    workspace = tmp_path / "workspace"
    _write(instance, _set(WI1, "measured", 3, evidence=report_evidence(workspace, "Count: 3")))
    _write(instance, _set(WI2, "measured", 4, evidence=report_evidence(workspace, "Count: 4")))
    _add_document(instance, "design-note", NOTE.encode())
    files = service_export_playbill_floor(instance)
    header, rows = _rows(files["sources/LEDGER"])
    by_source = {row[0]: row for row in rows}
    assert len(by_source) == len(rows)
    reports = by_source["repo.reports"]
    assert reports[1].endswith("repo.reports")
    # A foreign source's coordinate is a content digest: the ledger cannot locate it.
    assert reports[2] == "-"
    assert reports[3] == "2"
    head = str(instance.accepted_history()[-1].sequence)
    assert by_source["design-note"] == ["design-note", "-", "Document:design-note", "0", head]
    manifest_rows = {row["path"]: row for row in json.loads(files["manifest.json"])["files"]}
    assert int(header.rsplit(" ", 1)[-1]) == manifest_rows["sources/LEDGER"]["changed_at"]
    # The ledger carries no digests and no evidence bytes.
    assert b"sha256:" not in files["sources/LEDGER"]
    assert b"Count: 3" not in files["sources/LEDGER"]


def test_a_source_and_the_document_of_its_name_are_one_line(tmp_path: Any) -> None:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    workspace = tmp_path / "workspace"
    _write(instance, _set(WI1, "measured", 3, evidence=report_evidence(workspace, "Count: 3")))
    _add_document(instance, "repo.reports", NOTE.encode())
    _header, rows = _rows(service_export_playbill_floor(instance)["sources/LEDGER"])
    (line,) = rows
    source, contracts, locator, citing, changed = line
    assert (source, locator, citing) == ("repo.reports", "Document:repo.reports", "1")
    assert contracts.endswith("repo.reports")
    # The line changed when the Document arrived, after the Claim citing it.
    assert int(changed) == instance.accepted_history()[-1].sequence


def test_the_client_joins_workspace_paths_into_sources_and_projections(tmp_path: Any) -> None:
    from cruxible_client.authoring.workspace import materialize_floor
    from tests.test_floor.test_floor_current import _export_envelope

    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    workspace = tmp_path / "workspace"
    _write(instance, _set(WI1, "measured", 3, evidence=report_evidence(workspace, "Count: 3")))
    _add_document(instance, "reports", NOTE.encode())
    _add_document(instance, "design-note", NOTE.encode())
    # A local catalog entry whose file is gone.
    (workspace / ".playbill" / "sources.local.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: local\n"
        "entries:\n"
        "  - name: design-note\n"
        "    locator: notes/design.md\n"
        "    document_id: design-note\n"
        "    document_kind: design\n"
        "    title: Design note\n"
        "    media_type: text/markdown\n"
        "    governance_scope: [dev]\n",
        encoding="utf-8",
    )
    files = service_export_playbill_floor(instance)
    _header, ledger = _rows(files["sources/LEDGER"])
    materialize_floor(workspace, export=_export_envelope(files))
    floor = workspace / ".playbill/floor"
    header, rows = _rows((floor / "sources/INDEX").read_bytes())
    assert header.startswith("# sources INDEX  3 sources  columns: source, contracts, locator")
    assert header.endswith("(locators joined from the local workspace catalog)")
    located = {row[0]: row[2] for row in rows}
    assert located == {
        # By source name; the Document by its catalog document_id; a missing file marked.
        "repo.reports": "reports.md",
        "reports": "reports.md",
        "design-note": "notes/design.md (missing)",
    }
    # Every other cell is the ledger's.
    assert [row[:2] + row[3:] for row in rows] == [row[:2] + row[3:] for row in ledger]

    joined = (floor / "projections/INDEX").read_text(encoding="utf-8")
    projection_header, *lines = joined.splitlines()
    assert projection_header.startswith("# projections INDEX  2 bindings")
    generation = {row[0]: row[4] for row in ledger}
    assert lines == [
        f"reports.md\tdocument-body\tDocument:reports\t{generation['reports']}",
        f"reports.md\tevidence-source\trepo.reports\t{generation['repo.reports']}",
    ]
    # Both joined files are the client's own: the daemon manifest never lists
    # them, and an unchanged floor is still exactly the floor it verified.
    assert "projections/INDEX" not in files and "sources/INDEX" not in files
    again = materialize_floor(workspace, export=_export_envelope(files), force=False)
    assert again.status == "unchanged"


def test_without_a_catalog_the_index_keeps_the_ledger_locators(tmp_path: Any) -> None:
    from cruxible_client.authoring.workspace import materialize_floor
    from tests.test_floor.test_floor_current import _export_envelope

    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    _add_document(instance, "design-note", NOTE.encode())
    files = service_export_playbill_floor(instance)
    workspace = tmp_path / "bare"
    workspace.mkdir()
    materialize_floor(workspace, export=_export_envelope(files))
    _header, rows = _rows((workspace / ".playbill/floor/sources/INDEX").read_bytes())
    assert rows == _rows(files["sources/LEDGER"])[1]


def test_a_floor_whose_manifest_lists_sources_index_is_replaced_whole(tmp_path: Path) -> None:
    """A floor installed before sources/INDEX became the client's (renderer v5.2)
    names it in its manifest; that manifest no longer reads, so the next sync
    asks for the whole floor, and the old file is the client's to rewrite."""

    from cruxible_client.authoring.floor_apply import apply_floor_delta, read_floor_manifest
    from cruxible_client.authoring.workspace import write_projection_index
    from cruxible_client.contracts.floor import floor_manifest_digest
    from tests.support.floor_exports import floor_v5_delta
    from tests.test_client.test_floor_apply import COORDINATE

    head = {
        "README.md": (b"# floor\n", 0),
        "sources/LEDGER": (b"# sources LEDGER  0 sources  changed gen 0\n", 0),
    }
    old = floor_v5_delta(
        {"README.md": (b"# floor\n", 0), "sources/OLDNAME": (b"old\n", 0)},
        coordinate=COORDINATE,
        generation=4,
    )
    floor = tmp_path / ".playbill" / "floor"
    assert apply_floor_delta(floor, old).status == "applied"
    # Rewrite it as the old renderer left it: sources/INDEX in the manifest.
    (floor / "sources/OLDNAME").rename(floor / "sources/INDEX")
    manifest = (floor / "manifest.json").read_text(encoding="utf-8")
    (floor / "manifest.json").write_text(
        manifest.replace("sources/OLDNAME", "sources/INDEX"), encoding="utf-8"
    )
    assert read_floor_manifest(floor) is None

    full = floor_v5_delta(head, coordinate=COORDINATE, generation=5)
    assert apply_floor_delta(floor, full).status == "applied"
    installed = read_floor_manifest(floor)
    assert installed is not None and floor_manifest_digest(installed) == full.head_manifest_digest
    assert write_projection_index(tmp_path) == 0
    assert (floor / "sources/INDEX").read_text(encoding="utf-8").startswith("# sources INDEX  0")
