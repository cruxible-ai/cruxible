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
    (workspace / ".cruxible" / "sources.local.yaml").write_text(
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
    floor = workspace / ".cruxible/floor"
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
    _header, rows = _rows((workspace / ".cruxible/floor/sources/INDEX").read_bytes())
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
    floor = tmp_path / ".cruxible" / "floor"
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


def _source_claims(count: int) -> tuple[dict[str, Any], dict[str, int]]:
    from types import SimpleNamespace

    claims: dict[str, Any] = {}
    for index in range(count):
        claims[f"claims/c{index}.json"] = SimpleNamespace(
            lifecycle=SimpleNamespace(state="live"),
            identity=SimpleNamespace(name=f"c{index}"),
            digest="sha256:a",
        )
    claims["claims/b.json"] = SimpleNamespace(
        lifecycle=SimpleNamespace(state="live"),
        identity=SimpleNamespace(name="b"),
        digest="sha256:b",
    )
    return claims, {path: 1 for path in claims}


def _contracts(**labels: str) -> dict[str, Any]:
    from types import SimpleNamespace

    return {
        f"sha256:{key}": SimpleNamespace(identity=SimpleNamespace(name=label))
        for key, label in labels.items()
    }


def test_a_shared_source_line_moves_with_its_change_not_its_citing_claims(
    monkeypatch: Any,
) -> None:
    from cruxible_core.derived import persistent
    from cruxible_core.service.floor import floor_sources

    read: list[str] = []

    def sources(_instance: Any, claim: Any, contracts: Any) -> Any:
        read.append(claim.identity.name)
        contract = contracts.get(claim.digest)
        label = "-" if contract is None else contract.identity.name
        return ((("shared.source", label, "-"),), frozenset({claim.digest}))

    monkeypatch.setattr(floor_sources, "_claim_sources", sources)
    walked: dict[int, int] = {}
    for size in (64, 512):
        claims, latest = _source_claims(size)
        contracts = _contracts(a="contract.a", b="contract.b")
        state = floor_sources.update_sources_ledger(
            None,  # type: ignore[arg-type]
            floor_sources.EMPTY_SOURCES_LEDGER,
            claims=claims,
            latest=latest,
            contracts=contracts,
            documents=(),
            changed_claims=list(claims),
            changed_documents=(),
        )
        leaves = [0]
        original = persistent._leaves

        def counting(node: Any, _original: Any = original, _leaves: list[int] = leaves) -> Any:
            for leaf in _original(node):
                _leaves[0] += 1
                yield leaf

        monkeypatch.setattr(persistent, "_leaves", counting)
        read.clear()
        # One citing Claim revised at a newer generation: its line moves.
        latest = {**latest, "claims/c7.json": 9}
        moved = floor_sources.update_sources_ledger(
            None,  # type: ignore[arg-type]
            state,
            claims=claims,
            latest=latest,
            contracts=contracts,
            documents=(),
            changed_claims=["claims/c7.json"],
            changed_documents=(),
        )
        assert read == ["c7"]
        assert moved.rows["shared.source"].split("\t")[3:] == [str(size + 1), "9"]
        # That Claim retired: the line's newest generation goes back, from counts.
        retired = floor_sources.update_sources_ledger(
            None,  # type: ignore[arg-type]
            moved,
            claims={key: value for key, value in claims.items() if key != "claims/c7.json"},
            latest=latest,
            contracts=contracts,
            documents=(),
            changed_claims=["claims/c7.json"],
            changed_documents=(),
        )
        monkeypatch.setattr(persistent, "_leaves", original)
        assert retired.rows["shared.source"].split("\t")[3:] == [str(size), "1"]
        walked[size] = leaves[0]
    # No citing Claim is read or walked to move the line: the same work at any size.
    assert walked[64] == walked[512]


def test_a_contract_change_rereads_only_the_claims_taken_under_it(monkeypatch: Any) -> None:
    from cruxible_core.service.floor import floor_sources

    read: list[str] = []

    def sources(_instance: Any, claim: Any, contracts: Any) -> Any:
        read.append(claim.identity.name)
        contract = contracts.get(claim.digest)
        label = "-" if contract is None else contract.identity.name
        return (((f"source.{claim.digest[-1]}", label, "-"),), frozenset({claim.digest}))

    monkeypatch.setattr(floor_sources, "_claim_sources", sources)
    claims, latest = _source_claims(64)
    state = floor_sources.update_sources_ledger(
        None,  # type: ignore[arg-type]
        floor_sources.EMPTY_SOURCES_LEDGER,
        claims=claims,
        latest=latest,
        contracts=_contracts(a="contract.a", b="contract.b"),
        documents=(),
        changed_claims=list(claims),
        changed_documents=(),
    )
    read.clear()
    relabelled = floor_sources.update_sources_ledger(
        None,  # type: ignore[arg-type]
        state,
        claims=claims,
        latest=latest,
        contracts=_contracts(a="contract.a", b="contract.b2"),
        documents=(),
        changed_claims=(),
        changed_documents=(),
        changed_contracts=["sha256:b"],
    )
    # The 64 Claims under the unrelated contract are not read again.
    assert read == ["b"]
    assert relabelled.rows["source.b"].split("\t")[1] == "contract.b2"
    assert relabelled.rows["source.a"] is state.rows["source.a"]
    # The contract gone: its Claim's line shows no label, still re-read alone.
    read.clear()
    dropped = floor_sources.update_sources_ledger(
        None,  # type: ignore[arg-type]
        relabelled,
        claims=claims,
        latest=latest,
        contracts=_contracts(a="contract.a"),
        documents=(),
        changed_claims=(),
        changed_documents=(),
        changed_contracts=["sha256:b"],
    )
    assert read == ["b"] and dropped.rows["source.b"].split("\t")[1] == "-"
