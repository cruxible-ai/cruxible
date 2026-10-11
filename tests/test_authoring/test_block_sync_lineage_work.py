"""Block sync reads a backing's history only where that history moved.

A path's accepted bytes change only in generations whose change records name
it, so a Claim lineage is read at those revisions alone, and a Claim with one
accepted version ever needs no history read at all: the marker already pins it.
The counted work is lineage blob reads; it must stay flat as unrelated
generations accumulate, and every answer must equal the all-generations walk.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import ProjectionCheckRequest
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBacking,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring import projection_sync
from cruxible_core.service.authoring.projection_sync import service_check_projection_blocks
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.floor import projection_lineage
from cruxible_core.service.floor.projection_lineage import (
    ClaimLineageNode,
    clear_lineage_index,
    read_claim_lineages,
)
from tests.core_support._write_support import KIND, caller, seed_write_surface

_WHEN = datetime(2026, 10, 1, tzinfo=UTC)


def _write(instance: PlaybillInstance, *changes: dict[str, Any]) -> list[str]:
    request = WriteRequest.model_validate({"because": "Checked.", "changes": list(changes)})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return [str(change.claim) for change in outcome.changes if change.claim is not None]


def _set(subject: str, value: str) -> dict[str, Any]:
    return {"op": "set", "subject": f"{KIND}/{subject}", "field": "title", "value": value}


def _head(instance: PlaybillInstance) -> tuple[AcceptedCoordinate, int]:
    at = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    with instance.accepted_history_reader(at=at) as history:
        return at, history.sequence


def _stamps(
    instance: PlaybillInstance, claims: list[str], *, count: int = 4
) -> tuple[ProjectionBlockStampV1, ...]:
    at, sequence = _head(instance)
    blobs = instance.blobs_at(at.git_oid, tuple(claim_path(claim) for claim in claims))
    backing = tuple(
        ProjectionClaimBacking(
            identity=ArtifactIdentity(kind="Claim", name=parsed.identity.name),
            statement_digest=claim_statement_digest(parsed.statement).tagged,
        )
        for parsed in (parse_claim(raw, path=path) for path, raw in sorted(blobs.items()))
    )
    return tuple(
        ProjectionBlockStampV1(
            source_id=f"view-{index}",
            block_id=f"block-{index}",
            declared_generation=sequence,
            declared_coordinate=at,
            backing=backing,
            body_digest="sha256:" + "0" * 64,
        )
        for index in range(count)
    )


class _LineageReads:
    """Paths read from history by the lineage reader, per generation read."""

    def __init__(self, instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: Counter[str] = Counter()
        self.calls = 0
        original = instance.blobs_at

        def counted(oid: str, paths: Any) -> dict[str, bytes]:
            if sys._getframe(1).f_code.co_filename == projection_lineage.__file__:  # noqa: SLF001
                self.calls += 1
                self.paths.update(paths)
            return original(oid, paths)

        monkeypatch.setattr(instance, "blobs_at", counted)

    def take(self) -> tuple[int, Counter[str]]:
        found = (self.calls, self.paths.copy())
        self.calls = 0
        self.paths.clear()
        return found


def _check(instance: PlaybillInstance, stamps: tuple[ProjectionBlockStampV1, ...]) -> Any:
    clear_lineage_index(instance)
    return service_check_projection_blocks(
        instance, request=ProjectionCheckRequest(stamps=stamps, evaluation_time=_WHEN)
    )


def test_lineage_reads_stay_flat_as_unrelated_history_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    claims = _write(instance, *(_set(f"item-{index}", f"title {index}") for index in range(8)))
    stamps = _stamps(instance, claims)
    reads = _LineageReads(instance, monkeypatch)

    short = _check(instance, stamps)
    assert {result.status for result in short.results} == {"current"}
    assert reads.take() == (0, Counter())
    for index in range(4):
        _write(instance, _set(f"pad-{index}", "unrelated"))
    long = _check(instance, stamps)
    # Four more generations, and every backing is still one accepted version.
    assert [result.status for result in long.results] == [r.status for r in short.results]
    assert reads.take() == (0, Counter())

    # A revised backing reads its own path at its own two revisions, nothing else.
    _write(instance, _set("item-0", "title 0, revised"))
    revised = _check(instance, stamps)
    assert {result.status for result in revised.results} == {"successor"}
    calls, paths = reads.take()
    assert calls == 2 and paths == Counter({claim_path(claims[0]): 2})


def _all_generations(
    instance: PlaybillInstance, paths: tuple[str, ...], at: AcceptedCoordinate
) -> dict[str, dict[str, ClaimLineageNode]]:
    """The lineage as a walk of every generation reads it: the reference answer."""

    found: dict[str, dict[str, ClaimLineageNode]] = {path: {} for path in paths}
    last: dict[str, bytes | None] = dict.fromkeys(paths)
    for generation in instance.accepted_history():
        raws = instance.blobs_at(generation.oid, paths)
        coordinate = AcceptedCoordinate.from_internal(instance.coordinate_for_oid(generation.oid))
        for path in paths:
            raw = raws.get(path)
            if raw is None or raw == last[path]:
                continue
            last[path] = raw
            claim = parse_claim(raw, path=path)
            digest = claim_artifact_digest(claim).tagged
            found[path].setdefault(
                digest, ClaimLineageNode(claim, digest, coordinate, generation.sequence)
            )
        if generation.oid == at.git_oid:
            break
    return found


def test_revised_retired_and_re_added_lineages_match_the_all_generations_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    kept, revised, retired = _write(
        instance, _set("kept", "kept"), _set("revised", "first"), _set("retired", "gone")
    )
    stamps = _stamps(instance, [kept, revised, retired])
    _write(instance, _set("revised", "second"))
    _write(instance, {"op": "retire", "target": f"Claim:{retired}"})
    # The same value stated again is a new Claim; the retired one stays retired.
    (re_added,) = _write(instance, _set("retired", "gone"))
    assert re_added != retired
    _write(instance, _set("revised", "third"))
    at, _sequence = _head(instance)
    paths = tuple(claim_path(claim) for claim in (kept, revised, retired, re_added))

    clear_lineage_index(instance)
    assert read_claim_lineages(instance, paths=paths, at=at) == _all_generations(
        instance, paths, at
    )

    shortcut = _check(instance, stamps)
    # The same answers when no backing may take the one-version shortcut.
    original = projection_sync.path_revisions

    def no_single_versions(instance_: Any, *, paths: Any, at: Any) -> Any:
        return {
            path: (*rows, (sys.maxsize, "sha256:" + "f" * 64))
            for path, rows in original(instance_, paths=paths, at=at).items()
        }

    monkeypatch.setattr(projection_sync, "path_revisions", no_single_versions)
    walked = _check(instance, stamps)
    assert shortcut == walked
    (first, *_rest) = shortcut.results
    assert first.status == "unsyncable" and first.reason == "block_backing_retired"
    by_identity = {item.identity.name: item for item in first.issues}
    assert set(by_identity) == {retired}
    assert {backing.identity.name for backing in first.moved_backings} == {revised}
