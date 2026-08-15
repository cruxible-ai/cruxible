"""Coordinate resolution, refusals, artifact identity, and receipting for state diff.

The multigraph matching algorithm itself is covered in
``tests/test_graph/test_graph_diff.py``; this module owns everything the
comparator cannot see -- which coordinates were compared, what each one was
licensed to claim, and what the read persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.errors import ConcurrentStateDriftError, ConfigError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateMember
from cruxible_core.primitives import canonical_json
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_add_entities,
    service_add_relationships,
    service_create_state_overlay,
    service_lock,
    service_publish_state,
    service_pull_state_apply,
    service_pull_state_preview,
    service_reload_config,
    service_state_diff,
    service_state_diff_artifact,
)
from cruxible_core.service.snapshots import service_create_snapshot
from cruxible_core.service.state_diff import parse_state_coordinate, resolve_state_coordinate
from cruxible_core.workflow.proposal_preview import compare_pending_relationships

STATE_MODEL_YAML = """\
version: "1.0"
name: case_reference

entity_types:
  Case:
    properties:
      case_id:
        type: string
        primary_key: true
      title:
        type: string
      notes:
        type: json
        optional: true

relationships:
  - name: cites
    from: Case
    to: Case
"""

OVERLAY_CONFIG_LINES = (
    'version: "1.0"',
    "name: case-law-overlay",
    "extends: .cruxible/upstream/current/config.yaml",
    "entity_types: {}",
    "relationships:",
    "  - name: follow_up",
    "    from: Case",
    "    to: Case",
    "    properties:",
    "      reason:",
    "        type: string",
    "        optional: true",
)


def _case(case_id: str, title: str) -> EntityInstance:
    return EntityInstance(
        entity_type="Case",
        entity_id=case_id,
        properties={"case_id": case_id, "title": title},
    )


def test_pending_relationship_preview_uses_shared_edge_comparator(
    instance: CruxibleInstance,
) -> None:
    before = instance.load_graph().to_dict()
    preview = compare_pending_relationships(
        instance.load_graph(),
        "cites",
        [
            CandidateMember(
                relationship_type="cites",
                from_type="Case",
                from_id="CASE-A",
                to_type="Case",
                to_id="CASE-B",
            )
        ],
    )

    edges = preview["sections"]["edges"]
    assert edges["counts"]["added"] == 1
    assert edges["counts"]["removed"] == 0
    assert edges["counts"]["changed"] == 0
    assert edges["added"][0]["claim_id"].startswith("preview-")
    assert instance.load_graph().to_dict() == before


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    root = tmp_path / "root-model"
    root.mkdir()
    (root / "config.yaml").write_text(STATE_MODEL_YAML)
    created = CruxibleInstance.init(root, "config.yaml")
    service_lock(created, force=True)
    service_add_entities(created, [_case("CASE-A", "Alpha"), _case("CASE-B", "Beta")])
    return created


@pytest.fixture
def published_release(instance: CruxibleInstance, tmp_path: Path) -> tuple[CruxibleInstance, Path]:
    release_dir = tmp_path / "releases" / "current"
    service_publish_state(
        instance,
        transport_ref=f"file://{release_dir}",
        state_id="case-law",
        release_id="v1.0.0",
        compatibility="data_only",
    )
    return instance, release_dir


def _write_overlay_config(root: Path) -> None:
    (root / "config.yaml").write_text("\n".join(OVERLAY_CONFIG_LINES) + "\n")


def _make_overlay(release_dir: Path, root: Path) -> CruxibleInstance:
    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        root_dir=root,
    ).instance
    _write_overlay_config(root)
    service_reload_config(overlay)
    return overlay


# ---------------------------------------------------------------------------
# D1 -- grammar, defaults, self-diff
# ---------------------------------------------------------------------------


def test_reserved_literals_and_snapshot_ids_are_disjoint() -> None:
    """Ambiguity is impossible by CONSTRUCTION, not by convention."""
    assert parse_state_coordinate("current") == "current"
    assert parse_state_coordinate("upstream") == "upstream"
    assert parse_state_coordinate("origin") == "origin"
    assert parse_state_coordinate("snap_0123456789abcdef") == "snapshot"
    for reserved in ("current", "upstream", "origin"):
        assert not reserved.startswith("snap_")


@pytest.mark.parametrize(
    "spec",
    ["snap_TOOSHORT", "snap_0123456789ABCDEF", "snapshot_0123456789abcdef", "", "head"],
)
def test_unparseable_coordinate_refuses_with_the_grammar(spec: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_state_coordinate(spec)
    assert "snap_" in str(excinfo.value)


def test_self_diff_is_empty_and_resolves_current_once(instance: CruxibleInstance) -> None:
    result = service_state_diff(instance, from_coordinate="current", to_coordinate="current")
    assert result.summary["added"] == 0
    assert result.summary["removed"] == 0
    assert result.summary["changed"] == 0
    assert result.from_coordinate == result.to_coordinate


def test_bare_default_is_parent_of_head(instance: CruxibleInstance) -> None:
    first = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])
    second = service_create_snapshot(instance).snapshot
    assert second.parent_snapshot_id == first.snapshot_id

    result = service_state_diff(instance)
    assert result.default_basis == "parent_of_head"
    assert result.from_coordinate["identity"]["snapshot_id"] == first.snapshot_id
    assert result.to_coordinate["kind"] == "current"
    assert result.summary["added"] == 1


def test_first_snapshot_falls_back_to_head_and_stamps_the_basis(
    instance: CruxibleInstance,
) -> None:
    head = service_create_snapshot(instance).snapshot
    assert head.parent_snapshot_id is None

    result = service_state_diff(instance)
    assert result.default_basis == "head"
    assert result.from_coordinate["identity"]["snapshot_id"] == head.snapshot_id
    assert result.from_coordinate["default_basis"] == "head"


def test_bare_diff_without_a_head_snapshot_refuses_with_teaching(
    instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        service_state_diff(instance)
    message = str(excinfo.value)
    assert "cruxible snapshot list" in message
    assert "origin" in message


def test_origin_without_clone_provenance_refuses(instance: CruxibleInstance) -> None:
    with pytest.raises(ConfigError) as excinfo:
        service_state_diff(instance, from_coordinate="origin")
    assert "CLONE provenance" in str(excinfo.value)


def test_origin_resolves_on_a_clone(instance: CruxibleInstance, tmp_path: Path) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    clone, _ = CruxibleInstance.clone_from_snapshot(
        instance,
        snapshot.snapshot_id,
        tmp_path / "clone",
    )
    result = service_state_diff(clone, from_coordinate="origin", to_coordinate="current")
    assert result.from_coordinate["kind"] == "origin"
    assert result.from_coordinate["identity"]["snapshot_id"] == snapshot.snapshot_id


def test_unknown_snapshot_id_refuses(instance: CruxibleInstance) -> None:
    with pytest.raises(ConfigError, match="not found"):
        service_state_diff(instance, from_coordinate="snap_00000000000000ff")


# ---------------------------------------------------------------------------
# D2 -- sections are omitted, never emptied
# ---------------------------------------------------------------------------


def test_procedures_are_omitted_by_format_for_upstream(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    _root, release_dir = published_release
    overlay = _make_overlay(release_dir, tmp_path / "overlay")

    result = service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")
    assert "procedures" not in result.sections
    omitted = {entry["section"]: entry for entry in result.omitted_sections}
    assert omitted["procedures"]["from_status"] == "unavailable_by_format"
    assert omitted["procedures"]["side"] == "from"


def test_snapshot_without_procedures_artifact_omits_the_section(
    instance: CruxibleInstance,
) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    _delete_snapshot_artifact(instance, snapshot.snapshot_id, "procedures.json")

    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    omitted = {entry["section"]: entry for entry in result.omitted_sections}
    assert omitted["procedures"]["from_status"] == "unavailable_missing_artifact"
    assert "procedures" not in result.sections


def test_corrupt_unselected_procedures_artifact_does_not_block_an_edges_diff(
    instance: CruxibleInstance,
) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    _overwrite_snapshot_artifact(instance, snapshot.snapshot_id, "procedures.json", b"{ not json")

    result = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        sections=("edges",),
    )
    assert set(result.sections) == {"edges"}

    with pytest.raises(ConfigError, match="procedures.json"):
        service_state_diff(
            instance,
            from_coordinate=snapshot.snapshot_id,
            sections=("procedures",),
        )


def test_snapshot_missing_graph_artifact_refuses(instance: CruxibleInstance) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    _delete_snapshot_artifact(instance, snapshot.snapshot_id, "graph.json")
    with pytest.raises(ConfigError) as excinfo:
        service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    assert "graph.json" in str(excinfo.value)
    assert snapshot.snapshot_id in str(excinfo.value)


def test_snapshot_with_invalid_graph_artifact_refuses(instance: CruxibleInstance) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    _overwrite_snapshot_artifact(instance, snapshot.snapshot_id, "graph.json", b"[1, 2, 3]")
    with pytest.raises(ConfigError, match="node-link"):
        service_state_diff(instance, from_coordinate=snapshot.snapshot_id)


def test_pre_upstream_snapshot_propagates_unknown_ownership_and_disables_stubs(
    instance: CruxibleInstance,
) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    _delete_snapshot_artifact(instance, snapshot.snapshot_id, "upstream.json")

    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    assert result.from_coordinate["ownership"]["basis"] == "unknown"
    # Never recomputed from current metadata: BOTH sides go unknown.
    assert result.to_coordinate["ownership"]["basis"] == "unknown"
    assert result.sections["entities"]["diagnostics"]["stub_detection"] == "disabled"


def test_new_snapshots_pin_the_ownership_basis(instance: CruxibleInstance) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    assert instance.get_snapshot_artifact(snapshot.snapshot_id, "upstream.json") is not None
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    assert result.from_coordinate["ownership"]["basis"] == "pinned"
    assert result.sections["entities"]["diagnostics"]["stub_detection"] == "enabled"


# ---------------------------------------------------------------------------
# D2/D12 -- upstream verification tri-state
# ---------------------------------------------------------------------------


def test_upstream_refused_when_the_instance_is_not_an_overlay(
    instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError, match="not a pullable overlay"):
        service_state_diff(instance, from_coordinate="upstream")


def test_pinned_upstream_member_mismatch_refuses(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    _root, release_dir = published_release
    overlay_root = tmp_path / "overlay"
    overlay = _make_overlay(release_dir, overlay_root)
    (overlay_root / ".cruxible" / "upstream" / "current" / "graph.json").write_text("{}")

    with pytest.raises(ConfigError, match="no longer matches its recorded"):
        service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")


def test_unpinned_legacy_member_is_trusted_loudly_and_changes_the_digest(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    _root, release_dir = published_release
    overlay = _make_overlay(release_dir, tmp_path / "overlay")
    verified = service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")
    assert verified.artifact_trust == "verified"

    upstream = overlay.get_upstream_metadata()
    assert upstream is not None
    overlay.set_upstream_metadata(upstream.model_copy(update={"graph_digest": None}))

    unverified = service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")
    assert unverified.from_coordinate["members"]["graph.json"] == "unpinned_legacy"
    assert unverified.from_coordinate["verification"] == "unverified_legacy"
    assert unverified.artifact_trust == "unverified_upstream"
    # The trust flag is INSIDE the digest preimage: an unverified upstream can
    # never be laundered into looking verified.
    assert unverified.diff_digest != verified.diff_digest


# ---------------------------------------------------------------------------
# D3 -- named digest domains
# ---------------------------------------------------------------------------


def test_digest_domains_never_cross_compare(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    _root, release_dir = published_release
    overlay = _make_overlay(release_dir, tmp_path / "overlay")
    result = service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")
    domains = result.context["digest_domains"]

    # A semantic hash exists only on the live side; no equality flag is emitted.
    assert domains["semantic_config_digest"]["comparable"] is False
    assert domains["semantic_config_digest"]["equal"] is None
    assert domains["semantic_config_digest"]["from"] is None
    # graph_artifact_digest is a BYTE hash and is labeled as one.
    assert domains["graph_artifact_digest"]["scope"] == "bytes"


# ---------------------------------------------------------------------------
# D6 -- provenance normalization on the upstream side
# ---------------------------------------------------------------------------


def test_fresh_overlay_diffs_empty_against_its_upstream(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    _root, release_dir = published_release
    overlay = _make_overlay(release_dir, tmp_path / "overlay")
    result = service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")

    assert result.normalizations == ["upstream_clone_relabel"]
    assert result.liveness == "not_evaluated"
    assert result.summary["added"] == 0
    assert result.summary["removed"] == 0
    assert result.summary["changed"] == 0


def test_fresh_pull_leaves_upstream_owned_edges_unchanged(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    root_instance, release_dir = published_release
    overlay_root = tmp_path / "overlay"
    overlay = _make_overlay(release_dir, overlay_root)
    service_add_relationships(
        overlay,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="follow_up",
                to_type="Case",
                to_id="CASE-B",
                properties={"reason": "watch"},
            )
        ],
        source="test",
        source_ref="overlay-local",
    )
    service_add_relationships(
        root_instance,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="cites",
                to_type="Case",
                to_id="CASE-B",
            )
        ],
        source="test",
        source_ref="upstream-author",
    )
    successor = tmp_path / "releases" / "successor"
    service_publish_state(
        root_instance,
        transport_ref=f"file://{successor}",
        state_id="case-law",
        release_id="v1.1.0",
        compatibility="data_only",
    )
    shutil.rmtree(release_dir)
    shutil.copytree(successor, release_dir)
    preview = service_pull_state_preview(overlay)
    service_pull_state_apply(overlay, expected_apply_digest=preview.apply_digest)

    result = service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")
    upstream_changed = [
        item for item in result.sections["edges"]["changed"] if item["to_ownership"] == "upstream"
    ]
    assert upstream_changed == []
    added_types = {item["relationship_type"] for item in result.sections["edges"]["added"]}
    assert added_types == {"follow_up"}


# ---------------------------------------------------------------------------
# D5 -- a diff never mints and never writes claim identity
# ---------------------------------------------------------------------------


def test_diff_never_mints_a_claim_id(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    _root, release_dir = published_release
    overlay = _make_overlay(release_dir, tmp_path / "overlay")
    from cruxible_core.storage.sqlite import LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY

    before_ids = set(overlay.load_graph()._claim_ids)
    before_map = overlay.get_instance_state(LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY)

    service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")

    overlay.invalidate_graph_cache()
    assert set(overlay.load_graph()._claim_ids) == before_ids
    assert overlay.get_instance_state(LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY) == before_map


def test_upstream_coordinate_reports_claim_identity_coverage(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    _root, release_dir = published_release
    overlay = _make_overlay(release_dir, tmp_path / "overlay")
    result = service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")
    coverage = result.from_coordinate["claim_identity"]["coverage"]
    assert set(coverage) == {"resolved", "unresolved"}


# ---------------------------------------------------------------------------
# D8/D9 -- artifact identity, caps, elision, determinism
# ---------------------------------------------------------------------------


def test_repeat_runs_produce_byte_identical_artifacts(instance: CruxibleInstance) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])

    first = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    # A cache-cold reload must not move the digest.
    instance.invalidate_graph_cache()
    second = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    assert first.diff_digest == second.diff_digest
    assert Path(first.artifact_ref.path) == Path(second.artifact_ref.path)


def test_persisted_artifact_redigests_to_its_filename(instance: CruxibleInstance) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)

    path = Path(result.artifact_ref.path)
    payload = path.read_bytes()
    assert f"sha256:{hashlib.sha256(payload).hexdigest()}" == result.diff_digest
    assert path.stem == result.diff_digest.split(":", 1)[1]
    assert result.artifact_ref.byte_count == len(payload)


def test_artifact_retrieval_returns_the_persisted_body(instance: CruxibleInstance) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)

    fetched = service_state_diff_artifact(instance, result.diff_digest)
    assert fetched.diff_digest == result.diff_digest
    # content_bytes is what a verifier hashes: no serializer of the caller's
    # own is involved, so reproducing the digest needs nothing from Cruxible.
    payload = fetched.content_bytes.encode("utf-8")
    assert payload == Path(fetched.path).read_bytes()
    assert f"sha256:{hashlib.sha256(payload).hexdigest()}" == result.diff_digest
    assert fetched.content == json.loads(fetched.content_bytes)


def test_artifact_retrieval_refuses_bytes_that_no_longer_match_their_address(
    instance: CruxibleInstance,
) -> None:
    """Content addressing is only self-evident if the READER checks."""
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)

    path = Path(result.artifact_ref.path)
    tampered = json.loads(path.read_text())
    tampered["summary"]["added"] = 999
    path.write_text(canonical_json(tampered))

    with pytest.raises(ConfigError) as excinfo:
        service_state_diff_artifact(instance, result.diff_digest)
    message = str(excinfo.value)
    assert "does not match the digest it is stored under" in message
    assert result.diff_digest in message


def test_artifact_is_published_atomically_under_its_final_name(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No partial file is ever visible under the content-addressed name."""
    import cruxible_core.service.state_diff as state_diff_module

    observed: dict[str, Any] = {}
    real_replace = os.replace

    def _watch(src: Any, dst: Any) -> None:
        # At the moment of publication the final name must not exist yet, and
        # the complete bytes must already be on disk under the temp name.
        observed["final_existed_before_replace"] = Path(dst).exists()
        observed["temp_name"] = Path(src).name
        observed["temp_bytes"] = Path(src).read_bytes()
        real_replace(src, dst)

    monkeypatch.setattr(state_diff_module.os, "replace", _watch)
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)

    assert observed["final_existed_before_replace"] is False
    assert observed["temp_name"].startswith(".") and observed["temp_name"].endswith(".tmp")
    assert f"sha256:{hashlib.sha256(observed['temp_bytes']).hexdigest()}" == result.diff_digest
    # The temp sibling never survives a successful publish.
    assert sorted(p.name for p in Path(result.artifact_ref.path).parent.iterdir()) == [
        Path(result.artifact_ref.path).name
    ]


def test_artifact_retrieval_for_an_unknown_digest_refuses(instance: CruxibleInstance) -> None:
    with pytest.raises(ConfigError, match="never garbage-collected"):
        service_state_diff_artifact(instance, f"sha256:{'0' * 64}")


def test_artifact_retrieval_rejects_a_non_digest(instance: CruxibleInstance) -> None:
    with pytest.raises(ConfigError, match="not a diff digest"):
        service_state_diff_artifact(instance, "../../etc/passwd")


def test_caps_do_not_move_the_diff_digest_but_do_move_the_view_digest(
    instance: CruxibleInstance,
) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case(f"CASE-{n:03d}", f"Case {n}") for n in range(6)])

    uncapped = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    capped = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        max_items_per_bucket=2,
    )
    assert capped.diff_digest == uncapped.diff_digest
    assert capped.view_digest != uncapped.view_digest
    assert uncapped.artifact_complete is True
    assert capped.artifact_complete is False
    accounting = capped.sections["entities"]["view"]["added"]
    assert accounting == {"total": 6, "returned": 2, "truncated": True, "unit": "items"}
    # The two record-shaped buckets say so, because their `counts` tally ROWS
    # while their bodies list per-side records.
    assert capped.sections["edges"]["view"]["identity_conflict"]["unit"] == "records"
    assert capped.sections["edges"]["view"]["ambiguous"]["unit"] == "records"


def test_view_digest_differs_from_diff_digest_even_when_complete(
    instance: CruxibleInstance,
) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    assert result.artifact_complete is True
    assert result.view_digest != result.diff_digest


def test_oversized_values_are_elided_with_digest_and_byte_count(
    instance: CruxibleInstance,
) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Case",
                entity_id="CASE-A",
                properties={"case_id": "CASE-A", "title": "x" * 4096},
            )
        ],
    )
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    changed = result.sections["entities"]["changed"][0]
    elided = [
        change for change in changed["properties"]["changes"] if _is_elided(change["to_value"])
    ]
    assert elided, changed
    marker = elided[0]["to_value"]
    assert marker["byte_count"] > 2048
    assert marker["value_digest"].startswith("sha256:")
    assert result.artifact_complete is False
    assert result.view["elided_value_count"] >= 1


def test_a_selector_change_changes_the_diff_digest(instance: CruxibleInstance) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])

    whole = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    filtered = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        sections=("entities",),
    )
    assert whole.diff_digest != filtered.diff_digest
    assert filtered.selector["sections"] == ["entities"]


def test_edge_key_is_not_in_the_digest_preimage(instance: CruxibleInstance) -> None:
    """``edge_key`` is durable but semantically meaningless across images.

    Two graphs with identical semantic state but different ``edge_key``
    assignments must produce the same ``diff_digest``, or the plan artifact
    pins a serialization detail instead of the state it describes.
    """
    snapshot = service_create_snapshot(instance).snapshot
    service_add_relationships(
        instance,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="cites",
                to_type="Case",
                to_id="CASE-B",
            )
        ],
        source="test",
        source_ref="seed",
    )
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)

    artifact = json.loads(Path(result.artifact_ref.path).read_text())
    assert "diagnostic" not in _all_keys(artifact)
    # The section-level `diagnostics` accounting IS semantic and stays.
    assert "diagnostics" in _all_keys(artifact)
    assert artifact["sections"]["entities"]["diagnostics"]["stub_detection"] == "enabled"
    # The RETURNED VIEW keeps the per-item diagnostic for a human's eyes: the
    # key was moved out of the preimage, not deleted from the product.
    assert result.sections["edges"]["added"][0]["diagnostic"]["edge_key"] is not None


@pytest.mark.parametrize(
    "notes",
    [
        {"diagnostic": {"code": "E-42"}},
        {"nested": {"diagnostic": ["kept"]}},
        [{"diagnostic": "list entry"}],
        {"diagnostic": {"diagnostic": {"diagnostic": "deep"}}},
    ],
)
def test_domain_values_named_diagnostic_survive_into_the_artifact(
    instance: CruxibleInstance,
    notes: Any,
) -> None:
    """The preimage projection is PATH-scoped, never a recursive key sweep.

    A caller-authored property literally named ``diagnostic`` is domain data.
    Sweeping it out of the persisted body while ``artifact_complete`` still
    said the plan was whole would be silent data loss in the one artifact whose
    entire job is to be complete.
    """
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Case",
                entity_id="CASE-A",
                properties={"case_id": "CASE-A", "title": "Alpha", "notes": notes},
            )
        ],
    )
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    assert result.artifact_complete is True

    artifact = json.loads(Path(result.artifact_ref.path).read_text())
    changed = artifact["sections"]["entities"]["changed"][0]
    to_values = {
        change["property"]: change["to_value"] for change in changed["properties"]["changes"]
    }
    assert to_values["notes"] == notes
    # And the digest covers it: the persisted bytes carrying the value are
    # exactly what diff_digest is taken over.
    persisted = Path(result.artifact_ref.path).read_bytes()
    assert result.diff_digest == f"sha256:{hashlib.sha256(persisted).hexdigest()}"
    assert "diagnostic" in persisted.decode("utf-8")


def test_added_and_removed_items_keep_domain_diagnostic_values(
    instance: CruxibleInstance,
) -> None:
    payload = {"diagnostic": {"probe": "kept"}}
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Case",
                entity_id="CASE-NEW",
                properties={"case_id": "CASE-NEW", "title": "New", "notes": payload},
            )
        ],
    )
    artifact = json.loads(
        Path(
            service_state_diff(instance, from_coordinate=snapshot.snapshot_id).artifact_ref.path
        ).read_text()
    )
    added = artifact["sections"]["entities"]["added"][0]
    assert added["state"]["properties"]["notes"] == payload
    # The item's OWN ephemeral slot is still gone.
    assert "diagnostic" not in added


def test_reassigning_every_edge_key_leaves_the_preimage_identical(
    instance: CruxibleInstance,
) -> None:
    """A pure re-serialization must not move the plan digest.

    ``edge_key`` survives a round-trip, so it is stable WITHIN one lineage --
    but every path that re-materializes a graph re-assigns it, so two images of
    the same semantic state disagree on it. Comparing preimages directly keeps
    this test about the projection rather than about ``read_revision``, which
    legitimately moves whenever anything writes.
    """
    from cruxible_core.service.state_diff import _without_item_diagnostics

    snapshot = service_create_snapshot(instance).snapshot
    service_add_relationships(
        instance,
        [
            RelationshipInstance(
                from_type="Case",
                from_id="CASE-A",
                relationship_type="cites",
                to_type="Case",
                to_id="CASE-B",
            )
        ],
        source="test",
        source_ref="seed",
    )
    body = json.loads(
        Path(
            service_state_diff(instance, from_coordinate=snapshot.snapshot_id).artifact_ref.path
        ).read_text()
    )

    rekeyed = json.loads(canonical_json(body))
    for item in rekeyed["sections"]["edges"]["added"]:
        item["diagnostic"] = {"edge_key": 987654}
    assert _without_item_diagnostics(rekeyed) == _without_item_diagnostics(body)


def test_procedures_section_honors_the_bucket_selector(instance: CruxibleInstance) -> None:
    """The selector is part of the diff's definition in EVERY section."""
    snapshot = service_create_snapshot(instance).snapshot
    _seed_procedure(instance, "PRC-alpha")

    whole = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        sections=("procedures",),
    )
    assert whole.sections["procedures"]["counts"]["added"] == 1
    assert len(whole.sections["procedures"]["added"]) == 1

    filtered = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        sections=("procedures",),
        buckets=("changed",),
    )
    # Counts stay whole; only the reported body narrows.
    assert filtered.sections["procedures"]["counts"]["added"] == 1
    assert filtered.sections["procedures"]["added"] == []
    assert filtered.diff_digest != whole.diff_digest

    changed_only = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        sections=("procedures",),
        changed_only=True,
    )
    assert changed_only.sections["procedures"]["added"] == []
    assert changed_only.diff_digest not in {whole.diff_digest, filtered.diff_digest}


def test_edges_only_diff_never_queries_the_procedure_store(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``current``'s procedure table is an unbounded fetch; it must stay lazy."""
    snapshot = service_create_snapshot(instance).snapshot

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("procedure store queried for an edges-only diff")

    monkeypatch.setattr(CruxibleInstance, "get_procedure_store", _boom)
    result = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        sections=("edges",),
    )
    assert set(result.sections) == {"edges"}


def test_a_procedure_write_after_the_closing_revision_read_does_not_leak_in(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A coordinate is ONE revision across all its sections.

    The seam has to be AFTER the closing revision read. A write injected
    between the opening and closing reads is caught by the graph sandwich
    whether or not procedures are captured inside it, so probing there proves
    nothing about this fix. ``get_head_snapshot_id`` is the first call the
    resolver makes once the sandwich has closed, which is exactly the window
    where a lazily-loaded procedure table would silently pick up a newer
    revision than the coordinate it is stamped with.

    The assertion is therefore the ABSENCE of the late write: the artifact must
    describe revision N, not N plus whatever landed afterwards.
    """
    snapshot = service_create_snapshot(instance).snapshot
    real_head = CruxibleInstance.get_head_snapshot_id
    seeded: dict[str, bool] = {}

    def _seed_after_the_closing_read(self: CruxibleInstance) -> Any:
        head = real_head(self)
        if not seeded:
            seeded["done"] = True
            _seed_procedure(self, "PRC-late")
        return head

    monkeypatch.setattr(CruxibleInstance, "get_head_snapshot_id", _seed_after_the_closing_read)
    result = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        sections=("procedures",),
    )

    assert seeded == {"done": True}
    assert result.sections["procedures"]["counts"]["added"] == 0
    assert result.sections["procedures"]["added"] == []
    # The write really did land -- the diff simply does not describe it, which
    # is the whole point: it happened after this coordinate was closed.
    monkeypatch.undo()
    later = service_state_diff(
        instance,
        from_coordinate=snapshot.snapshot_id,
        sections=("procedures",),
    )
    assert later.sections["procedures"]["counts"]["added"] == 1


def test_procedure_drift_inside_the_sandwich_refuses_rather_than_mixing_revisions(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revision that genuinely moves mid-capture refuses after one retry.

    This is the sandwich's own guarantee (a write between the opening and
    closing reads), not the lazy-loader fix -- kept because the two together
    are what make a coordinate single-revision: this one refuses when the
    revision moved, the test above proves nothing is picked up after it
    settled.
    """
    snapshot = service_create_snapshot(instance).snapshot
    real_load = CruxibleInstance.load_graph
    counter = {"n": 0}

    def _always_drift(self: CruxibleInstance) -> Any:
        graph = real_load(self)
        counter["n"] += 1
        _seed_procedure(self, f"PRC-racer-{counter['n']}")
        return graph

    monkeypatch.setattr(CruxibleInstance, "load_graph", _always_drift)
    with pytest.raises(ConcurrentStateDriftError):
        service_state_diff(
            instance,
            from_coordinate=snapshot.snapshot_id,
            sections=("procedures",),
        )
    assert counter["n"] == 2


def test_max_items_per_bucket_must_be_at_least_one(instance: CruxibleInstance) -> None:
    with pytest.raises(ConfigError, match="at least 1"):
        service_state_diff(instance, from_coordinate="current", max_items_per_bucket=0)


def test_corrupt_upstream_json_degrades_with_a_named_reason(
    instance: CruxibleInstance,
) -> None:
    """Ownership is an annotation: losing it must not make a snapshot undiffable."""
    snapshot = service_create_snapshot(instance).snapshot
    _overwrite_snapshot_artifact(instance, snapshot.snapshot_id, "upstream.json", b"{ not json")

    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    ownership = result.from_coordinate["ownership"]
    assert ownership["basis"] == "unknown"
    assert "upstream.json" in (ownership["reason"] or "")
    assert "unreadable" in (ownership["reason"] or "")
    assert result.sections["entities"]["diagnostics"]["stub_detection"] == "disabled"


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"owned_entity_types": "Case"}',
        b'{"state_id": "case-law"}',
        b'{"owned_entity_types": [1, 2], "owned_relationship_types": []}',
        b"[]",
    ],
)
def test_syntactically_valid_garbage_upstream_json_degrades_to_unknown(
    instance: CruxibleInstance,
    payload: bytes,
) -> None:
    """Parsing is not validating.

    ``{}`` used to become a PINNED EMPTY boundary that silently enabled stub
    detection, and a string where a list belongs became a boundary over its
    individual characters -- both confidently wrong rather than honestly
    unknown.
    """
    snapshot = service_create_snapshot(instance).snapshot
    _overwrite_snapshot_artifact(instance, snapshot.snapshot_id, "upstream.json", payload)

    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    ownership = result.from_coordinate["ownership"]
    assert ownership["basis"] == "unknown"
    assert "upstream.json" in (ownership["reason"] or "")
    assert ownership["owned_entity_types"] == []
    assert result.sections["entities"]["diagnostics"]["stub_detection"] == "disabled"


def test_a_null_upstream_json_is_a_pinned_empty_boundary(instance: CruxibleInstance) -> None:
    """No upstream THEN is a pinned fact, not an unknown one."""
    snapshot = service_create_snapshot(instance).snapshot
    _overwrite_snapshot_artifact(instance, snapshot.snapshot_id, "upstream.json", b"null")

    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    assert result.from_coordinate["ownership"]["basis"] == "pinned"
    assert result.sections["entities"]["diagnostics"]["stub_detection"] == "enabled"


@pytest.mark.parametrize("payload", [b"{}", b'{"nodes": []}', b'{"directed": true}'])
def test_malformed_graph_json_objects_get_the_named_refusal(
    instance: CruxibleInstance,
    payload: bytes,
) -> None:
    """Well-formed JSON that is not node-link data is still a NAMED refusal.

    ``{}`` parses, passes the object check, then makes networkx raise
    ``KeyError('nodes')`` -- which escaped the D12 message entirely and
    surfaced as an unhandled server error.
    """
    snapshot = service_create_snapshot(instance).snapshot
    _overwrite_snapshot_artifact(instance, snapshot.snapshot_id, "graph.json", payload)

    with pytest.raises(ConfigError) as excinfo:
        service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    message = str(excinfo.value)
    assert "graph.json" in message
    assert snapshot.snapshot_id in message


def test_absent_upstream_json_names_a_different_reason_than_a_corrupt_one(
    instance: CruxibleInstance,
) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    _delete_snapshot_artifact(instance, snapshot.snapshot_id, "upstream.json")

    reason = service_state_diff(instance, from_coordinate=snapshot.snapshot_id).from_coordinate[
        "ownership"
    ]["reason"]
    assert "absent" in (reason or "")


def test_invalid_selector_values_refuse_listing_the_valid_ones(
    instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError, match="Valid sections"):
        service_state_diff(instance, from_coordinate="current", sections=("receipts",))
    with pytest.raises(ConfigError, match="Valid buckets"):
        service_state_diff(instance, from_coordinate="current", buckets=("maybe",))
    with pytest.raises(ConfigError, match="Present at these coordinates"):
        service_state_diff(instance, from_coordinate="current", entity_types=("Widget",))
    with pytest.raises(ConfigError, match="Present at these coordinates"):
        service_state_diff(instance, from_coordinate="current", relationship_types=("orbits",))


def test_a_not_yet_pulled_release_is_refused_and_names_pull_preview(
    instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        service_state_diff(instance, from_coordinate="file:///tmp/some-release")
    message = str(excinfo.value)
    assert "pull-preview" in message or "pull-apply" in message
    assert "NOT a coordinate" in message


def test_pinned_upstream_member_that_went_missing_refuses(
    published_release: tuple[CruxibleInstance, Path],
    tmp_path: Path,
) -> None:
    _root, release_dir = published_release
    overlay_root = tmp_path / "overlay"
    overlay = _make_overlay(release_dir, overlay_root)
    (overlay_root / ".cruxible" / "upstream" / "current" / "graph.json").unlink()

    with pytest.raises(ConfigError, match="missing its materialized"):
        service_state_diff(overlay, from_coordinate="upstream", to_coordinate="current")


def test_artifact_persistence_failure_fails_the_read(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cruxible_core.service.state_diff as state_diff_module

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk is full")

    monkeypatch.setattr(state_diff_module, "_persist_artifact", _boom)
    with pytest.raises(OSError, match="disk is full"):
        service_state_diff(instance, from_coordinate="current")


def test_receipt_persistence_failure_fails_the_read(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cruxible_core.service.state_diff as state_diff_module

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise ConfigError("receipt store is unavailable")

    monkeypatch.setattr(state_diff_module, "_record_receipt", _boom)
    with pytest.raises(ConfigError, match="receipt store is unavailable"):
        service_state_diff(instance, from_coordinate="current")


# ---------------------------------------------------------------------------
# D9 -- consistent `current` capture
# ---------------------------------------------------------------------------


def test_second_revision_mismatch_refuses_concurrent_state_drift(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revisions = iter([1, 2, 3, 4, 5, 6, 7, 8])
    monkeypatch.setattr(
        CruxibleInstance,
        "get_read_revision",
        lambda _self: next(revisions),
    )
    with pytest.raises(ConcurrentStateDriftError) as excinfo:
        resolve_state_coordinate(instance, "current", sections=frozenset({"edges"}))
    assert excinfo.value.opening_revision == 3
    assert excinfo.value.closing_revision == 4


def test_one_retry_recovers_from_a_single_drift(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revisions = iter([1, 2, 7, 7, 7, 7])
    monkeypatch.setattr(
        CruxibleInstance,
        "get_read_revision",
        lambda _self: next(revisions),
    )
    resolved = resolve_state_coordinate(instance, "current", sections=frozenset({"edges"}))
    assert resolved.identity["read_revision"] == 7


# ---------------------------------------------------------------------------
# D10 -- receipting
# ---------------------------------------------------------------------------


def test_diff_persists_a_listable_receipt_with_both_coordinates(
    instance: CruxibleInstance,
) -> None:
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(instance, [_case("CASE-C", "Gamma")])
    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)

    store = instance.get_receipt_store()
    try:
        rows = store.list_receipts(operation_type="state_diff", limit=10)
    finally:
        store.close()
    assert len(rows) == 1
    parameters = rows[0]["parameters"]
    assert parameters["diff_digest"] == result.diff_digest
    assert parameters["from"]["identity"]["snapshot_id"] == snapshot.snapshot_id
    assert parameters["to"]["kind"] == "current"
    assert result.receipt_id == rows[0]["receipt_id"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_procedure(instance: CruxibleInstance, procedure_id: str) -> None:
    """Persist one minimal procedure definition so the section has content."""
    from cruxible_core.procedure import compute_procedure_definition_digest
    from cruxible_core.procedure.types import ProcedureDefinition, ProcedureRecord

    definition = ProcedureDefinition.model_validate(
        {
            "name": "fixture_procedure",
            "steps": [{"id": "eligible", "assert_exists": {"ref": "$input.value"}}],
            "returns": "eligible",
            "precondition": {"entity_type": "Case", "condition": {"title": "Alpha"}},
            "budget": {"wall_clock_s": 60, "max_provider_calls": 0},
        }
    )
    with instance.write_transaction() as uow:
        uow.procedures.save_procedure(
            ProcedureRecord(
                procedure_id=procedure_id,
                definition=definition,
                definition_digest=compute_procedure_definition_digest(definition),
                proposed_actor_context=None,
            )
        )


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys |= _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


def _is_elided(value: Any) -> bool:
    return isinstance(value, dict) and value.get("elided") is True


def _snapshot_db(instance: CruxibleInstance) -> sqlite3.Connection:
    return sqlite3.connect(instance.get_instance_dir() / "state.db")


def _delete_snapshot_artifact(
    instance: CruxibleInstance,
    snapshot_id: str,
    artifact_name: str,
) -> None:
    """Simulate an image written before ``artifact_name`` existed."""
    connection = _snapshot_db(instance)
    try:
        connection.execute(
            "DELETE FROM snapshot_artifacts WHERE snapshot_id = ? AND artifact_name = ?",
            (snapshot_id, artifact_name),
        )
        connection.commit()
    finally:
        connection.close()


def _overwrite_snapshot_artifact(
    instance: CruxibleInstance,
    snapshot_id: str,
    artifact_name: str,
    content: bytes,
) -> None:
    connection = _snapshot_db(instance)
    try:
        connection.execute(
            "UPDATE snapshot_artifacts SET content = ? WHERE snapshot_id = ? AND artifact_name = ?",
            (content, snapshot_id, artifact_name),
        )
        connection.commit()
    finally:
        connection.close()
