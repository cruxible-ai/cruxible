"""Service-layer tests for `config adopt`: materialized-instance migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cruxible_core.config.source_pointer import load_config_source
from cruxible_core.errors import ConfigError
from cruxible_core.kits import materialize_kit
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import service_adopt_config, service_refresh_config
from cruxible_core.workflow.compiler import load_lock
from tests.test_service.test_config_refresh import (
    BASE_KIT_CONFIG,
    EXTRA_GUARD,
    KIT_ID,
    _write_kit_config,
    kit_instance,  # noqa: F401  (fixture re-export)
    kit_root,  # noqa: F401  (fixture re-export)
)


@pytest.fixture
def legacy_instance(tmp_path: Path, kit_root: Path) -> CruxibleInstance:  # noqa: F811
    """A pre-pointer instance shaped like a legacy `init --kit`: materialized
    kit dir plus a flattened editable config.yaml, no source pointer."""
    project = tmp_path / "legacy-project"
    project.mkdir()
    materialize_kit(
        kit=f"file://{kit_root}",
        root=project / "kits" / KIT_ID,
        expected_role="standalone",
    )
    (project / "config.yaml").write_text(yaml.safe_dump(BASE_KIT_CONFIG, sort_keys=False))
    return CruxibleInstance.init(project, "config.yaml")


def _tighten_kit_source(kit_root: Path) -> None:  # noqa: F811
    updated = dict(BASE_KIT_CONFIG)
    updated["mutation_guards"] = [*BASE_KIT_CONFIG["mutation_guards"], EXTRA_GUARD]
    _write_kit_config(kit_root, updated)


def test_adopt_preview_shows_accumulated_drift_and_touches_nothing(
    legacy_instance: CruxibleInstance,
    kit_root: Path,  # noqa: F811
) -> None:
    _tighten_kit_source(kit_root)

    result = service_adopt_config(legacy_instance, kits=[f"file://{kit_root}"])

    assert result.applied is False
    assert result.receipt_id is None
    assert result.classification == "tightened"
    assert any("guarded_reopen" in line for line in result.governance_changes)
    # The preview carries the FULL config diff, not just the governance subset.
    assert any("guarded_reopen" in line for line in result.config_diff)
    assert result.before_composed_digest != result.after_composed_digest
    # Nothing changed on disk: no pointer, config.yaml intact, no receipt.
    assert not legacy_instance.has_config_source()
    assert (legacy_instance.get_root_path() / "config.yaml").exists()
    assert not (legacy_instance.get_root_path() / "config.materialized.bak").exists()
    # The stale materialized kit copy was not touched by the preview.
    kit_config = yaml.safe_load(
        (legacy_instance.get_root_path() / "kits" / KIT_ID / "config.yaml").read_text()
    )
    assert [guard["name"] for guard in kit_config["mutation_guards"]] == ["guarded_close"]


def test_adopt_happy_path_migrates_the_instance(
    legacy_instance: CruxibleInstance,
    kit_root: Path,  # noqa: F811
) -> None:
    _tighten_kit_source(kit_root)
    root = legacy_instance.get_root_path()

    result = service_adopt_config(legacy_instance, kits=[f"file://{kit_root}"], accept=True)

    assert result.applied is True
    assert result.classification == "tightened"
    # The pointer was written with the declared layer refs.
    pointer = load_config_source(legacy_instance.get_config_source_path())
    assert [layer.ref for layer in pointer.layers] == [f"file://{kit_root}"]
    assert legacy_instance.has_config_source()
    # kits/<kit_id>/ was re-materialized from the resolved bundle (this is
    # what delivers provider/config updates accumulated since init).
    kit_config = yaml.safe_load((root / "kits" / KIT_ID / "config.yaml").read_text())
    assert [guard["name"] for guard in kit_config["mutation_guards"]] == [
        "guarded_close",
        "guarded_reopen",
    ]
    assert not (root / "kits" / f"{KIT_ID}.adopt-bak").exists()
    # The lock was rebuilt against the adopted composition.
    lock = load_lock(Path(result.lock_path))
    assert lock.config_digest == result.after_composed_digest
    # The receipt records the pointer/layer/composed digests and classification.
    assert result.receipt_id is not None
    receipt = legacy_instance.get_receipt_store().get_receipt(result.receipt_id)
    assert receipt is not None
    assert receipt.operation_type == "config_adopt"
    assert receipt.committed is True
    validation_nodes = [node for node in receipt.nodes if node.node_type == "validation"]
    assert len(validation_nodes) == 1
    detail = validation_nodes[0].detail
    assert detail["pointer_digest"] == result.pointer_digest
    assert detail["before_composed_digest"] == result.before_composed_digest
    assert detail["after_composed_digest"] == result.after_composed_digest
    assert detail["classification"] == "tightened"
    assert detail["layers"] == result.layers
    # The materialized config was retired, never to be read again.
    assert not (root / "config.yaml").exists()
    assert (root / "config.materialized.bak").exists()
    assert result.config_backup_path == str(root / "config.materialized.bak")
    # The instance serves the adopted composition and refresh now works.
    assert {guard.name for guard in legacy_instance.load_config().mutation_guards} == {
        "guarded_close",
        "guarded_reopen",
    }
    refresh = service_refresh_config(legacy_instance)
    assert refresh.classification == "neutral"
    # A fresh instance object (daemon restart) serves the same composition.
    restarted = CruxibleInstance.load(root)
    assert restarted.has_config_source()
    assert restarted.load_composed_config_source().composed_digest == result.after_composed_digest


def test_adopt_without_acceptance_never_applies(
    legacy_instance: CruxibleInstance,
    kit_root: Path,  # noqa: F811
) -> None:
    before_metadata = (legacy_instance.get_instance_dir() / "instance.json").read_text()

    result = service_adopt_config(legacy_instance, kits=[f"file://{kit_root}"])

    assert result.applied is False
    assert result.receipt_id is None
    assert not legacy_instance.has_config_source()
    assert (legacy_instance.get_instance_dir() / "instance.json").read_text() == before_metadata


def test_adopt_failure_leaves_the_instance_exactly_as_it_was(
    legacy_instance: CruxibleInstance,
    kit_root: Path,  # noqa: F811
) -> None:
    root = legacy_instance.get_root_path()
    config_bytes_before = (root / "config.yaml").read_bytes()
    kit_config_before = (root / "kits" / KIT_ID / "config.yaml").read_text()
    metadata_before = (legacy_instance.get_instance_dir() / "instance.json").read_text()
    # A kit whose lock rebuild fails (provider file missing) aborts the adopt
    # AFTER the kit dirs were re-materialized, exercising the rollback.
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
        service_adopt_config(legacy_instance, kits=[f"file://{kit_root}"], accept=True)

    # Exactly as it was: no pointer, original config.yaml, no backup, the
    # original materialized kit copy restored, metadata untouched.
    assert not legacy_instance.has_config_source()
    assert (root / "config.yaml").read_bytes() == config_bytes_before
    assert not (root / "config.materialized.bak").exists()
    assert (root / "kits" / KIT_ID / "config.yaml").read_text() == kit_config_before
    assert not (root / "kits" / f"{KIT_ID}.adopt-bak").exists()
    assert (legacy_instance.get_instance_dir() / "instance.json").read_text() == metadata_before


def test_adopt_refuses_pointer_instances(
    kit_instance: CruxibleInstance,  # noqa: F811
    kit_root: Path,  # noqa: F811
) -> None:
    with pytest.raises(ConfigError, match="already serves its config from a source pointer"):
        service_adopt_config(kit_instance, kits=[f"file://{kit_root}"], accept=True)


def test_adopt_requires_at_least_one_kit(legacy_instance: CruxibleInstance) -> None:  # noqa: F811
    with pytest.raises(ConfigError, match="at least one kit"):
        service_adopt_config(legacy_instance, kits=["  "])


def test_adopt_composes_the_fragment_as_the_last_layer(
    legacy_instance: CruxibleInstance,
    kit_root: Path,  # noqa: F811
) -> None:
    root = legacy_instance.get_root_path()
    fragment = root / ".cruxible" / "instance.yaml"
    fragment.write_text(
        yaml.safe_dump(
            {"name": "refresh-kit-config", "mutation_guards": [EXTRA_GUARD]},
            sort_keys=False,
        )
    )

    result = service_adopt_config(
        legacy_instance,
        kits=[f"file://{kit_root}"],
        fragment=".cruxible/instance.yaml",
        accept=True,
    )

    assert result.applied is True
    assert [layer["kind"] for layer in result.layers] == ["kit", "fragment"]
    assert {guard.name for guard in legacy_instance.load_config().mutation_guards} == {
        "guarded_close",
        "guarded_reopen",
    }
