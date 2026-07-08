"""Auth-off daemons materialize a local operator for auth-managed types.

Auth-managed entity types still enter the graph only through the internal
``token_mint`` source. With server auth enabled, runtime credentials are the
identity source of truth. With server auth disabled, sandbox/local instances get a
declared ``operator`` actor so auth-managed configs are usable without credential
ceremony and provenance remains truthful.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.loader import load_config
from cruxible_core.errors import DirectWriteRefusedError
from cruxible_core.runtime.api import init_governed
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service import (
    service_add_entity_inputs,
    service_backup_instance,
    service_clone_snapshot,
    service_create_snapshot,
    service_create_state_overlay,
    service_publish_state,
    service_pull_state_apply,
    service_pull_state_preview,
    service_restore_instance,
    service_state_status,
)
from cruxible_core.service.lifecycle import (
    service_init,
    service_reload_config,
)
from cruxible_core.service.types import EntityWriteInput
from cruxible_core.workflow.compiler import build_lock, write_lock

AUTH_MANAGED_YAML = """
version: "1.0"
name: auth_managed_demo
enums:
  actor_kind: {values: [human, agent, service_account, system]}
  actor_status: {values: [active, inactive]}
entity_types:
  Actor:
    write_policy: mint_only
    auth_managed: true
    properties:
      actor_id: {type: string, primary_key: true}
      label: {type: string}
      kind: {type: string, enum_ref: actor_kind}
      status: {type: string, enum_ref: actor_status, default: active}
relationships: []
named_queries:
  all_actors:
    mode: collection
    returns: Actor
    result_shape: entity
"""

PLAIN_YAML = """
version: "1.0"
name: plain_demo
entity_types:
  Widget:
    properties:
      widget_id: {type: string, primary_key: true}
      label: {type: string, optional: true}
relationships: []
named_queries:
  all_widgets:
    mode: collection
    returns: Widget
    result_shape: entity
"""

KIT_MANIFEST = """\
schema_version: cruxible.kit.v1
kit_id: auth-managed-kit
version: 0.2.0
role: standalone
entry_config: config.yaml
provider_paths: []
copy_paths: []
requires_extras: []
"""


def _auth_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)


def _auth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")


def _assert_operator_actor(instance: CruxibleInstance) -> None:
    actor = instance.load_graph().get_entity("Actor", "operator")
    assert actor is not None
    assert actor.properties["label"] == "operator"
    assert actor.properties["kind"] == "human"
    assert actor.properties["status"] == "active"
    assert actor.metadata.actor_context is not None
    assert actor.metadata.actor_context.actor_type == "human_user"
    assert actor.metadata.actor_context.actor_id == "operator"
    assert actor.metadata.actor_context.org_id == "local"
    assert actor.metadata.actor_context.operation_id.startswith("op_")


def _assert_no_operator_actor(instance: CruxibleInstance) -> None:
    assert instance.load_graph().get_entity("Actor", "operator") is None


def _actor_write(entity_id: str = "manual") -> EntityWriteInput:
    return EntityWriteInput(
        entity_type="Actor",
        entity_id=entity_id,
        properties={"label": entity_id, "kind": "human"},
    )


class TestAuthManagedLocalOperatorAtInit:
    def test_auth_off_materializes_operator_for_auth_managed_config_at_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        result = service_init(tmp_path / "inst", config_yaml=AUTH_MANAGED_YAML)

        assert "Actor" in result.instance.load_config().entity_types
        _assert_operator_actor(result.instance)

    def test_auth_off_materializes_operator_for_auth_managed_kit_at_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "kit-cache"))
        source = tmp_path / "kit-source"
        source.mkdir()
        source.joinpath("cruxible-kit.yaml").write_text(KIT_MANIFEST)
        source.joinpath("config.yaml").write_text(AUTH_MANAGED_YAML)
        kit_config = load_config(source / "config.yaml")
        write_lock(build_lock(kit_config, source), source / "cruxible.lock.yaml")

        result = service_init(tmp_path / "inst", kits=[f"file://{source}"])

        _assert_operator_actor(result.instance)

    def test_auth_on_accepts_auth_managed_config_without_operator_at_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        result = service_init(tmp_path / "inst", config_yaml=AUTH_MANAGED_YAML)

        assert "Actor" in result.instance.load_config().entity_types
        _assert_no_operator_actor(result.instance)

    def test_auth_off_config_without_auth_managed_types_does_not_materialize_operator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        result = service_init(tmp_path / "inst", config_yaml=PLAIN_YAML)

        assert "Widget" in result.instance.load_config().entity_types
        _assert_no_operator_actor(result.instance)


class TestAuthManagedLocalOperatorAtGovernedUpload:
    def test_auth_off_governed_upload_materializes_operator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
        reset_registry()
        get_manager().clear()
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()

        try:
            registry = get_registry()
            before_count = registry.count_instances()
            result = init_governed(str(workspace_root), config_yaml=AUTH_MANAGED_YAML)

            assert registry.count_instances() == before_count + 1
            _assert_operator_actor(get_manager().get(result.instance_id))
        finally:
            reset_registry()
            get_manager().clear()


class TestAuthManagedLocalOperatorAtReload:
    def test_auth_off_reload_materializes_operator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        instance = service_init(tmp_path / "inst", config_yaml=PLAIN_YAML).instance

        service_reload_config(instance, config_yaml=AUTH_MANAGED_YAML)

        assert "Actor" in instance.load_config().entity_types
        _assert_operator_actor(instance)

    def test_auth_on_reload_does_not_create_operator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        instance = service_init(tmp_path / "inst", config_yaml=PLAIN_YAML).instance

        service_reload_config(instance, config_yaml=AUTH_MANAGED_YAML)

        assert "Actor" in instance.load_config().entity_types
        _assert_no_operator_actor(instance)


class TestAuthManagedLocalOperatorAtRestoreAndClone:
    def _backup_auth_managed_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        _auth_on(monkeypatch)
        instance = service_init(tmp_path / "source", config_yaml=AUTH_MANAGED_YAML).instance
        artifact = tmp_path / "backup.cruxible.zip"
        service_backup_instance(
            instance,
            instance_id="inst_auth_managed",
            artifact_path=artifact,
        )
        return artifact

    def test_auth_off_restore_materializes_operator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifact = self._backup_auth_managed_instance(tmp_path, monkeypatch)

        _auth_off(monkeypatch)
        restored = service_restore_instance(artifact_path=artifact, root_dir=tmp_path / "restored")

        _assert_operator_actor(restored.instance)

    def test_auth_off_snapshot_clone_materializes_operator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        instance = service_init(tmp_path / "source", config_yaml=AUTH_MANAGED_YAML).instance
        snapshot_id = service_create_snapshot(instance).snapshot.snapshot_id

        _auth_off(monkeypatch)
        cloned = service_clone_snapshot(instance, snapshot_id, tmp_path / "clone")

        _assert_operator_actor(cloned.instance)


CASE_BASE_YAML = """\
version: "1.0"
name: case_reference

entity_types:
  Case:
    properties:
      case_id:
        type: string
        primary_key: true
      title:
        type: string

relationships:
  - name: cites
    from: Case
    to: Case
"""

CASE_BASE_WITH_AUTH_MANAGED_YAML = """\
version: "1.0"
name: case_reference

enums:
  actor_kind: {values: [human, agent, service_account, system]}
  actor_status: {values: [active, inactive]}
entity_types:
  Case:
    properties:
      case_id:
        type: string
        primary_key: true
      title:
        type: string
  Actor:
    write_policy: mint_only
    auth_managed: true
    properties:
      actor_id: {type: string, primary_key: true}
      label: {type: string}
      kind: {type: string, enum_ref: actor_kind}
      status: {type: string, enum_ref: actor_status, default: active}

relationships:
  - name: cites
    from: Case
    to: Case
"""

AUTH_MANAGED_OVERLAY_YAML = """\
version: "1.0"
name: case-law-overlay
extends: .cruxible/upstream/current/config.yaml
enums:
  actor_kind: {values: [human, agent, service_account, system]}
  actor_status: {values: [active, inactive]}
entity_types:
  Actor:
    write_policy: mint_only
    auth_managed: true
    properties:
      actor_id: {type: string, primary_key: true}
      label: {type: string}
      kind: {type: string, enum_ref: actor_kind}
      status: {type: string, enum_ref: actor_status, default: active}
relationships: []
"""


def _publish_release(
    tmp_path: Path,
    *,
    config_yaml: str,
    release_id: str = "v1.0.0",
) -> tuple[CruxibleInstance, Path]:
    root = tmp_path / "root-model"
    root.mkdir(exist_ok=True)
    (root / "config.yaml").write_text(config_yaml)
    instance = CruxibleInstance.init(root, "config.yaml")
    release_dir = tmp_path / "releases" / "current"
    service_publish_state(
        instance,
        transport_ref=f"file://{release_dir}",
        state_id="case-law",
        release_id=release_id,
        compatibility="data_only",
    )
    return instance, release_dir


class TestAuthManagedLocalOperatorAtOverlayAndPull:
    def test_auth_off_overlay_create_materializes_operator_from_auth_managed_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        _, release_dir = _publish_release(tmp_path, config_yaml=CASE_BASE_WITH_AUTH_MANAGED_YAML)

        _auth_off(monkeypatch)
        overlay = service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            root_dir=tmp_path / "overlay",
        )

        _assert_operator_actor(overlay.instance)

    def test_auth_off_pull_apply_materializes_operator_from_new_auth_managed_upstream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        root_instance, release_dir = _publish_release(tmp_path, config_yaml=CASE_BASE_YAML)
        overlay_instance = service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            root_dir=tmp_path / "overlay",
        ).instance

        _auth_on(monkeypatch)
        service_reload_config(root_instance, config_yaml=CASE_BASE_WITH_AUTH_MANAGED_YAML)
        successor_dir = tmp_path / "releases" / "successor"
        service_publish_state(
            root_instance,
            transport_ref=f"file://{successor_dir}",
            state_id="case-law",
            release_id="v1.1.0",
            compatibility="data_only",
        )
        shutil.rmtree(release_dir)
        shutil.copytree(successor_dir, release_dir)

        _auth_off(monkeypatch)
        preview = service_pull_state_preview(overlay_instance)
        assert preview.target_release_id == "v1.1.0"
        applied = service_pull_state_apply(
            overlay_instance,
            expected_apply_digest=preview.apply_digest,
        )

        assert applied.release_id == "v1.1.0"
        status = service_state_status(overlay_instance)
        assert status.upstream is not None
        assert status.upstream.release_id == "v1.1.0"
        _assert_operator_actor(overlay_instance)

    def test_auth_off_upstream_reload_materializes_operator_from_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        _, release_dir = _publish_release(tmp_path, config_yaml=CASE_BASE_YAML)
        overlay_instance = service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            root_dir=tmp_path / "overlay",
        ).instance

        service_reload_config(overlay_instance, config_yaml=AUTH_MANAGED_OVERLAY_YAML)

        _assert_operator_actor(overlay_instance)


@pytest.mark.parametrize("auth_enabled", [False, True])
def test_direct_writes_to_auth_managed_actor_remain_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_enabled: bool,
) -> None:
    if auth_enabled:
        _auth_on(monkeypatch)
    else:
        _auth_off(monkeypatch)
    instance = service_init(tmp_path / "inst", config_yaml=AUTH_MANAGED_YAML).instance

    with pytest.raises(DirectWriteRefusedError):
        service_add_entity_inputs(instance, [_actor_write()])
