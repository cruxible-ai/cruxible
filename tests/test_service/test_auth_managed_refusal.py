"""Auth-off daemons must LOUDLY refuse configs declaring auth-managed types.

Auth-managed entity types materialize only from runtime-credential mints, which
require ``CRUXIBLE_SERVER_AUTH=true``. On an auth-off daemon no mint can ever
happen, so such a type is permanently empty and unwritable. The daemon must refuse
the config at init/reload rather than fail silently with empty queries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.loader import load_config
from cruxible_core.errors import ConfigError
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
        # Refused before materializing the instance: no instance.json marker is
        # left behind, so a corrected retry is not blocked by "already exists".
        assert not (
            tmp_path / "inst" / CruxibleInstance.INSTANCE_DIR / "instance.json"
        ).exists()

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
        # The message names the instance's own config copy so an agent can act.
        assert str(tmp_path / "inst") in message

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
