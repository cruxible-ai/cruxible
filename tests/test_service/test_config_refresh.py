"""Service-layer tests for config-by-reference: pointer init and config refresh."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cruxible_core.config.source_pointer import (
    compute_config_source_digest,
    load_config_source,
)
from cruxible_core.errors import ConfigError, PermissionDeniedError
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.runtime.permissions import (
    PermissionMode,
    check_permission,
    request_permission_scope,
)
from cruxible_core.service import (
    EntityWriteInput,
    service_add_entity_inputs,
    service_config_status,
    service_init,
    service_refresh_config,
    service_reload_config,
)
from cruxible_core.workflow.compiler import build_kit_root_lock, load_lock, write_lock
from tests.test_cli.conftest import CAR_PARTS_YAML

KIT_ID = "refresh-kit"

KIT_MANIFEST_YAML = f"""\
schema_version: cruxible.kit.v1
kit_id: {KIT_ID}
version: 0.1.0
role: standalone
entry_config: config.yaml
provider_paths: [providers]
copy_paths: []
requires_extras: []
"""

BASE_KIT_CONFIG = {
    "version": "1.0",
    "name": "refresh-kit-config",
    "entity_types": {
        "WorkItem": {
            "properties": {
                "work_item_id": {"type": "string", "primary_key": True},
                "status": {"type": "string"},
            },
        },
    },
    "mutation_guards": [
        {
            "name": "guarded_close",
            "entity_type": "WorkItem",
            "property": "status",
            "new_value": "closed",
            "condition": {"type": "actor", "allowed_actor_ids": ["reviewer"]},
        },
    ],
}

EXTRA_GUARD = {
    "name": "guarded_reopen",
    "entity_type": "WorkItem",
    "property": "status",
    "new_value": "open",
    "condition": {"type": "actor", "allowed_actor_ids": ["reviewer"]},
}


def _write_kit_config(kit_root: Path, config: dict) -> None:
    (kit_root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))


@pytest.fixture
def kit_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "kit-cache"))
    monkeypatch.delenv("CRUXIBLE_ALLOWED_ROOTS", raising=False)
    root = tmp_path / "kit-src" / KIT_ID
    root.mkdir(parents=True)
    (root / "cruxible-kit.yaml").write_text(KIT_MANIFEST_YAML)
    _write_kit_config(root, BASE_KIT_CONFIG)
    write_lock(build_kit_root_lock(root), root / "cruxible.lock.yaml")
    return root


@pytest.fixture
def kit_instance(tmp_path: Path, kit_root: Path) -> CruxibleInstance:
    project = tmp_path / "project"
    project.mkdir()
    result = service_init(project, kits=[f"file://{kit_root}"])
    instance = result.instance
    assert isinstance(instance, CruxibleInstance)
    return instance


def _facade_authorize(classification: str) -> None:
    """The exact asymmetric gate shape the runtime facade applies."""
    if classification == "weakened":
        check_permission("cruxible_config_refresh_weakening")


# ---------------------------------------------------------------------------
# Pointer init behavior
# ---------------------------------------------------------------------------


def test_kit_init_writes_pointer_instead_of_flattened_config(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    pointer_path = kit_instance.get_config_source_path()
    assert pointer_path.exists()
    pointer = load_config_source(pointer_path)
    assert [layer.ref for layer in pointer.layers] == [f"file://{kit_root}"]
    # No flattened config anywhere in the instance dir.
    assert not (kit_instance.get_instance_dir() / "configs").exists()
    # The serving config is composed at load and matches the kit layer.
    config = kit_instance.load_config()
    assert "WorkItem" in config.entity_types
    assert [guard.name for guard in config.mutation_guards] == ["guarded_close"]
    # The installed instance lock matches the composed digest.
    lock = load_lock(kit_instance.get_instance_dir() / "cruxible.lock.yaml")
    assert lock.config_digest == kit_instance.load_composed_config_source().composed_digest


def test_pointer_instance_refuses_config_save_and_reload(
    kit_instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError, match="no editable config copy"):
        kit_instance.save_config(kit_instance.load_config())
    with pytest.raises(ConfigError, match="config refresh"):
        service_reload_config(kit_instance, config_yaml=CAR_PARTS_YAML)
    # Validate-only reload (no replacement) still works against the composition.
    result = service_reload_config(kit_instance)
    assert result.updated is False


def test_pre_pointer_instance_loads_with_deprecation_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = tmp_path / "materialized-project"
    project.mkdir()
    (project / "config.yaml").write_text(CAR_PARTS_YAML)
    instance = CruxibleInstance.init(project, "config.yaml")

    with caplog.at_level("WARNING", logger="cruxible_core.runtime.instance"):
        config = instance.load_config()
        instance.load_config()

    assert "Vehicle" in config.entity_types
    warnings = [
        record for record in caplog.records if "materialized config copy" in record.getMessage()
    ]
    # Warned once per instance object, not once per load.
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# config refresh
# ---------------------------------------------------------------------------


def test_tightening_refresh_succeeds_at_graph_write(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    before = kit_instance.load_composed_config_source()
    updated = dict(BASE_KIT_CONFIG)
    updated["mutation_guards"] = [*BASE_KIT_CONFIG["mutation_guards"], EXTRA_GUARD]
    _write_kit_config(kit_root, updated)

    with request_permission_scope(PermissionMode.GRAPH_WRITE):
        check_permission("cruxible_config_refresh")
        result = service_refresh_config(kit_instance, authorize_classification=_facade_authorize)

    assert result.classification == "tightened"
    assert result.before_composed_digest == before.composed_digest
    assert result.after_composed_digest != before.composed_digest
    assert result.pointer_digest == compute_config_source_digest(before.pointer)
    assert any("guarded_reopen" in line for line in result.governance_changes)
    assert [layer["kind"] for layer in result.layers] == ["kit"]
    assert result.layers[0]["ref"] == f"file://{kit_root}"
    assert result.receipt_id is not None

    # The serving config swapped in memory and the lock was rebuilt.
    serving = kit_instance.load_config()
    assert {guard.name for guard in serving.mutation_guards} == {
        "guarded_close",
        "guarded_reopen",
    }
    lock = load_lock(Path(result.lock_path))
    assert lock.config_digest == result.after_composed_digest


def test_weakening_refresh_refused_at_graph_write_and_allowed_at_admin(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    before_digest = kit_instance.load_composed_config_source().composed_digest
    weakened = dict(BASE_KIT_CONFIG)
    weakened["mutation_guards"] = []
    _write_kit_config(kit_root, weakened)

    with request_permission_scope(PermissionMode.GRAPH_WRITE):
        with pytest.raises(PermissionDeniedError):
            service_refresh_config(kit_instance, authorize_classification=_facade_authorize)

    # The refusal left the old composition serving.
    assert kit_instance.load_composed_config_source().composed_digest == before_digest
    assert [guard.name for guard in kit_instance.load_config().mutation_guards] == ["guarded_close"]

    with request_permission_scope(PermissionMode.ADMIN):
        result = service_refresh_config(kit_instance, authorize_classification=_facade_authorize)

    assert result.classification == "weakened"
    assert any("guarded_close" in line for line in result.governance_changes)
    assert kit_instance.load_config().mutation_guards == []


def test_fragment_escaping_containment_is_a_load_error(
    kit_instance: CruxibleInstance, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-fragment.yaml"
    outside.write_text("mutation_guards: []\n")
    pointer_path = kit_instance.get_config_source_path()
    data = yaml.safe_load(pointer_path.read_text())
    data["layers"].append({"kind": "fragment", "path": str(outside)})
    pointer_path.write_text(yaml.safe_dump(data, sort_keys=False))

    with pytest.raises(ConfigError, match="escapes the allowed roots"):
        service_refresh_config(kit_instance)
    # A fresh instance object (daemon restart) fails the same way at load.
    reloaded = CruxibleInstance.load(kit_instance.get_root_path())
    with pytest.raises(ConfigError, match="escapes the allowed roots"):
        reloaded.load_config()


def test_contained_fragment_composes_as_last_layer(
    kit_instance: CruxibleInstance,
) -> None:
    root = kit_instance.get_root_path()
    fragment = root / ".cruxible" / "instance.yaml"
    fragment.write_text(
        yaml.safe_dump(
            {"name": "refresh-kit-config", "mutation_guards": [EXTRA_GUARD]},
            sort_keys=False,
        )
    )
    pointer_path = kit_instance.get_config_source_path()
    data = yaml.safe_load(pointer_path.read_text())
    data["layers"].append({"kind": "fragment", "path": ".cruxible/instance.yaml"})
    pointer_path.write_text(yaml.safe_dump(data, sort_keys=False))

    result = service_refresh_config(kit_instance)

    assert result.classification == "tightened"
    assert [layer["kind"] for layer in result.layers] == ["kit", "fragment"]
    assert {guard.name for guard in kit_instance.load_config().mutation_guards} == {
        "guarded_close",
        "guarded_reopen",
    }


def test_lock_rebuild_failure_leaves_old_config_serving(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    before_digest = kit_instance.load_composed_config_source().composed_digest
    lock_path = kit_instance.get_instance_dir() / "cruxible.lock.yaml"
    lock_bytes_before = lock_path.read_bytes()
    broken = dict(BASE_KIT_CONFIG)
    broken["contracts"] = {"NoInput": {"fields": {}}}
    broken["providers"] = {
        "missing_provider": {
            "ref": "kit://providers/missing.py::run",
            "version": "1.0",
            "contract_in": "NoInput",
            "contract_out": "NoInput",
        }
    }
    _write_kit_config(kit_root, broken)

    with pytest.raises(ConfigError):
        service_refresh_config(kit_instance)

    # Old composition and old lock still serving; no receipt-committed swap.
    assert kit_instance.load_composed_config_source().composed_digest == before_digest
    assert lock_path.read_bytes() == lock_bytes_before
    assert [guard.name for guard in kit_instance.load_config().mutation_guards] == ["guarded_close"]


def test_refresh_receipt_records_pointer_layers_digests_and_diff(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    updated = dict(BASE_KIT_CONFIG)
    updated["mutation_guards"] = [*BASE_KIT_CONFIG["mutation_guards"], EXTRA_GUARD]
    _write_kit_config(kit_root, updated)

    result = service_refresh_config(kit_instance)

    assert result.receipt_id is not None
    receipt = kit_instance.get_receipt_store().get_receipt(result.receipt_id)
    assert receipt is not None
    assert receipt.operation_type == "config_refresh"
    assert receipt.committed is True
    assert receipt.actor_context is None  # local auth-off path fabricates no actor
    # The semantic refresh record rides a validation node: receipt parameters
    # are subject to mutation-payload retention (redacted by default).
    validation_nodes = [node for node in receipt.nodes if node.node_type == "validation"]
    assert len(validation_nodes) == 1
    detail = validation_nodes[0].detail
    assert detail["pointer_digest"] == result.pointer_digest
    assert detail["before_composed_digest"] == result.before_composed_digest
    assert detail["after_composed_digest"] == result.after_composed_digest
    assert detail["classification"] == "tightened"
    assert detail["layers"] == [
        {
            "kind": "kit",
            "ref": f"file://{kit_root}",
            "digest": result.layers[0]["digest"],
        }
    ]
    assert detail["layers"][0]["digest"].startswith("sha256:")
    assert any("guarded_reopen" in line for line in detail["governance_diff"])


def test_refresh_requires_a_source_pointer(tmp_path: Path) -> None:
    project = tmp_path / "materialized-project"
    project.mkdir()
    (project / "config.yaml").write_text(CAR_PARTS_YAML)
    instance = CruxibleInstance.init(project, "config.yaml")

    with pytest.raises(ConfigError, match="no config source pointer"):
        service_refresh_config(instance)


def test_noop_refresh_is_neutral_and_receipted(kit_instance: CruxibleInstance) -> None:
    result = service_refresh_config(kit_instance)
    assert result.classification == "neutral"
    assert result.governance_changes == []
    assert result.before_composed_digest == result.after_composed_digest
    assert result.receipt_id is not None


# ---------------------------------------------------------------------------
# config status
# ---------------------------------------------------------------------------


def test_status_on_pointer_instance_without_drift(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    serving = kit_instance.load_composed_config_source()

    result = service_config_status(kit_instance)

    assert result.source == "pointer"
    assert result.serving_composed_digest == serving.composed_digest
    assert result.receipted_composed_digest == serving.composed_digest
    assert result.recomposed_digest == serving.composed_digest
    assert result.pointer_digest == serving.pointer_digest
    assert result.layers == [
        {"kind": "kit", "ref": f"file://{kit_root}", "digest": serving.layers[0].digest}
    ]
    assert result.drift is False
    assert result.drift_classification is None
    assert result.drift_changes == []
    assert result.serving_matches_receipt is True


def test_status_detects_and_classifies_source_drift(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    serving_digest = kit_instance.load_composed_config_source().composed_digest
    updated = dict(BASE_KIT_CONFIG)
    updated["mutation_guards"] = [*BASE_KIT_CONFIG["mutation_guards"], EXTRA_GUARD]
    _write_kit_config(kit_root, updated)

    result = service_config_status(kit_instance)

    assert result.drift is True
    assert result.drift_classification == "tightened"
    assert any("guarded_reopen" in line for line in result.drift_changes)
    assert result.recomposed_digest != serving_digest
    # Status is read-only: the serving composition did not move.
    assert kit_instance.load_composed_config_source().composed_digest == serving_digest
    assert result.serving_matches_receipt is True

    # A receipted refresh clears the drift.
    service_refresh_config(kit_instance)
    assert service_config_status(kit_instance).drift is False


def test_status_on_restarted_instance_classifies_drift_against_receipt(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    weakened = dict(BASE_KIT_CONFIG)
    weakened["mutation_guards"] = []
    _write_kit_config(kit_root, weakened)

    # A fresh instance object (daemon restart) serves the drifted composition,
    # so serving == recomposed; drift is detected against the receipted digest
    # and classified by reconstructing the receipted side from the
    # materialized kit copies.
    restarted = CruxibleInstance.load(kit_instance.get_root_path())
    result = service_config_status(restarted)

    assert result.drift is True
    assert result.drift_classification == "weakened"
    assert any("guarded_close" in line for line in result.drift_changes)
    assert result.serving_matches_receipt is False


def test_status_on_pre_pointer_instance_reports_materialized(tmp_path: Path) -> None:
    project = tmp_path / "materialized-project"
    project.mkdir()
    (project / "config.yaml").write_text(CAR_PARTS_YAML)
    instance = CruxibleInstance.init(project, "config.yaml")

    result = service_config_status(instance)

    assert result.source == "materialized (pre-pointer)"
    assert result.serving_composed_digest.startswith("sha256:")
    assert result.pointer_digest is None
    assert result.layers == []
    assert result.drift is False
    assert result.drift_classification is None
    assert result.serving_matches_receipt is None


# ---------------------------------------------------------------------------
# warn-on-start drift check
# ---------------------------------------------------------------------------


def test_load_warns_once_when_source_drifted_past_receipt(
    kit_instance: CruxibleInstance,
    kit_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    updated = dict(BASE_KIT_CONFIG)
    updated["mutation_guards"] = [*BASE_KIT_CONFIG["mutation_guards"], EXTRA_GUARD]
    _write_kit_config(kit_root, updated)

    restarted = CruxibleInstance.load(kit_instance.get_root_path())
    with caplog.at_level("WARNING", logger="cruxible_core.runtime.instance"):
        restarted.load_config()
        restarted.load_config()

    warnings = [
        record
        for record in caplog.records
        if "drifted without a receipted refresh" in record.getMessage()
    ]
    # Warned once per instance object, not once per load.
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(kit_instance.get_root_path()) in message
    assert "classification: tightened" in message


def test_load_does_not_warn_without_drift(
    kit_instance: CruxibleInstance,
    caplog: pytest.LogCaptureFixture,
) -> None:
    restarted = CruxibleInstance.load(kit_instance.get_root_path())
    with caplog.at_level("WARNING", logger="cruxible_core.runtime.instance"):
        restarted.load_config()

    assert not any(
        "drifted without a receipted refresh" in record.getMessage() for record in caplog.records
    )


# ---------------------------------------------------------------------------
# write-path verification
# ---------------------------------------------------------------------------


def test_mutations_fail_closed_when_serving_config_is_not_receipted(
    kit_instance: CruxibleInstance, kit_root: Path
) -> None:
    weakened = dict(BASE_KIT_CONFIG)
    weakened["mutation_guards"] = []
    _write_kit_config(kit_root, weakened)

    # A restarted daemon serves the drifted (never-receipted) composition.
    restarted = CruxibleInstance.load(kit_instance.get_root_path())
    entity = EntityWriteInput(
        entity_type="WorkItem",
        entity_id="WI-1",
        properties={"work_item_id": "WI-1", "status": "open"},
    )

    with pytest.raises(ConfigError, match="does not match the last receipted"):
        service_add_entity_inputs(restarted, [entity])
    assert restarted.load_graph().entity_count() == 0

    # The receipted refresh is the exit: it is exempt from the check, and a
    # successful refresh re-opens the write path.
    with request_permission_scope(PermissionMode.ADMIN):
        service_refresh_config(restarted, authorize_classification=_facade_authorize)
    result = service_add_entity_inputs(restarted, [entity])
    assert result.added == 1


def test_mutations_pass_verification_on_receipted_instance(
    kit_instance: CruxibleInstance,
) -> None:
    result = service_add_entity_inputs(
        kit_instance,
        [
            EntityWriteInput(
                entity_type="WorkItem",
                entity_id="WI-1",
                properties={"work_item_id": "WI-1", "status": "open"},
            )
        ],
    )
    assert result.added == 1
