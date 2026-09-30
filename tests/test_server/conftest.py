"""Shared PB-E HTTP fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from tests.core_support._support import build_inputs, restamp_state_root
from tests.core_support._world_templates import TEMPLATES, copy_template


def _fresh_playbill_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    require_independent_approval: bool = False,
) -> Iterator[tuple[TestClient, str, Path]]:
    state = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    registered = get_registry().create_governed_instance_with_id("inst_playbill_http")
    instance_id = registered.record.instance_id
    managed = Path(registered.record.location)
    owner = generate_client_principal_key(
        tmp_path / "owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    reviewer = generate_client_principal_key(
        tmp_path / "reviewer-custody",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    with TestClient(create_app()) as client:
        initialized = client.post(
            f"/api/v1/{instance_id}/playbill/init",
            json={
                "require_independent_approval": require_independent_approval,
                "principals": [
                    owner.principal.model_dump(mode="json"),
                    reviewer.principal.model_dump(mode="json"),
                ],
            },
        )
        assert initialized.status_code == 200, initialized.text
        yield client, instance_id, reviewer.private_key_path
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


@dataclass(frozen=True)
class _HttpWorld:
    instance_id: str
    reviewer_key: Path


def _build_http_world(root: Path, require_independent_approval: bool) -> _HttpWorld:
    """Initialize one host under ``root`` exactly as the fixture does, then stop it."""

    with pytest.MonkeyPatch.context() as build:
        opened = _fresh_playbill_http(
            root, build, require_independent_approval=require_independent_approval
        )
        _client, instance_id, reviewer_key = next(opened)
        for _ in opened:
            pass
    return _HttpWorld(instance_id=instance_id, reviewer_key=reviewer_key.relative_to(root))


def _playbill_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    require_independent_approval: bool = False,
) -> Iterator[tuple[TestClient, str, Path]]:
    """An initialized host; a copy of this process's template host when one applies.

    The copy carries the registry, trust roots, credentials and signed genesis a
    fresh `playbill/init` writes (see `tests/core_support/_world_templates.py`),
    and the app below opens it the way a restarted daemon opens its state root.
    """

    template = TEMPLATES.template(
        ("playbill_http", require_independent_approval, build_inputs()),
        lambda root: _build_http_world(root, require_independent_approval),
    )
    copied = None if template is None else copy_template(template, tmp_path)
    if template is None or copied is None:
        yield from _fresh_playbill_http(
            tmp_path, monkeypatch, require_independent_approval=require_independent_approval
        )
        return
    TEMPLATES.copies += 1
    state = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    restamp_state_root(state)
    with TestClient(create_app()) as client:
        yield client, template.value.instance_id, tmp_path / template.value.reviewer_key
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


@pytest.fixture
def playbill_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, Path]]:
    """An initialized host. Provider packages are installed separately."""

    yield from _playbill_http(tmp_path, monkeypatch)
