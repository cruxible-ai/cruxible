"""Client-only declared-block bootstrap, backing refresh, and whole-source CAS laws."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client import Playbill
from cruxible_client import contracts as api
from cruxible_client.authoring.blocks import (
    ProjectionRepinError,
    parse_projection_blocks,
    repin_projection_block,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claims import ClaimStatement, LiteralClaimObject
from cruxible_client.contracts.declared_blocks import ProjectionQueryBackingV1
from cruxible_client.contracts.semantic import SemanticAddress

COORDINATE = api.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
BODY = b"reflects generation 7\nstatus: ready --> preserve this prose\n"


def _workspace(root: Path) -> Path:
    (root / ".playbill").mkdir()
    (root / ".playbill" / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: portable\n"
        "entries:\n"
        "  - name: corpus.runbook\n"
        "    locator: runbook.md\n"
        "    document_id: runbook\n"
        "    document_kind: runbook\n"
        "    title: Runbook\n"
        "    media_type: text/markdown\n"
        "    governance_scope: [Document:runbook]\n"
    )
    source = root / "runbook.md"
    source.write_bytes(
        b"prefix\n<!-- playbill:block:summary -->\n"
        + BODY
        + b"<!-- /playbill:block:summary -->\nsuffix\n"
    )
    return source


class _RepinClient:
    def __init__(self) -> None:
        self.query_verdict = "completed"
        self.clipped_budgets: list[str] = []
        self.on_claim = lambda: None

    def search_playbill(self, _instance_id: str, **values: Any) -> api.PlaybillSearchResult:
        return api.PlaybillSearchResult(
            mode=values["mode"],
            coordinate=COORDINATE,
            evaluation_time=str(values["evaluation_time"]),
            rows=[],
            orientation={"generation": 7} if values["mode"] == "orient" else None,
            selection_basis_digest="sha256:" + "6" * 64,
            truncated=False,
            result_digest="sha256:" + "7" * 64,
        )

    def get_playbill_claim(
        self,
        _instance_id: str,
        name: str,
        **_values: Any,
    ) -> SimpleNamespace:
        self.on_claim()
        statement = ClaimStatement(
            subject=SemanticAddress.whole_artifact("subjects/project.work_item/wi-42.json"),
            claim_type=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
            claim_type_digest="sha256:" + "8" * 64,
            predicate="project.work_item.status",
            object=LiteralClaimObject(value="ready"),
            role="observation",
        )
        return SimpleNamespace(
            coordinate=COORDINATE,
            envelope={"identity": f"Claim:{name}"},
            facts=[
                {
                    "schema_id": "playbill.claim.statement",
                    "value": statement.model_dump(mode="json"),
                },
                {
                    "schema_id": "playbill.claim.lifecycle",
                    "value": {"lifecycle": {"state": "live"}},
                },
            ],
        )

    def run_playbill_query(
        self,
        _instance_id: str,
        name: str,
        **values: Any,
    ) -> SimpleNamespace:
        parameters = [
            {
                "tag": "playbill-query-parameter-binding-v1",
                "name": key,
                "value_type": "string",
                "value": value,
            }
            for key, value in sorted(values["parameters"].items())
        ]
        return SimpleNamespace(
            coordinate=COORDINATE,
            name=name,
            definition_digest="sha256:" + "9" * 64,
            result={
                "verdict": self.query_verdict,
                "parameters": parameters,
                "truncation": {"clipped_budgets": self.clipped_budgets},
                "rows": [{"subject": "wi-42"}],
                "conflicts": [],
                "result_shape": "subject",
                "result_cardinality": "many",
                "result_binding": "item",
                "dedupe": "subject",
            },
        )

    def close(self) -> None:
        return None


def _repin(
    client: _RepinClient,
    root: Path,
    *,
    claims: tuple[str, ...] = (),
    queries: tuple[tuple[str, dict[str, object]], ...] = (),
):  # type: ignore[no-untyped-def]
    return repin_projection_block(
        client,  # type: ignore[arg-type]
        "inst_projection",
        workspace=root,
        source_id="corpus.runbook",
        block_id="summary",
        claims=claims,
        queries=queries,
        evaluation_time=NOW,
    )


def test_bootstrap_repin_changes_only_opening_then_preserves_or_replaces_backings(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path)
    original = source.read_bytes()
    client = _RepinClient()

    with pytest.raises(ProjectionRepinError, match="explicit backing"):
        _repin(client, tmp_path)
    assert source.read_bytes() == original

    first = _repin(client, tmp_path, claims=("CLM-first",))
    (parsed,) = parse_projection_blocks(source.read_bytes(), source_id="corpus.runbook")
    assert parsed.stamp == first
    assert (
        source.read_bytes()[parsed.opening_end :]
        == original[
            original.index(b"<!-- playbill:block:summary -->\n")
            + len(b"<!-- playbill:block:summary -->\n") :
        ]
    )
    assert first.body_digest == "sha256:" + hashlib.sha256(BODY).hexdigest()

    preserved = _repin(client, tmp_path)
    assert preserved.backing[0].identity.name == "CLM-first"
    changed = _repin(client, tmp_path, claims=("CLM-second",))
    assert changed.backing[0].identity.name == "CLM-second"


def test_body_edit_is_preserved_and_repin_updates_only_its_commitment(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    client = _RepinClient()
    first = _repin(client, tmp_path, claims=("CLM-first",))
    edited = source.read_bytes().replace(b"status: ready", b"status: blocked")
    source.write_bytes(edited)

    refreshed = _repin(client, tmp_path)

    assert refreshed.body_digest != first.body_digest
    (parsed,) = parse_projection_blocks(source.read_bytes(), source_id="corpus.runbook")
    assert source.read_bytes()[parsed.body_start : parsed.body_end] == BODY.replace(
        b"status: ready", b"status: blocked"
    )


def test_whole_file_cas_preserves_concurrent_author_edits(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    client = _RepinClient()
    concurrent = source.read_bytes() + b"agent wrote this concurrently\n"
    client.on_claim = lambda: source.write_bytes(concurrent)

    with pytest.raises(ProjectionRepinError, match="compare-and-swap"):
        _repin(client, tmp_path, claims=("CLM-first",))

    assert source.read_bytes() == concurrent


def test_query_backing_preserves_resolved_parameters_on_subsequent_repin(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _RepinClient()

    first = _repin(client, tmp_path, queries=(("project.items", {"status": "ready"}),))
    preserved = _repin(client, tmp_path)

    assert isinstance(first.backing[0], ProjectionQueryBackingV1)
    assert isinstance(preserved.backing[0], ProjectionQueryBackingV1)
    assert preserved.backing[0].resolved_parameter_bindings[0].value == "ready"


@pytest.mark.parametrize("kind", ["refused", "truncated"])
def test_repin_refuses_incomplete_query_backings(tmp_path: Path, kind: str) -> None:
    source = _workspace(tmp_path)
    before = source.read_bytes()
    client = _RepinClient()
    if kind == "refused":
        client.query_verdict = "refused"
    else:
        client.clipped_budgets = ["max_results"]

    with pytest.raises(ProjectionRepinError, match=kind):
        _repin(client, tmp_path, queries=(("project.items", {}),))

    assert source.read_bytes() == before


def test_sdk_block_facade_bootstraps_at_its_active_coordinate(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _RepinClient()
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_projection",
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    stamp = playbill.block.repin(
        "corpus.runbook",
        "summary",
        claims=("CLM-first",),
        evaluation_time=NOW,
    )

    assert stamp.declared_generation == 7
    assert stamp.backing[0].identity.qualified == "Claim:CLM-first"
