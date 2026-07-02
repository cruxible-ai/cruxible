"""Auth-off daemons must LOUDLY refuse configs declaring auth-managed types.

Auth-managed entity types materialize only from runtime-credential mints, which
require ``CRUXIBLE_SERVER_AUTH=true``. On an auth-off daemon no mint can ever
happen, so such a type is permanently empty and unwritable. The daemon must refuse
the config at init/reload rather than fail silently with empty queries.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.loader import load_config
from cruxible_core.errors import ConfigError
from cruxible_core.service import (
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
from cruxible_core.workflow.compiler import build_lock, write_lock

AUTH_MANAGED_YAML = """
version: "1.0"
name: auth_managed_demo
entity_types:
  Actor:
    write_policy: mint_only
    auth_managed: true
    properties:
      actor_id: {type: string, primary_key: true}
      label: {type: string, optional: true}
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

KIT_MANIFEST = (
    "schema_version: cruxible.kit.v1\n"
    "kit_id: auth-managed-kit\n"
    "version: 0.2.0\n"
    "role: standalone\n"
    "entry_config: config.yaml\n"
    "provider_paths: []\n"
    "copy_paths: []\n"
    "requires_extras: []\n"
)


def _auth_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)


def _auth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")


class TestAuthManagedRefusalAtInit:
    def test_auth_off_refuses_auth_managed_config_at_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        with pytest.raises(ConfigError) as exc:
            service_init(tmp_path / "inst", config_yaml=AUTH_MANAGED_YAML)
        message = str(exc.value)
        assert "Actor" in message
        assert "CRUXIBLE_SERVER_AUTH=true" in message
        assert "auth_managed: true" in message
        assert "write_policy: mint_only" in message
        # Refused before the managed config or instance is written: the root is
        # untouched, so a corrected retry is not blocked by "already exists".
        assert not (tmp_path / "inst").exists()

    def test_auth_off_refuses_auth_managed_kit_at_init(
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

        with pytest.raises(ConfigError) as exc:
            service_init(tmp_path / "inst", kit=f"file://{source}")
        message = str(exc.value)
        assert "Actor" in message
        # Refused BEFORE kit materialization: nothing was copied into the root.
        assert not (tmp_path / "inst").exists()

    def test_auth_on_accepts_auth_managed_config_at_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        result = service_init(tmp_path / "inst", config_yaml=AUTH_MANAGED_YAML)
        assert "Actor" in result.instance.load_config().entity_types

    def test_auth_off_accepts_config_without_auth_managed_types(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        result = service_init(tmp_path / "inst", config_yaml=PLAIN_YAML)
        assert "Widget" in result.instance.load_config().entity_types


class TestAuthManagedRefusalAtReload:
    def test_auth_off_refuses_auth_managed_config_at_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Create a clean instance while auth is ON, then flip auth OFF and try to
        # reload an auth-managed config into it.
        _auth_on(monkeypatch)
        result = service_init(tmp_path / "inst", config_yaml=PLAIN_YAML)
        instance = result.instance

        _auth_off(monkeypatch)
        with pytest.raises(ConfigError) as exc:
            service_reload_config(instance, config_yaml=AUTH_MANAGED_YAML)
        message = str(exc.value)
        assert "Actor" in message
        assert "CRUXIBLE_SERVER_AUTH=true" in message
        # The refused reload did not overwrite the instance's active config.
        assert "Widget" in instance.load_config().entity_types

    def test_auth_on_accepts_auth_managed_config_at_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        result = service_init(tmp_path / "inst", config_yaml=PLAIN_YAML)
        instance = result.instance

        service_reload_config(instance, config_yaml=AUTH_MANAGED_YAML)
        assert "Actor" in instance.load_config().entity_types


class TestAuthManagedRefusalAtRestoreAndClone:
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

    def test_auth_off_refuses_auth_managed_config_at_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifact = self._backup_auth_managed_instance(tmp_path, monkeypatch)

        _auth_off(monkeypatch)
        target = tmp_path / "restored"
        with pytest.raises(ConfigError) as exc:
            service_restore_instance(artifact_path=artifact, root_dir=target)
        message = str(exc.value)
        assert "Actor" in message
        assert "CRUXIBLE_SERVER_AUTH=true" in message
        # Refused before any file was staged: the restore target was never created.
        assert not target.exists()

    def test_auth_on_accepts_auth_managed_config_at_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifact = self._backup_auth_managed_instance(tmp_path, monkeypatch)

        restored = service_restore_instance(
            artifact_path=artifact, root_dir=tmp_path / "restored"
        )
        assert "Actor" in restored.instance.load_config().entity_types

    def test_auth_off_refuses_auth_managed_config_at_snapshot_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        instance = service_init(tmp_path / "source", config_yaml=AUTH_MANAGED_YAML).instance
        snapshot_id = service_create_snapshot(instance).snapshot.snapshot_id

        _auth_off(monkeypatch)
        target = tmp_path / "clone"
        with pytest.raises(ConfigError) as exc:
            service_clone_snapshot(instance, snapshot_id, target)
        message = str(exc.value)
        assert "Actor" in message
        assert "CRUXIBLE_SERVER_AUTH=true" in message
        # Refused before clone_from_snapshot wrote anything into the target root.
        assert not target.exists()

    def test_auth_on_accepts_auth_managed_config_at_snapshot_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        instance = service_init(tmp_path / "source", config_yaml=AUTH_MANAGED_YAML).instance
        snapshot_id = service_create_snapshot(instance).snapshot.snapshot_id

        cloned = service_clone_snapshot(instance, snapshot_id, tmp_path / "clone")
        assert "Actor" in cloned.instance.load_config().entity_types


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
      actor_id:
        type: string
        primary_key: true
      label:
        type: string
        optional: true

relationships:
  - name: cites
    from: Case
    to: Case
"""

AUTH_MANAGED_OVERLAY_YAML = """\
version: "1.0"
name: case-law-overlay
extends: .cruxible/upstream/current/config.yaml
entity_types:
  Actor:
    write_policy: mint_only
    auth_managed: true
    properties:
      actor_id:
        type: string
        primary_key: true
      label:
        type: string
        optional: true
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


class TestAuthManagedRefusalAtOverlayAndPull:
    def test_auth_off_refuses_auth_managed_base_at_overlay_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_on(monkeypatch)
        _, release_dir = _publish_release(
            tmp_path, config_yaml=CASE_BASE_WITH_AUTH_MANAGED_YAML
        )

        _auth_off(monkeypatch)
        overlay_root = tmp_path / "overlay"
        with pytest.raises(ConfigError) as exc:
            service_create_state_overlay(
                transport_ref=f"file://{release_dir}",
                root_dir=overlay_root,
            )
        message = str(exc.value)
        assert "Actor" in message
        assert "CRUXIBLE_SERVER_AUTH=true" in message
        # Refused before anything was materialized into the overlay root.
        assert not overlay_root.exists()

    def test_auth_off_refuses_auth_managed_upstream_at_pull_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Publish a plain release, create the overlay while it is clean, then
        # republish the base WITH an auth-managed type and try to pull it in.
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
        active_config_path = overlay_instance.get_config_path()
        active_before = active_config_path.read_text()

        with pytest.raises(ConfigError) as exc:
            service_pull_state_apply(
                overlay_instance,
                expected_apply_digest=preview.apply_digest,
            )
        message = str(exc.value)
        assert "Actor" in message
        assert "CRUXIBLE_SERVER_AUTH=true" in message
        # The refused pull left the active composed config byte-identical and the
        # overlay still tracking the previous release.
        assert active_config_path.read_text() == active_before
        status = service_state_status(overlay_instance)
        assert status.upstream is not None
        assert status.upstream.release_id == "v1.0.0"

    def test_auth_off_refuses_auth_managed_overlay_at_upstream_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auth_off(monkeypatch)
        _, release_dir = _publish_release(tmp_path, config_yaml=CASE_BASE_YAML)
        overlay_root = tmp_path / "overlay"
        overlay_instance = service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            root_dir=overlay_root,
        ).instance

        active_config_path = overlay_instance.get_config_path()
        active_before = active_config_path.read_text()
        overlay_config_path = overlay_root / "config.yaml"
        overlay_before = overlay_config_path.read_text()

        # Uploaded-YAML upstream reload: refused BEFORE the overlay file and the
        # composed active config are written.
        with pytest.raises(ConfigError) as exc:
            service_reload_config(overlay_instance, config_yaml=AUTH_MANAGED_OVERLAY_YAML)
        assert "Actor" in str(exc.value)
        assert overlay_config_path.read_text() == overlay_before
        assert active_config_path.read_text() == active_before

        # File-based upstream reload: the overlay file on disk declares an
        # auth-managed type, but the refused reload must leave the previously
        # composed active config byte-identical.
        overlay_config_path.write_text(AUTH_MANAGED_OVERLAY_YAML)
        with pytest.raises(ConfigError) as exc:
            service_reload_config(overlay_instance)
        assert "Actor" in str(exc.value)
        assert active_config_path.read_text() == active_before
