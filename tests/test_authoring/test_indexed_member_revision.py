"""A member's projected revision from the history index equals the record-derived one."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.compiler.projection_artifacts import projected_revision
from tests.core_support._knowledge_loop_support import seed_claims


def test_indexed_revision_matches_the_records_for_every_member(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    records = instance.member_record_history(
        tuple(
            member.path
            for generation in instance.accepted_history()[1:]
            if generation.record is not None
            for member in generation.record.members
        )
    )
    member_paths = sorted({member.path for _path, record in records for member in record.members})
    assert member_paths
    novel = "sha256:" + "ab" * 32
    checked = 0
    for path in member_paths:
        touching = instance.member_record_history((path,))
        digests = {
            getattr(member, "candidate_artifact_digest", None)
            for _record_path, record in touching
            for member in record.members
            if member.path == path
        } - {None}
        for digest in (*sorted(digests), novel):
            expected = projected_revision(
                touching, path=path, input_digest=digest, artifact_digest=digest
            )
            assert instance.projected_member_revision(path, digest) == expected, (path, digest)
            checked += 1
    assert checked > len(member_paths)
