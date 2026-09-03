"""Shared PB-E HTTP fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from tests.support.provider_seed import write_workspace_seed_config


def _playbill_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: bool,
) -> Iterator[tuple[TestClient, str, Path]]:
    state = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    if seed:
        write_workspace_seed_config(state)
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
                "principals": [
                    owner.principal.model_dump(mode="json"),
                    reviewer.principal.model_dump(mode="json"),
                ],
                "seed": seed,
            },
        )
        assert initialized.status_code == 200, initialized.text
        yield client, instance_id, reviewer.private_key_path
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


@pytest.fixture
def playbill_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, Path]]:
    """A host initialized with the explicit seed opt-out.

    Nothing here is about the Provider seed, so the daemon needs no configured
    local materialization and no adapter checkout.
    """

    yield from _playbill_http(tmp_path, monkeypatch, seed=False)


@pytest.fixture
def seeded_playbill_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, Path]]:
    """A host seeded from the real adapter checkout, or a skip naming its card."""

    yield from _playbill_http(tmp_path, monkeypatch, seed=True)
