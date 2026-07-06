"""Acceptance coverage for multi-kit compose-at-init.

The headline flow was previously impossible: on an AUTH-ENABLED daemon, an
overlay kit had no working init path at all (``init --kit`` refused overlays
and ``state create-overlay`` requires a published state). These tests prove:

* ``cruxible init --bootstrap --kit agent-operation --kit project-domain``
  succeeds against an auth-enabled in-process daemon (real CLI wiring over the
  real FastAPI routes, bootstrap bearer auth), and the resulting instance's
  composed config carries both layers' entity types, with base and overlay
  query surfaces resolving over authenticated requests.
* a service-level composed init of the shipped kev-reference + kev-triage pair
  materializes each bundle under ``kits/<kit_id>/``, compiles a base-kit
  canonical workflow against the installed instance lock, resolves an overlay
  named query, and resolves kit-scoped ``kit://`` provider refs from BOTH kits
  to their own ``kits/<kit_id>/`` roots.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.config.source_pointer import compose_config_source, load_config_source
from cruxible_core.errors import ConfigError
from cruxible_core.kits import KitBundle, KitManifest, resolve_kit_provider_ref
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service import service_describe_query, service_init, service_plan

# Base URL is a label only; the real transport is the in-process TestClient.
_SERVER_URL = "http://cruxible-daemon"
_BOOTSTRAP_SECRET = "bootstrap-secret"
_INSTANCE_ID_RE = re.compile(r"Instance ID:\s*(\S+)")


@pytest.fixture
def auth_cli_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[..., Result], TestClient]]:
    """A ``cruxible ...`` invoker bound to a fresh AUTH-ENABLED in-process daemon.

    Mirrors tests/test_cli/test_state_command_flow_smoke.py, with
    ``CRUXIBLE_SERVER_AUTH=true`` and an unclaimed runtime bootstrap secret. The
    TestClient starts with the bootstrap secret as its bearer, matching the
    documented first-boot flow (`cruxible init --kit ... --bootstrap`).
    """
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", _BOOTSTRAP_SECRET)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()

    test_client = TestClient(create_app())
    test_client.headers.update({"Authorization": f"Bearer {_BOOTSTRAP_SECRET}"})
    client = CruxibleClient(base_url=_SERVER_URL)
    client._client = test_client
    runner = CliRunner()

    def invoke(*args: str, instance_id: str | None = None) -> Result:
        base = ["--server-url", _SERVER_URL]
        if instance_id is not None:
            base += ["--instance-id", instance_id]
        return runner.invoke(
            cli,
            base + list(args),
            obj={"_client": client, "server_url": _SERVER_URL},
        )

    try:
        yield invoke, test_client
    finally:
        test_client.close()
        get_manager().clear()


def test_bootstrap_init_composes_overlay_kit_on_auth_enabled_daemon(
    auth_cli_runner: tuple[Callable[..., Result], TestClient],
) -> None:
    invoke, test_client = auth_cli_runner

    init = invoke(
        "init",
        "--kit",
        "agent-operation",
        "--kit",
        "project-domain",
        "--bootstrap",
    )
    assert init.exit_code == 0, init.output
    match = _INSTANCE_ID_RE.search(init.output)
    assert match is not None, init.output
    instance_id = match.group(1)
    assert "Source: kit agent-operation project-domain" in init.output

    # Both bundles are materialized under kits/<kit_id>/ and the instance
    # stores a source pointer whose composed config carries both layers'
    # entity types. No flattened config is written to disk.
    record = get_registry().get(instance_id)
    assert record is not None
    root = Path(record.location)
    assert (root / "kits" / "agent-operation" / "cruxible-kit.yaml").exists()
    assert (root / "kits" / "project-domain" / "cruxible-kit.yaml").exists()
    pointer = load_config_source(root / ".cruxible" / "config-source.yaml")
    assert [layer.ref for layer in pointer.layers] == ["agent-operation", "project-domain"]
    assert not (root / ".cruxible" / "configs" / "active.yaml").exists()
    composed = compose_config_source(pointer, instance_root=root).config
    assert "WorkItem" in composed.entity_types
    assert "ReviewRequest" in composed.entity_types
    assert "ProductArea" in composed.entity_types
    assert "RoadmapItem" in composed.entity_types

    # Claim the bootstrap for the composed instance and continue as ADMIN --
    # the documented first-boot path.
    claimed = test_client.post(
        f"/api/v1/{instance_id}/runtime/bootstrap/claim",
        json={"bootstrap_secret": _BOOTSTRAP_SECRET},
    )
    assert claimed.status_code == 200, claimed.text
    test_client.headers.update({"Authorization": f"Bearer {claimed.json()['token']}"})

    # A base-kit query and an overlay query both resolve over authenticated
    # CLI requests against the composed instance. The bootstrap claim
    # materialized the auth-managed Actor for the admin credential, so the
    # base traversal runs against a real entry entity.
    base_query = invoke(
        "query",
        "run",
        "actor_work_queue",
        "--param",
        "actor_id=bootstrap-admin",
        "--json",
        instance_id=instance_id,
    )
    assert base_query.exit_code == 0, base_query.output
    overlay_query = invoke(
        "query",
        "describe",
        "--query",
        "work_items_for_area",
        "--json",
        instance_id=instance_id,
    )
    assert overlay_query.exit_code == 0, overlay_query.output
    assert "work_items_for_area" in overlay_query.output


def test_bootstrap_init_rejects_overlay_without_base(
    auth_cli_runner: tuple[Callable[..., Result], TestClient],
) -> None:
    invoke, _test_client = auth_cli_runner

    result = invoke("init", "--kit", "project-domain", "--bootstrap")

    assert result.exit_code != 0
    assert "must be role: standalone" in result.output
    assert "project-domain" in result.output
    assert "agent-operation" in result.output


def _bundle(kit_id: str, role: str, target_state: str | None = None) -> KitBundle:
    return KitBundle(
        root=Path("/nonexistent") / kit_id,
        manifest=KitManifest(
            kit_id=kit_id,
            version="0.2.0",
            role=role,
            target_state=target_state,
        ),
        digest="sha256:test",
    )


def test_kit_sequence_validation_names_offender_and_target() -> None:
    from cruxible_core.config.source_pointer import validate_kit_layer_sequence

    base = _bundle("base", "standalone")
    overlay = _bundle("overlay", "overlay", target_state="base")
    stranger = _bundle("stranger", "overlay", target_state="elsewhere")

    validate_kit_layer_sequence(["base", "overlay"], [base, overlay])

    with pytest.raises(ConfigError, match="must be role: standalone"):
        validate_kit_layer_sequence(["overlay"], [overlay])
    with pytest.raises(
        ConfigError,
        match=r"'stranger' targets state 'elsewhere', which is not an earlier kit",
    ):
        validate_kit_layer_sequence(["base", "stranger"], [base, stranger])
    with pytest.raises(ConfigError, match="'overlay' appears more than once"):
        validate_kit_layer_sequence(
            ["base", "overlay", "overlay"],
            [base, overlay, overlay],
        )
    with pytest.raises(ConfigError, match="every kit after the first"):
        validate_kit_layer_sequence(["base", "base"], [base, base])


def test_service_composed_kev_init_resolves_workflows_queries_and_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "kit-cache"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    root = tmp_path / "kev-composed"

    result = service_init(root, kits=["kev-reference", "kev-triage"])
    instance = result.instance

    composed = instance.load_config()
    assert "Vulnerability" in composed.entity_types  # base layer
    assert "Asset" in composed.entity_types  # overlay layer

    # kit:// provider refs from BOTH kits are namespaced and resolve to their
    # own kits/<kit_id>/ roots.
    config_base_path = instance.get_config_path().parent
    base_ref = composed.providers["normalize_public_kev_reference"].ref
    overlay_ref = composed.providers["match_software_to_products"].ref
    assert base_ref == "kit://kev-reference/providers/reference.py::normalize_public_kev_reference"
    assert overlay_ref == "kit://kev-triage/providers/matching.py::match_software_to_products"
    base_path, _attr, base_root = resolve_kit_provider_ref(base_ref, config_base_path)
    overlay_path, _attr, overlay_root = resolve_kit_provider_ref(overlay_ref, config_base_path)
    assert base_root == (root / "kits" / "kev-reference").resolve()
    assert overlay_root == (root / "kits" / "kev-triage").resolve()
    assert base_path == base_root / "providers" / "reference.py"
    assert overlay_path == overlay_root / "providers" / "matching.py"

    # A base-kit canonical workflow compiles against the installed instance
    # lock, and an overlay named query resolves on the composed instance.
    plan = service_plan(instance, "build_public_kev_reference", {})
    assert plan.plan.workflow == "build_public_kev_reference"
    described = service_describe_query(instance, "vulnerability_asset_context")
    assert described.name == "vulnerability_asset_context"
