"""A daemon serves only instances under its own state root.

The incident this pins: a daemon started on a COPIED state root wrote the live
instance, because the registry it copied stored absolute instance locations.
Locations are now stored relative to the state root, absolute rows under it are
migrated in place, and a row whose location resolves outside the root is
refused by name rather than served.
"""

from __future__ import annotations

import gc
import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.errors import InstanceLocationRefusedError
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import (
    InstanceRegistry,
    get_registry,
    reset_registry,
)

_INSTANCE = "inst_isolated_root"


def _serve_root(monkeypatch: pytest.MonkeyPatch, state_root: Path) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_root))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    return TestClient(create_app())


def _initialized_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state_root = tmp_path / "original"
    client = _serve_root(monkeypatch, state_root)
    created = client.post("/api/v1/runtime/instances", json={"instance_id": _INSTANCE})
    assert created.status_code == 200, created.text
    owner = generate_client_principal_key(
        tmp_path / "owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(state_root,),
    )
    initialized = client.post(
        f"/api/v1/{_INSTANCE}/playbill/init",
        json={"principals": [owner.principal.model_dump(mode="json")]},
    )
    assert initialized.status_code == 200, initialized.text
    get_playbill_manager().clear()
    reset_registry()
    # Let the original's own handles finish closing (a WAL checkpoint on close
    # rewrites its index files) before anything is compared against it.
    gc.collect()
    return state_root.resolve()


def _stored_locations(state_root: Path) -> dict[str, str]:
    connection = sqlite3.connect(f"file:{state_root / 'daemon' / 'registry.db'}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT instance_id, location FROM instances").fetchall()
    finally:
        connection.close()
    return dict(rows)


def _store_absolute(state_root: Path, instance_id: str, location: Path) -> None:
    """Make the registry what it was before relative storage: an absolute row, no step."""

    connection = sqlite3.connect(state_root / "daemon" / "registry.db")
    try:
        with connection:
            connection.execute(
                "UPDATE instances SET location = ? WHERE instance_id = ?",
                (str(location), instance_id),
            )
            connection.execute("DROP TABLE registry_migrations")
    finally:
        connection.close()


def _migration_steps(state_root: Path) -> list[str]:
    connection = sqlite3.connect(f"file:{state_root / 'daemon' / 'registry.db'}?mode=ro", uri=True)
    try:
        return [row[0] for row in connection.execute("SELECT step FROM registry_migrations")]
    finally:
        connection.close()


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_locations_are_stored_relative_to_the_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_root = _initialized_root(monkeypatch, tmp_path)

    assert _stored_locations(state_root) == {_INSTANCE: f"instances/{_INSTANCE}"}
    record = InstanceRegistry(state_root / "daemon" / "registry.db").get(_INSTANCE)
    assert record is not None
    assert Path(record.location) == state_root / "instances" / _INSTANCE
    assert record.within_state_root


def test_absolute_rows_under_the_root_migrate_to_relative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_root = _initialized_root(monkeypatch, tmp_path)
    _store_absolute(state_root, _INSTANCE, state_root / "instances" / _INSTANCE)

    registry = InstanceRegistry(state_root / "daemon" / "registry.db")

    assert _stored_locations(state_root) == {_INSTANCE: f"instances/{_INSTANCE}"}
    # The step is recorded once, by its own id, and an open runs it no more.
    assert _migration_steps(state_root) == ["2026-10-01-relative-locations"]
    InstanceRegistry(state_root / "daemon" / "registry.db")
    assert _migration_steps(state_root) == ["2026-10-01-relative-locations"]
    record = registry.get(_INSTANCE)
    assert record is not None
    assert registry.instance_root(record) == state_root / "instances" / _INSTANCE


def test_containment_is_case_insensitive_and_maps_into_this_root(tmp_path: Path) -> None:
    state_root = (tmp_path / "Root").resolve()
    registry = InstanceRegistry(state_root / "daemon" / "registry.db")
    spelled = Path(str(state_root).upper()) / "instances" / _INSTANCE

    relative = registry.relative_location(spelled)

    assert relative is not None and relative.as_posix() == f"instances/{_INSTANCE}"
    assert registry.relative_location(tmp_path / "Rooted" / "instances" / _INSTANCE) is None
    assert registry.relative_location(state_root) is None


def test_a_relative_row_escaping_the_root_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_root = _initialized_root(monkeypatch, tmp_path)
    connection = sqlite3.connect(state_root / "daemon" / "registry.db")
    with connection:
        connection.execute(
            "UPDATE instances SET location = ? WHERE instance_id = ?",
            (f"../elsewhere/{_INSTANCE}", _INSTANCE),
        )
    connection.close()

    _serve_root(monkeypatch, state_root)
    with pytest.raises(InstanceLocationRefusedError, match="outside this daemon's state root"):
        get_playbill_manager().get(_INSTANCE)


def test_a_copied_pre_migration_root_never_writes_the_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = _initialized_root(monkeypatch, tmp_path)
    # The registry as it was before relative storage: an absolute location.
    _store_absolute(original, _INSTANCE, original / "instances" / _INSTANCE)
    copy = tmp_path / "copy"
    shutil.copytree(original, copy, symlinks=True)
    before = _snapshot(original)

    client = _serve_root(monkeypatch, copy)
    with pytest.raises(InstanceLocationRefusedError) as refused:
        get_playbill_manager().get(_INSTANCE)
    assert refused.value.error_code == "playbill.host.location_outside_state_root"
    assert str(original / "instances" / _INSTANCE) in str(refused.value)
    stored = client.post(
        f"/api/v1/{_INSTANCE}/playbill/bodies",
        json={"content_base64": "aGVsbG8="},
    )
    assert stored.status_code == 409, stored.text
    assert stored.json()["error_code"] == "playbill.host.location_outside_state_root"
    status = client.get("/api/v1/server/info")
    assert status.status_code == 200, status.text
    (host,) = status.json()["hosts"]
    assert host["compatibility"] == "refused"
    assert host["reason"]["code"] == "location_outside_state_root"
    # The copy's registry keeps the foreign row as it was: it is not rewritten
    # to point anywhere, and the original is byte-for-byte untouched.
    assert _stored_locations(copy.resolve()) == {_INSTANCE: str(original / "instances" / _INSTANCE)}
    assert _snapshot(original) == before


def test_a_copied_migrated_root_serves_and_writes_only_its_own_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = _initialized_root(monkeypatch, tmp_path)
    copy = tmp_path / "copy"
    shutil.copytree(original, copy, symlinks=True)
    before = _snapshot(original)

    client = _serve_root(monkeypatch, copy)
    instance = get_playbill_manager().get(_INSTANCE)
    assert instance.root.resolve() == (copy / "instances" / _INSTANCE).resolve()
    stored = client.post(
        f"/api/v1/{_INSTANCE}/playbill/bodies",
        json={"content_base64": "aGVsbG8="},
    )
    assert stored.status_code == 200, stored.text

    assert _snapshot(original) == before
    assert _snapshot(copy) != _snapshot(original)


def test_the_live_root_keeps_serving_after_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_root = _initialized_root(monkeypatch, tmp_path)
    _store_absolute(state_root, _INSTANCE, state_root / "instances" / _INSTANCE)

    _serve_root(monkeypatch, state_root)
    instance = get_playbill_manager().get(_INSTANCE)

    assert instance.root.resolve() == state_root / "instances" / _INSTANCE
    assert get_registry().get(_INSTANCE) is not None
    assert _stored_locations(state_root) == {_INSTANCE: f"instances/{_INSTANCE}"}
