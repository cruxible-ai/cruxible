"""Revision-faithful procedure snapshot and clone semantics."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_core.procedure.types import ProcedureRun
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_promote_procedure,
    service_propose_procedure,
    service_retire_procedure,
)
from tests.test_procedures.conftest import actor, provider_definition


def test_clone_restores_snapshot_time_procedures_but_excludes_runs(
    procedure_instance: CruxibleInstance,
    tmp_path: Path,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("snapshot_action"),
        actor_context=actor("snapshot-proposer"),
    )
    procedure_id = proposed.procedure.procedure_id
    promoted = service_promote_procedure(
        procedure_instance,
        procedure_id,
        expected_version=1,
        actor_context=actor("snapshot-reviewer"),
    )

    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_run(
            ProcedureRun(
                procedure_id=procedure_id,
                definition_digest=promoted.procedure.definition_digest,
            )
        )

    snapshot = procedure_instance.create_snapshot(label="procedure-portability")
    artifact_path = (
        procedure_instance.get_instance_dir()
        / "snapshots"
        / snapshot.snapshot_id
        / "procedures.json"
    )
    artifact = json.loads(artifact_path.read_text())
    assert artifact["format_version"] == 1
    assert [item["procedure_id"] for item in artifact["procedures"]] == [procedure_id]

    service_retire_procedure(
        procedure_instance,
        procedure_id,
        expected_version=2,
        reason="retired after the snapshot",
        actor_context=actor("retiring-reviewer"),
    )
    post_snapshot = service_propose_procedure(
        procedure_instance,
        provider_definition("post_snapshot_action"),
        actor_context=actor("later-proposer"),
    )

    clone, _ = CruxibleInstance.clone_from_snapshot(
        procedure_instance,
        snapshot.snapshot_id,
        tmp_path / "clone",
    )
    clone_store = clone.get_procedure_store()
    try:
        cloned_procedures = clone_store.list_procedures(limit=100)
        assert [item.procedure_id for item in cloned_procedures] == [procedure_id]
        assert cloned_procedures[0].status == "live"
        assert cloned_procedures[0].version == 2
        assert clone_store.get_procedure(post_snapshot.procedure.procedure_id) is None
        assert clone_store.count_runs() == 0
        assert clone_store.list_runs(procedure_id=procedure_id) == []
    finally:
        clone_store.close()
