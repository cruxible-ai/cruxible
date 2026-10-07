"""MCP workspace adapters derive source and coverage wire from local bytes."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.source_catalog import SourceCatalog, SourceCatalogEntry
from cruxible_core.errors import DataValidationError
from cruxible_core.mcp import handlers


def _coordinate() -> contracts.AcceptedCoordinate:
    return contracts.AcceptedCoordinate(
        git_oid="1" * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def _write_catalog(workspace: Path) -> None:
    catalog = SourceCatalog(
        catalog_kind="portable",
        entries=(
            SourceCatalogEntry(
                name="decision",
                locator="docs/decision.md",
                document_id="decision",
                document_kind="decision",
                title="Decision",
                media_type="text/markdown",
                governance_scope=("project:playbill",),
            ),
        ),
    )
    (workspace / "catalog.json").write_text(catalog.model_dump_json(), encoding="utf-8")


def test_workspace_source_compile_and_check_read_bytes_and_derive_bundle(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs/decision.md").write_text("status: ready\n", encoding="utf-8")
    _write_catalog(workspace)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    checked: dict[str, Any] = {}

    class StubClient:
        def source_context(self, instance_id: str) -> contracts.SourceContext:
            return contracts.SourceContext(
                accepted_coordinate=_coordinate(),
                documents=[],
            )

        def check_source_bundle(
            self,
            instance_id: str,
            *,
            bundle: dict[str, Any],
        ) -> contracts.SourceCheckResult:
            checked.update(bundle)
            return contracts.SourceCheckResult(
                compilation_digest=bundle["manifest"]["compilation_digest"],
                accepted_coordinate=_coordinate(),
                alignments=[],
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    bundle = handlers.handle_playbill_workspace_source_compile(
        "inst_test",
        catalog_path="catalog.json",
        repository_root=".",
        local_catalog_path=None,
        root_aliases={},
    )
    result = handlers.handle_playbill_source_check("inst_test", catalog_path="catalog.json")

    assert base64.b64decode(bundle.documents[0].body_base64) == b"status: ready\n"
    assert result.compilation_digest == bundle.manifest.compilation_digest
    assert checked["manifest"]["compilation_digest"] == bundle.manifest.compilation_digest
    assert "repository_root" not in bundle.model_dump_json()


def test_workspace_coverage_derives_observations_from_decision_bearing_selection(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "docs/decision.md"
    source.parent.mkdir(parents=True)
    source.write_text("first\nstatus: ready\nthird\n", encoding="utf-8")
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    captured: list[dict[str, Any]] = []

    class StubClient:
        def resolve_coverage(
            self,
            instance_id: str,
            *,
            observations: list[dict[str, Any]],
            budget: dict[str, Any] | None,
            scan_budget: dict[str, Any] | None,
        ) -> contracts.CoverageResult:
            captured.extend(observations)
            return contracts.CoverageResult(
                coordinate=_coordinate(),
                result={"tag": "playbill-coverage-result-v2", "spans": []},
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    handlers.handle_playbill_coverage(
        "inst_test",
        bindings={"docs/decision.md": "external:workspace.decision"},
        ranges=("docs/decision.md:2-2",),
    )
    # The same selection as inline grep output: no saved file, no path.
    handlers.handle_playbill_coverage(
        "inst_test",
        bindings={"docs/decision.md": "external:workspace.decision"},
        grep_results="docs/decision.md:2:status: ready\n",
    )

    assert len(captured) == 2
    assert captured[1]["selections"] == captured[0]["selections"]
    assert captured[0]["source"] == {
        "tag": "playbill-logical-source-identity-v1",
        "plane": "external",
        "identity": "workspace.decision",
    }
    assert base64.b64decode(captured[0]["content_base64"]) == source.read_bytes()
    assert captured[0]["selections"][0]["start_byte"] == len(b"first\n")
    assert "content_digest" in captured[0]


def test_workspace_coverage_status_observes_every_declared_binding(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.md").write_text("one\n", encoding="utf-8")
    (workspace / "two.md").write_text("two\n", encoding="utf-8")
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    counts: list[int] = []

    class StubClient:
        def resolve_coverage(
            self,
            instance_id: str,
            *,
            observations: list[dict[str, Any]],
            budget: dict[str, Any] | None,
            scan_budget: dict[str, Any] | None,
        ) -> contracts.CoverageResult:
            counts.append(len(observations))
            return contracts.CoverageResult(
                coordinate=_coordinate(),
                result={"tag": "playbill-coverage-result-v2", "spans": []},
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    handlers.handle_playbill_coverage(
        "inst_test",
        bindings={
            "one.md": "external:one",
            "two.md": "ledger:two",
        },
        whole_working_set=True,
    )

    assert counts == [2]


def test_merged_tools_take_exactly_one_input_shape() -> None:
    with pytest.raises(DataValidationError, match="exactly one of observations or bindings"):
        handlers.handle_playbill_coverage("inst_test")
    with pytest.raises(DataValidationError, match="exactly one of observations or bindings"):
        handlers.handle_playbill_coverage("inst_test", observations=[], bindings={})
    with pytest.raises(DataValidationError, match="apply only with bindings"):
        handlers.handle_playbill_coverage("inst_test", observations=[], whole_working_set=True)
    with pytest.raises(DataValidationError, match="exactly one of bundle or catalog_path"):
        handlers.handle_playbill_source_check("inst_test")
    with pytest.raises(DataValidationError, match="apply only with catalog_path"):
        handlers.handle_playbill_source_check("inst_test", bundle={}, root_aliases={"a": "b"})
    with pytest.raises(DataValidationError, match="force applies only"):
        handlers.handle_playbill_floor_export("inst_test", mode="bytes", force=True)
