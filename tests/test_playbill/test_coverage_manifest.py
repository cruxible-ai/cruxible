"""PC-F2 local coverage manifest: atomic publication and a monotonic epoch.

The manifest is a disposable local file, not accepted state, so what has to be
true of it is narrow and exact: it commits to no wall clock, it advances rather
than moves sideways, it is republished atomically, it is deleted rather than
trusted when it does not reproduce, and deleting it costs nothing but provable
freshness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.coverage.contracts import CoverageWatcherHealthV1
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV2,
    CoverageScanBudgetV1,
    build_evidence_citation_index_v2,
    build_working_occurrence_overlay,
)
from cruxible_core.playbill.coverage.manifest import (
    COVERAGE_MANIFEST_FILE_V2,
    CoverageManifestError,
    CoverageWorkingSetScopeV1,
    advance_coverage_manifest,
    coverage_manifest_body,
    coverage_manifest_body_v2,
    coverage_manifest_digest_v2,
    coverage_manifest_path,
    coverage_manifest_path_v2,
    discard_coverage_manifest,
    load_coverage_manifest_file,
    load_coverage_manifest_file_v2,
    render_coverage_manifest,
    write_coverage_manifest,
    write_coverage_manifest_v2,
)
from cruxible_core.playbill.coverage.resolver import resolve_coverage_v3
from tests.test_playbill._coverage_support import (
    CITED,
    EPILOGUE,
    HANDBOOK,
    INSTANCE_ID,
    PREAMBLE,
    SCRATCH,
    capture,
    index,
    index_v2,
    manifest,
    manifest_v2,
    overlay,
    profile,
    request,
    unmaterialized_wanted,
    working,
)

HANDBOOK_BODY = PREAMBLE + CITED + EPILOGUE


def _built() -> tuple[object, object]:
    citations = index(capture(HANDBOOK, CITED, with_handle=True))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    return citations, snapshot


# -- determinism and disposability ----------------------------------------


def test_manifest_deletion_and_rebuild_reproduce_the_same_projection(tmp_path: Path) -> None:
    citations = index_v2(capture(HANDBOOK, CITED, with_handle=True))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    body = manifest_v2(citations, snapshot)
    before = resolve_coverage_v3(
        request(HANDBOOK),
        index=citations,
        overlay=snapshot,
        access=profile(),
        manifest=body,
    )

    write_coverage_manifest_v2(tmp_path, body, written_at="2026-08-19T09:00:00Z")
    discard_coverage_manifest(tmp_path)
    assert load_coverage_manifest_file_v2(tmp_path) is None

    rebuilt_citations = index_v2(capture(HANDBOOK, CITED, with_handle=True))
    rebuilt_snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=rebuilt_citations)
    rebuilt = manifest_v2(rebuilt_citations, rebuilt_snapshot)
    written = write_coverage_manifest_v2(tmp_path, rebuilt, written_at="2026-08-19T11:30:00Z")
    reloaded = load_coverage_manifest_file_v2(tmp_path)
    after = resolve_coverage_v3(
        request(HANDBOOK),
        index=rebuilt_citations,
        overlay=rebuilt_snapshot,
        access=profile(),
        manifest=rebuilt,
    )

    assert written.name == COVERAGE_MANIFEST_FILE_V2
    assert reloaded is not None
    assert reloaded.body == rebuilt
    assert coverage_manifest_digest_v2(rebuilt) == coverage_manifest_digest_v2(body)
    assert reloaded.body == body
    assert canonical_bytes(after.model_dump(mode="json")) == canonical_bytes(
        before.model_dump(mode="json")
    )


def test_v2_manifest_discards_the_local_v1_cache_instead_of_migrating_it(
    tmp_path: Path,
) -> None:
    legacy_capture = capture(HANDBOOK, CITED, with_handle=True)
    legacy_index = index(legacy_capture)
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=legacy_index)
    write_coverage_manifest(tmp_path, manifest(legacy_index, snapshot))

    v2_capture = CaptureCitationInputV2.model_validate(
        {
            **legacy_capture.model_dump(mode="json"),
            "tag": "playbill-coverage-capture-citation-input-v2",
            "observation_trust": "proposer_observed",
        }
    )
    v2_index = build_evidence_citation_index_v2(
        at=legacy_index.at,
        captures=(v2_capture,),
    )
    v2_body = coverage_manifest_body_v2(
        instance_id=INSTANCE_ID,
        index=v2_index,
        overlay=snapshot,
        access_profile=profile(),
    )
    written = write_coverage_manifest_v2(tmp_path, v2_body)
    reloaded = load_coverage_manifest_file_v2(tmp_path)

    assert written.name == COVERAGE_MANIFEST_FILE_V2
    assert written == coverage_manifest_path_v2(tmp_path)
    assert not coverage_manifest_path(tmp_path).exists()
    assert load_coverage_manifest_file(tmp_path) is None
    assert reloaded is not None
    assert reloaded.body.tag == "playbill-coverage-manifest-v2"
    assert reloaded.body.format_version == 2


def test_publication_time_stays_outside_the_digest_preimage() -> None:
    citations, snapshot = _built()
    body = manifest(citations, snapshot)

    early = render_coverage_manifest(body, written_at="2026-08-19T09:00:00Z")
    late = render_coverage_manifest(body, written_at="2027-01-01T00:00:00Z")

    assert early != late
    assert json.loads(early)["manifest_digest"] == json.loads(late)["manifest_digest"]
    # The epoch is a counter, and no field of the preimage is a time.
    preimage = body.model_dump(mode="json")
    assert "written_at" not in preimage
    assert isinstance(preimage["epoch"], int)


# -- the monotonic epoch ---------------------------------------------------


def test_the_epoch_advances_and_never_moves_backwards(tmp_path: Path) -> None:
    citations, snapshot = _built()
    body = manifest(citations, snapshot)
    write_coverage_manifest(tmp_path, body)

    edited = overlay(working(HANDBOOK, PREAMBLE + EPILOGUE), citations=citations)
    advanced = advance_coverage_manifest(body, index=citations, overlay=edited)
    write_coverage_manifest(tmp_path, advanced)

    assert advanced.epoch == body.epoch + 1
    published = load_coverage_manifest_file(tmp_path)
    assert published is not None and published.body.epoch == 1

    with pytest.raises(CoverageManifestError, match="monotonic"):
        write_coverage_manifest(tmp_path, body)
    with pytest.raises(CoverageManifestError, match="monotonic"):
        write_coverage_manifest(tmp_path, advanced)


def test_republishing_after_an_edit_restores_a_complete_boundary(tmp_path: Path) -> None:
    citations = index_v2(capture(HANDBOOK, CITED, with_handle=True))
    snapshot = overlay(working(HANDBOOK, HANDBOOK_BODY), citations=citations)
    body = manifest_v2(citations, snapshot)
    edited = overlay(working(HANDBOOK, PREAMBLE + EPILOGUE), citations=citations)

    stale = resolve_coverage_v3(
        request(HANDBOOK), index=citations, overlay=edited, access=profile(), manifest=body
    )
    assert stale.spans[0].health == "stale"

    advanced = coverage_manifest_body_v2(
        instance_id=INSTANCE_ID,
        index=citations,
        overlay=edited,
        access_profile=profile(),
        epoch=body.epoch + 1,
    )
    write_coverage_manifest_v2(tmp_path, advanced)
    fresh = resolve_coverage_v3(
        request(HANDBOOK), index=citations, overlay=edited, access=profile(), manifest=advanced
    )

    assert fresh.spans[0].health == "complete"
    assert fresh.spans[0].match_state == "drifted"
    assert fresh.epoch == 1


# -- failing closed --------------------------------------------------------


def test_a_manifest_that_does_not_reproduce_is_deleted_rather_than_trusted(
    tmp_path: Path,
) -> None:
    citations, snapshot = _built()
    write_coverage_manifest(tmp_path, manifest(citations, snapshot))
    target = coverage_manifest_path(tmp_path)

    record = json.loads(target.read_bytes())
    record["body"]["epoch"] = 99
    target.write_bytes(json.dumps(record).encode("utf-8"))

    assert load_coverage_manifest_file(tmp_path) is None
    assert not target.exists()


def test_a_malformed_manifest_is_deleted_rather_than_raised(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    coverage_manifest_path(tmp_path).write_bytes(b"{not json")

    assert load_coverage_manifest_file(tmp_path) is None
    assert not coverage_manifest_path(tmp_path).exists()


def test_a_truncated_scan_makes_the_manifest_partial(tmp_path: Path) -> None:
    citations, _ = _built()
    starved = build_working_occurrence_overlay(
        (working(HANDBOOK, HANDBOOK_BODY),),
        wanted=unmaterialized_wanted(citations),
        budget=CoverageScanBudgetV1(max_scanned_bytes=0),
    )
    body = coverage_manifest_body(
        instance_id=INSTANCE_ID,
        index=citations,
        overlay=starved,
        access_profile=profile(),
    )

    assert body.completeness == "partial"
    assert body.truncation_reason_codes == ("scan_budget_exceeded",)
    assert body.scope.complete is False


def test_a_manifest_cannot_commit_to_a_source_outside_its_declared_scope() -> None:
    citations, snapshot = _built()
    body = manifest(citations, snapshot)

    with pytest.raises(ValueError, match="outside its declared scope"):
        body.model_validate(
            body.model_dump()
            | {
                "scope": CoverageWorkingSetScopeV1(sources=(SCRATCH,)).model_dump(),
            }
        )


def test_watcher_health_is_a_closed_enum_the_manifest_must_state() -> None:
    citations, snapshot = _built()

    for health in ("absent", "healthy", "degraded", "overflowed"):
        body = manifest(citations, snapshot, watcher_health=health)
        assert body.watcher_health == health

    with pytest.raises(ValueError):
        manifest(citations, snapshot, watcher_health="probably_fine")

    assert set(CoverageWatcherHealthV1.__args__) == {
        "absent",
        "healthy",
        "degraded",
        "overflowed",
    }
