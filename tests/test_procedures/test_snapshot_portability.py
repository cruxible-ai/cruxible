"""Revision-faithful procedure snapshot and clone semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.procedure.types import ProcedureDefinition, ProcedureRun
from cruxible_core.runtime.instance import (
    _SUPPORTED_PROCEDURES_SNAPSHOT_FORMATS,
    CruxibleInstance,
    _load_snapshot_procedures,
)
from cruxible_core.service import (
    service_accept_procedure,
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
    accepted = service_accept_procedure(
        procedure_instance,
        procedure_id,
        expected_version=1,
        actor_context=actor("snapshot-reviewer"),
    )

    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_run(
            ProcedureRun(
                procedure_id=procedure_id,
                definition_digest=accepted.procedure.definition_digest,
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
    assert artifact["format_version"] == 2
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


def test_clone_from_pre_procedures_snapshot_yields_empty_table(
    procedure_instance: CruxibleInstance, tmp_path: Path
) -> None:
    snapshot = procedure_instance.create_snapshot(label="legacy-shape")
    artifact_path = (
        procedure_instance.get_instance_dir()
        / "snapshots"
        / snapshot.snapshot_id
        / "procedures.json"
    )
    artifact_path.unlink()

    clone, _ = CruxibleInstance.clone_from_snapshot(
        procedure_instance,
        snapshot.snapshot_id,
        tmp_path / "legacy-clone",
    )
    clone_store = clone.get_procedure_store()
    try:
        assert clone_store.list_procedures(limit=10) == []
        assert clone_store.count_runs() == 0
    finally:
        clone_store.close()


def _accepted(
    instance: CruxibleInstance,
    definition: ProcedureDefinition,
    proposer: str,
) -> str:
    proposed = service_propose_procedure(
        instance,
        definition,
        actor_context=actor(proposer),
    )
    service_accept_procedure(
        instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor(f"{proposer}-reviewer"),
    )
    return proposed.procedure.procedure_id


def _v2_definition(name: str) -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            **provider_definition(name).model_dump(mode="json", by_alias=True, exclude_none=True),
            "graph_format": 2,
        }
    )


def test_t5_a_v2_procedure_survives_snapshot_and_clone_with_its_pins(
    procedure_instance: CruxibleInstance,
    tmp_path: Path,
) -> None:
    """Pins ride the artifact, or a clone silently produces unrunnable rows.

    A restored v2 procedure with no recorded accepted world is refused by its
    own fail-closed rule, so "the definitions survived" is not enough.
    """
    v2_id = _accepted(procedure_instance, _v2_definition("portable_v2"), "v2-author")
    v1_id = _accepted(procedure_instance, provider_definition("portable_v1"), "v1-author")

    source_store = procedure_instance.get_procedure_store()
    try:
        expected_pins = source_store.list_acceptance_node_pins(v2_id)
        v2_digest = source_store.get_procedure(v2_id)
        assert v2_digest is not None
    finally:
        source_store.close()
    assert expected_pins

    snapshot = procedure_instance.create_snapshot(label="v2-portability")
    artifact = json.loads(
        (
            procedure_instance.get_instance_dir()
            / "snapshots"
            / snapshot.snapshot_id
            / "procedures.json"
        ).read_text()
    )
    assert artifact["format_version"] == 2
    assert v2_id in artifact["node_pins"]

    clone, _ = CruxibleInstance.clone_from_snapshot(
        procedure_instance,
        snapshot.snapshot_id,
        tmp_path / "v2-clone",
    )
    clone_store = clone.get_procedure_store()
    try:
        restored = clone_store.get_procedure(v2_id)
        assert restored is not None
        assert restored.definition_digest == v2_digest.definition_digest
        assert restored.definition.graph_format == 2
        assert restored.definition_format_version == 2
        assert clone_store.list_acceptance_node_pins(v2_id) == expected_pins

        # A v1 procedure survives with its digest and its COARSE pins, which
        # remain authoritative for it.
        restored_v1 = clone_store.get_procedure(v1_id)
        assert restored_v1 is not None
        assert restored_v1.acceptance_config_digest is not None
        assert restored_v1.acceptance_lock_digest is not None
    finally:
        clone_store.close()


def test_the_reader_registry_accepts_version_one_and_refuses_the_unknown(
    procedure_instance: CruxibleInstance,
) -> None:
    """A 0.4 core must read 0.3 snapshots; that is the upgrade path.

    Refusing an unknown version is the other half: a reader that guessed would
    be exactly the failure the format discriminator exists to prevent.
    """
    assert _SUPPORTED_PROCEDURES_SNAPSHOT_FORMATS == frozenset({1, 2})
    v1_artifact = json.dumps({"format_version": 1, "procedures": []}).encode("utf-8")
    assert _load_snapshot_procedures(v1_artifact, snapshot_id="SNP-legacy") == []

    future = json.dumps({"format_version": 3, "procedures": []}).encode("utf-8")
    with pytest.raises(ConfigError, match="supported: 1, 2"):
        _load_snapshot_procedures(future, snapshot_id="SNP-future")
