"""Coordinate resolution, refusals, artifact identity, and receipting for state diff.

The multigraph matching algorithm itself is covered in
``tests/test_graph/test_graph_diff.py``; this module owns everything the
comparator cannot see -- which coordinates were compared, what each one was
licensed to claim, and what the read persisted.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.errors import ConcurrentStateDriftError, ConfigError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
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
from cruxible_core.service.state_diff import (
    parse_state_coordinate,
    resolve_state_coordinate,
)

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
    assert canonical_json(fetched.content).encode("utf-8") == Path(fetched.path).read_bytes()


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
    assert accounting == {"total": 6, "returned": 2, "truncated": True}


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
