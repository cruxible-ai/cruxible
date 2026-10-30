"""Obsolete derived schemas refuse; missing current caches rebuild from authority.

Both coordinates belong to the current compiler. Earlier accepted compiler
formats require reseeding, so an old signed instance is not a cache fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.indexes.projection import PROJECTION_STORAGE_SCHEMA_VERSION
from cruxible_core.indexes.sqlite import ProjectionIntegrityError, bind_projection
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._support import initialize_local
from tests.test_indexes.test_resolution_contracts import _accept_tree


def _manifests(root: Path) -> dict[str, int]:
    return {
        path.name: json.loads(path.read_bytes())["storage_schema_version"]
        for path in (root / "projections").glob("projection-*.json")
    }


def test_an_obsolete_derived_schema_refuses_and_current_coordinates_rebuild(tmp_path: Path) -> None:
    current = PROJECTION_STORAGE_SCHEMA_VERSION
    instance, owner = initialize_local(tmp_path)
    genesis = instance.accepted_coordinate()
    tree = instance.tree_at(genesis.git_oid)
    subject = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="cache.test/one"),
        subject_kind="cache.test",
        subject_id="one",
    )
    tree[subject_path("cache.test", "one")] = render_subject(subject)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name="cache-head"
    )
    head = instance.accepted_coordinate()
    assert head != genesis and head.compiler == genesis.compiler
    root = instance.root
    directory = root / "projections"
    stale = {}
    retained = _manifests(root)
    assert len(retained) == 2 and set(retained.values()) == {current}
    for label, coordinate in (("genesis", genesis), ("head", head)):
        manifest = next(
            path
            for path in directory.glob("projection-*.json")
            if json.loads(path.read_bytes())["git_oid"] == coordinate.git_oid
        )
        data = json.loads(manifest.read_bytes())
        # Damage only disposable cache metadata. No ledger/compiler/receipt bytes
        # are changed or reinterpreted under a historical schema.
        data["storage_schema_version"] = current - 1
        obsolete = directory / f"projection-obsolete-{label}.json"
        obsolete.write_text(json.dumps(data))
        stale[obsolete] = obsolete.read_bytes()
        with pytest.raises(ProjectionIntegrityError, match="malformed"):
            bind_projection(obsolete, expected=coordinate)
        manifest.unlink()  # Explicit cache repair: remove the missing-current pointer target.

    reopened = PlaybillInstance.open(root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == head
    with reopened.bind_accepted_projection(head) as projection:
        assert projection.typed.connection.execute("SELECT count(*) FROM subject_keys").fetchone()
        assert (
            projection.typed.envelope(subject.identity.qualified).identity
            == subject.identity.qualified
        )
    with reopened.bind_accepted_projection(genesis) as projection:
        tables = {
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert {"subject_keys", "subject_keys_by_key", "claims_by_predicate"} <= tables
        assert projection.typed.envelope(subject.identity.qualified) is None
    rebuilt = _manifests(root)
    assert sorted(version for version in rebuilt.values() if version == current) == [
        current,
        current,
    ]
    assert set(retained) <= set(rebuilt)
    for path, original in stale.items():
        if path.exists():
            assert path.read_bytes() == original
    assert reopened.accepted_coordinate() == head
