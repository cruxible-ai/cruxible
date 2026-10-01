"""Pinned reads of older coordinates never push the serving head's stamp out.

Only the newest few source-authentication stamps are kept beside the pieces, and
only stamps on disk when a process first binds there are trusted. Each pinned
read of an older coordinate that has to authenticate its piece writes a stamp of
its own; once more of those than the ring holds had landed, the head's stamp was
gone, and the next process to bind the head re-derived every typed row from
source (15-17 s on real state). The head's stamp is now kept however many older
stamps arrive, and protection follows the serving pointer when the head moves.

Each older-coordinate authentication is represented by the stamp write it ends
with (`_record_authentication_stamp`, the call `require_source_authentication`
makes); the seeded world has fewer accepted generations than the ring holds.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cruxible_core.indexes import sqlite as playbill_projection
from cruxible_core.indexes import typed_sqlite
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate, ProjectionManifest
from cruxible_core.indexes.serving import (
    SERVING_MANIFEST_FILE,
    load_serving_manifest,
    render_serving_manifest,
)
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._knowledge_loop_support import seed_claims

_RING = playbill_projection._SOURCE_AUTHENTICATION_STAMPS_RETAINED


def _count_authentications(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = typed_sqlite.authenticate_source_rows

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(typed_sqlite, "authenticate_source_rows", counted)
    return calls


def _older_coordinate_reads(
    directory: Path,
    accepted: AcceptedProjectionCoordinate,
    manifest: ProjectionManifest,
    count: int,
) -> None:
    """Land ``count`` distinct older-coordinate authentication stamps."""

    for index in range(count):
        older = accepted.model_copy(
            update={"git_oid": hashlib.sha256(f"older-{index}".encode()).hexdigest()}
        )
        playbill_projection._record_authentication_stamp(directory, older, manifest)


def _stamps(directory: Path) -> list[dict[str, object]]:
    path = directory / playbill_projection.SOURCE_AUTHENTICATION_STAMPS
    loaded = json.loads(path.read_bytes())
    assert isinstance(loaded, list)
    return loaded


def _bind_head(instance: PlaybillInstance) -> None:
    with instance.bind_accepted_projection(instance.accepted_coordinate()):
        pass


def test_a_head_read_after_many_older_reads_does_not_reauthenticate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    head = instance.accepted_coordinate()
    with instance.bind_accepted_projection(head) as handle:
        directory = handle.index_path.parent
        manifest = handle.manifest
    head_stamp = playbill_projection._authentication_stamp(head, manifest)

    _older_coordinate_reads(directory, head, manifest, _RING + 3)

    calls = _count_authentications(monkeypatch)
    playbill_projection.reset_projection_verification_memo()  # a new process
    _bind_head(instance)
    assert calls == []
    stamps = _stamps(directory)
    assert len(stamps) == _RING
    assert head_stamp in stamps


def test_protection_follows_the_serving_pointer_and_stays_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    first_head = instance.accepted_coordinate()
    with instance.bind_accepted_projection(first_head) as handle:
        directory = handle.index_path.parent
        first_manifest = handle.manifest
    first_stamp = playbill_projection._authentication_stamp(first_head, first_manifest)

    # Move the serving pointer to an earlier generation's projection, as an
    # activation moves it to a new one.
    earlier = instance.coordinate_for_oid(instance.accepted_history()[-2].oid)
    with instance.bind_accepted_projection(earlier) as handle:
        earlier_manifest = handle.manifest
        earlier_manifest_path = handle.manifest_path
    earlier_stamp = playbill_projection._authentication_stamp(earlier, earlier_manifest)
    assert earlier_stamp in _stamps(directory)
    serving = load_serving_manifest(directory).model_copy(
        update={
            "git_oid": earlier.git_oid,
            "semantic_root": earlier.semantic_root,
            "generation_root": earlier.generation_root,
            "compiler_digest": earlier.compiler.rule_digest,
            "schema_version": earlier.compiler.schema_version,
            "projection_manifest_name": earlier_manifest_path.name,
            "projection_manifest_digest": "sha256:"
            + hashlib.sha256(earlier_manifest_path.read_bytes()).hexdigest(),
            "logical_digest": earlier_manifest.logical_digest,
        }
    )
    serving_path = directory / SERVING_MANIFEST_FILE
    serving_path.chmod(0o600)
    serving_path.write_bytes(render_serving_manifest(serving))

    _older_coordinate_reads(directory, first_head, first_manifest, _RING + 3)

    calls = _count_authentications(monkeypatch)
    playbill_projection.reset_projection_verification_memo()  # a new process
    with instance.bind_accepted_projection(earlier):
        pass
    assert calls == []
    stamps = _stamps(directory)
    assert len(stamps) == _RING
    assert earlier_stamp in stamps
    # The former head is an ordinary older stamp now and ages out.
    assert first_stamp not in stamps
