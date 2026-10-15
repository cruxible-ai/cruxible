"""A retained projection of an earlier physical schema never binds as the current one.

The fixture is an instance an earlier build (``history.json``: ``built_by``)
wrote under typed projection storage schema 14: two accepted generations, each
with its retained projection. Schema 15 added the whole-key Subject index and a
predicate index, so this build names its projections differently, and binding
either coordinate -- the head on reopen, the retained genesis on a historical
read -- rebuilds it under schema 15 instead of reading the old physical schema
(review F-004).
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from cruxible_client.contracts.projection import AcceptedProjectionCoordinate
from cruxible_client.contracts.types import TrustRoot
from cruxible_core.indexes.projection import PROJECTION_STORAGE_SCHEMA_VERSION
from cruxible_core.runtime.instance import PlaybillInstance

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "storage_schema_14_history"


def _historical_instance(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    history = json.loads((FIXTURE / "history.json").read_text())
    assert history["storage_schema_version"] == 14
    with tarfile.open(FIXTURE / "instance.tar.gz") as archive:
        archive.extractall(tmp_path, filter="data")
    root = tmp_path / str(history["instance_root"])
    config = root / "ledger.git" / "config"
    config.write_text(config.read_text().replace("@INSTANCE_ROOT@", str(root)))
    return root, history


def _manifests(root: Path) -> dict[str, int]:
    return {
        path.name: json.loads(path.read_bytes())["storage_schema_version"]
        for path in (root / "projections").glob("projection-*.json")
    }


def test_a_retained_schema_14_projection_is_rebuilt_not_bound(tmp_path: Path) -> None:
    current = PROJECTION_STORAGE_SCHEMA_VERSION
    assert current > 14
    root, history = _historical_instance(tmp_path)
    retained = _manifests(root)
    assert set(retained.values()) == {14} and len(retained) == 2

    instance = PlaybillInstance.open(
        root, trust_root=TrustRoot.model_validate(history["trust_root"])
    )

    def at(recorded: object) -> AcceptedProjectionCoordinate:
        coordinate = AcceptedProjectionCoordinate.model_validate(recorded)
        return instance.resolve_accepted_coordinate(
            git_oid=coordinate.git_oid,
            semantic_root=coordinate.semantic_root,
            generation_root=coordinate.generation_root,
            compiler_digest=coordinate.compiler.rule_digest,
        )

    head, genesis = at(history["head"]), at(history["genesis"])
    assert instance.accepted_coordinate() == head
    with instance.bind_accepted_projection(head) as projection:
        assert projection.typed.connection.execute("SELECT count(*) FROM subject_keys").fetchone()
    # The retained genesis projection was written under schema 14; a historical
    # read rebuilds it under schema 15 rather than binding the old schema.
    with instance.bind_accepted_projection(genesis) as projection:
        tables = {
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert {"subject_keys", "subject_keys_by_key", "claims_by_predicate"} <= tables

    # Head and genesis each hold a current-schema projection now; the schema-14
    # ones are never bound (reopen may sweep them).
    rebuilt = _manifests(root)
    assert sorted(version for version in rebuilt.values() if version == current) == [
        current,
        current,
    ]
    assert len(set(rebuilt) - set(retained)) == 2
