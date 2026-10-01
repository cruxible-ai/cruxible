"""sources/INDEX: one line per evidence source current Claims cite, and every Document."""

from __future__ import annotations

from typing import Any

from cruxible_core.service.floor.floor import service_export_playbill_floor
from tests.core_support._write_support import KIND, report_evidence, seed_write_surface
from tests.test_floor.test_floor_current import NOTE, _add_document, _set, _write

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"


def _rows(files: dict[str, bytes]) -> tuple[str, list[list[str]]]:
    header, *rows = files["sources/INDEX"].decode().splitlines()
    return header, [row.split("\t") for row in rows]


def test_sources_index_lists_evidence_sources_but_not_self_sources(tmp_path: Any) -> None:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    _write(instance, _set(WI1, "title", "Self-evidenced"))
    files = service_export_playbill_floor(instance)
    header, rows = _rows(files)
    # A value the writer stated on its own say is no evidence source.
    assert rows == []
    assert header.startswith("# sources INDEX  0 sources  columns: source, contract, locator")

    workspace = tmp_path / "workspace"
    _write(instance, _set(WI1, "measured", 3, evidence=report_evidence(workspace, "Count: 3")))
    _write(instance, _set(WI2, "measured", 4, evidence=report_evidence(workspace, "Count: 4")))
    _add_document(instance, "design-note", NOTE.encode())
    files = service_export_playbill_floor(instance)
    header, rows = _rows(files)
    by_source = {row[0]: row for row in rows}
    reports = by_source["repo.reports"]
    assert reports[1].endswith("repo.reports")
    assert reports[3] == "2"
    note = by_source["Document:design-note"]
    assert note[3] == "0"
    manifest_rows = {
        row["path"]: row for row in __import__("json").loads(files["manifest.json"])["files"]
    }
    assert int(header.rsplit(" ", 1)[-1]) == manifest_rows["sources/INDEX"]["changed_at"]
    # The index carries no digests and no evidence bytes.
    assert b"sha256:" not in files["sources/INDEX"]
    assert b"Count: 3" not in files["sources/INDEX"]


def test_the_client_joins_its_workspace_bindings_into_projections_index(tmp_path: Any) -> None:
    from cruxible_client.authoring.workspace import materialize_playbill_floor
    from tests.test_floor.test_floor_current import _export_envelope

    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    workspace = tmp_path / "workspace"
    _write(instance, _set(WI1, "measured", 3, evidence=report_evidence(workspace, "Count: 3")))
    files = service_export_playbill_floor(instance)
    _header, rows = _rows(files)
    (reports,) = (row for row in rows if row[0] == "repo.reports")
    materialize_playbill_floor(workspace, export=_export_envelope(files))
    joined = (workspace / ".playbill/floor/projections/INDEX").read_text(encoding="utf-8")
    header, *lines = joined.splitlines()
    assert header.startswith("# projections INDEX  1 bindings")
    assert lines == [f"reports.md\tevidence-source\trepo.reports\t{reports[4]}"]
    # The joined file is the client's own: the daemon manifest never lists it,
    # and an unchanged floor is still exactly the floor it verified.
    assert "projections/INDEX" not in files
    again = materialize_playbill_floor(workspace, export=_export_envelope(files), force=False)
    assert again.status == "unchanged"
