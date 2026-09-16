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
from cruxible_client.authoring.projection_package import load_projection_manifests
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
        self.declared: list[dict[str, Any]] = []

    def declare_playbill_block(
        self,
        _instance_id: str,
        stamp: dict[str, Any],
    ) -> api.PlaybillBlockDeclareResultV1:
        """Record the declaration a repin makes after it writes the marker.

        A stamped marker the instance has never heard of is an orphan to every
        reader that asks whether a block is sanctioned, so the write is only
        half the operation: the page carries the stamp and the instance carries
        the registration. The stub keeps what it was told so the tests can pin
        that the two agree.
        """

        self.declared.append(stamp)
        return api.PlaybillBlockDeclareResultV1(
            source_id=stamp["source_id"],
            block_id=stamp["block_id"],
            outcome="declared",
            declared_generation=stamp["declared_generation"],
            coordinate=COORDINATE,
        )

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
    claims: tuple[str, ...] | None = None,
    queries: tuple[tuple[str, dict[str, object]], ...] | None = None,
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
    assert b"<!-- playbill:block:summary:ref:" in source.read_bytes()
    (parsed,) = parse_projection_blocks(
        source.read_bytes(),
        source_id="corpus.runbook",
        manifests=load_projection_manifests(tmp_path, source.read_bytes()),
    )
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


def test_a_repin_registers_the_marker_it_just_wrote(tmp_path: Path) -> None:
    """The page and the instance have to agree that a block exists.

    A stamped marker the instance has never heard of is what every reader that
    asks "is this block sanctioned?" calls an orphan, and before this a block an
    agent declared was never registered anywhere -- the question was answered by
    whether its id happened to begin `pub-`, a spelling only the retired
    publication road minted. A repin declares what it wrote, in that order: the
    marker lands first, because a registration for a marker that never landed
    would be the same disagreement in the other direction.
    """

    source = _workspace(tmp_path)
    client = _RepinClient()

    stamp = _repin(client, tmp_path, claims=("CLM-first",))

    assert [item["block_id"] for item in client.declared] == ["summary"]
    assert client.declared[0] == stamp.model_dump(mode="json")
    (block,) = parse_projection_blocks(
        source.read_bytes(),
        source_id="corpus.runbook",
        manifests=load_projection_manifests(tmp_path, source.read_bytes()),
    )
    assert block.stamp == stamp


def test_body_edit_is_preserved_and_repin_updates_only_its_commitment(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    client = _RepinClient()
    first = _repin(client, tmp_path, claims=("CLM-first",))
    edited = source.read_bytes().replace(b"status: ready", b"status: blocked")
    source.write_bytes(edited)

    refreshed = _repin(client, tmp_path)

    assert refreshed.body_digest != first.body_digest
    (parsed,) = parse_projection_blocks(
        source.read_bytes(),
        source_id="corpus.runbook",
        manifests=load_projection_manifests(tmp_path, source.read_bytes()),
    )
    assert source.read_bytes()[parsed.body_start : parsed.body_end] == BODY.replace(
        b"status: ready", b"status: blocked"
    )


def test_repin_of_a_stamped_block_preserves_an_adjacent_unstamped_draft(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path)
    client = _RepinClient()
    _repin(client, tmp_path, claims=("CLM-first",))
    draft = b"<!-- playbill:block:draft -->\nagent-owned draft\n<!-- /playbill:block:draft -->\n"
    source.write_bytes(source.read_bytes() + draft)

    refreshed = _repin(client, tmp_path)

    assert refreshed.backing[0].identity.name == "CLM-first"
    assert source.read_bytes().endswith(draft)
    blocks = parse_projection_blocks(
        source.read_bytes(),
        source_id="corpus.runbook",
        allow_bootstrap=True,
        manifests=load_projection_manifests(tmp_path, source.read_bytes()),
    )
    assert [block.block_id for block in blocks] == ["summary", "draft"]
    assert blocks[1].stamp is None


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
    content = (tmp_path / "runbook.md").read_bytes()
    assert b"<!-- playbill:block:summary:ref:" in content
    (block,) = parse_projection_blocks(
        content, source_id="corpus.runbook", manifests=load_projection_manifests(tmp_path, content)
    )
    assert block.stamp == stamp


def test_repin_preserves_omitted_categories_and_policy_and_removes_only_explicit_ones(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.declared_blocks import ProjectionArtifactBackingV1

    source = _workspace(tmp_path)
    client = _RepinClient()
    client.get_playbill_subject = lambda *a, **k: SimpleNamespace(  # type: ignore[attr-defined]
        envelope={"artifact_digest": "sha256:" + "5" * 64}
    )
    client.get_playbill_claim_type = lambda *a, **k: SimpleNamespace(  # type: ignore[attr-defined]
        artifact_digest="sha256:" + "6" * 64
    )

    def repin(**kwargs: Any):  # type: ignore[no-untyped-def]
        return repin_projection_block(
            client,
            "inst_projection",
            workspace=tmp_path,
            source_id="corpus.runbook",
            block_id="summary",
            evaluation_time=NOW,
            **kwargs,
        )

    subject = ArtifactIdentity(kind="Subject", name="project.work_item/wi-42")
    predicate = ArtifactIdentity(kind="ClaimType", name="project.work_item.status")
    first = repin(
        claims=("CLM-first",),
        queries=(("items", {"status": "ready"}),),
        artifacts=(subject, predicate),
        currency_policy="require_current",
        compact=True,
    )
    second = repin(claims=("CLM-second",))
    assert second.currency_policy == "require_current"
    assert {b.identity.qualified for b in second.backing} == {
        "Claim:CLM-second",
        "QueryDefinition:items",
        subject.qualified,
        predicate.qualified,
    }
    assert next(b for b in second.backing if isinstance(b, ProjectionQueryBackingV1)) == next(
        b for b in first.backing if isinstance(b, ProjectionQueryBackingV1)
    )
    assert repin().backing == second.backing
    third = repin(claims=(), queries=())
    assert all(isinstance(b, ProjectionArtifactBackingV1) for b in third.backing)
    assert repin().backing == third.backing  # artifact-only repin
    before = source.read_bytes()
    with pytest.raises(ProjectionRepinError, match="at least one"):
        repin(artifacts=())
    assert source.read_bytes() == before


def test_old_manifest_bytes_remain_verifiable_after_repin_adds_policy(tmp_path: Path) -> None:
    from cruxible_client.authoring.projection_package import load_projection_manifests
    from cruxible_client.contracts.declared_blocks import (
        ProjectionBlockStampV1,
        frame_projection_block,
        projection_manifest,
    )

    source = _workspace(tmp_path)
    client = _RepinClient()
    current = _repin(client, tmp_path, claims=("CLM-first",))
    old = ProjectionBlockStampV1.model_validate(
        {
            k: v
            for k, v in {
                **current.model_dump(mode="json"),
                "tag": "playbill-projection-stamp-v1",
            }.items()
            if k != "currency_policy"
        }
    )
    old_digest, old_bytes = projection_manifest(old)
    assert b"currency_policy" not in old_bytes
    source.write_bytes(frame_projection_block(stamp=old, body=BODY))
    repin_projection_block(
        client,
        "inst_projection",
        workspace=tmp_path,
        source_id="corpus.runbook",
        block_id="summary",
        evaluation_time=NOW,
        currency_policy="require_current",
        compact=True,
    )
    (parsed,) = parse_projection_blocks(
        source.read_bytes(),
        source_id="corpus.runbook",
        manifests=load_projection_manifests(tmp_path, source.read_bytes()),
    )
    assert parsed.stamp.tag == "playbill-projection-stamp-v2"
    assert projection_manifest(old) == (old_digest, old_bytes)
    (historical,) = parse_projection_blocks(
        frame_projection_block(stamp=old, body=BODY), source_id="corpus.runbook"
    )
    assert projection_manifest(historical.stamp) == (old_digest, old_bytes)
