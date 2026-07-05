"""Daemon-backed smoke test for the documented KEV quickstart.

Runs the steps from ``docs/quickstart.md`` end-to-end against a *fresh
ephemeral* in-process daemon (``create_app()`` driven through the real
``CruxibleClient`` over a FastAPI ``TestClient``), the same client the CLI
uses. This proves the advertised KEV onboarding flow actually functions
through the daemon HTTP surface, not just the in-process service layer.

The flow mirrors the quickstart exactly:

1. ``init --kit kev-reference``                     (create the reference world)
2. ``lock`` + ``run --workflow build_public_kev_reference`` (preview) + ``apply``
3. ``query run vulnerability_products --param cve_id=CVE-2020-1472``
4. ``state publish`` to a ``file://`` transport     (source-checkout overlay path)
5. ``state create-overlay --transport-ref ... --kit kev-triage``
6. ``lock`` + ``run --workflow build_local_state`` (preview) + ``apply``
7. ``propose --workflow propose_asset_products`` + ``group list/get`` + ``group resolve``

No live daemon, no sockets, no network: the reference and overlay kits both
build from bundled, digest-pinned local artifacts.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry

# Zerologon — present in the pinned public KEV reference data bundle.
QUICKSTART_CVE = "CVE-2020-1472"


@pytest.fixture
def daemon_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CruxibleClient]:
    """A real CruxibleClient bound to a fresh ephemeral in-process daemon.

    Reproduces the daemon-backed transport the CLI uses (``--server-url``)
    without binding a socket: the client's sync HTTP transport is a FastAPI
    ``TestClient`` over a freshly created ``create_app()``. State lives under a
    per-test temp dir; default permission mode is ADMIN (``CRUXIBLE_MODE``
    unset), matching the quickstart's admin surface for bootstrap and
    canonical apply.
    """
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()

    test_client = TestClient(create_app())
    client = CruxibleClient(base_url="http://cruxible-daemon")
    # The real CLI client speaks to the daemon over its own httpx.Client;
    # swap in the in-process TestClient (a sync httpx.Client) so every request
    # exercises the actual FastAPI routes without binding a real socket.
    client._client = test_client
    try:
        yield client
    finally:
        test_client.close()
        get_manager().clear()


def _run_canonical_workflow(
    client: CruxibleClient,
    instance_id: str,
    workflow_name: str,
) -> None:
    """Preview a canonical workflow, then apply it after checking the digest.

    Mirrors the quickstart ``run --save-preview`` then ``apply --preview-file``
    sequence: a canonical ``run`` produces an ``apply_digest`` (preview only),
    and ``apply`` commits it with optimistic-concurrency guards.
    """
    preview = client.workflow_run(instance_id, workflow_name=workflow_name)
    assert preview.canonical is True
    assert preview.apply_digest, f"{workflow_name} run did not produce an apply_digest"
    assert preview.receipt_id

    applied = client.workflow_apply(
        instance_id,
        workflow_name=workflow_name,
        expected_apply_digest=preview.apply_digest,
        expected_head_snapshot_id=preview.head_snapshot_id,
    )
    assert applied.canonical is True
    assert applied.committed_snapshot_id, f"{workflow_name} apply did not commit a snapshot"
    assert applied.receipt_id


def test_kev_quickstart_end_to_end_against_daemon(
    daemon_client: CruxibleClient,
    tmp_path: Path,
) -> None:
    client = daemon_client

    # ── Step 1: Create the KEV reference world (init --kit kev-reference) ──
    reference_root = tmp_path / "kev-reference-workspace"
    init = client.init(str(reference_root), kits=["kev-reference"])
    reference_id = init.instance_id
    assert reference_id and reference_id != str(reference_root)

    # ── Step 2: lock + build_public_kev_reference (preview) + apply ──
    lock = client.workflow_lock(reference_id)
    assert lock.config_digest
    _run_canonical_workflow(client, reference_id, "build_public_kev_reference")

    # ── Step 3: query vulnerability_products for the quickstart CVE ──
    query = client.query(
        reference_id,
        "vulnerability_products",
        params={"cve_id": QUICKSTART_CVE},
    )
    assert query.items, f"{QUICKSTART_CVE} returned no affected products"
    # Every query returns a receipt the quickstart tells users to inspect.
    assert query.receipt_id

    # ── Step 4: publish the reference to a file:// transport ──
    # (the source-checkout path the quickstart documents in place of a
    # published OCI reference state).
    release_dir = tmp_path / "releases" / "current"
    publish = client.state_publish(
        reference_id,
        transport_ref=f"file://{release_dir}",
        state_id="kev-reference",
        release_id="smoke-1",
        compatibility="data_only",
    )
    assert publish.manifest.release_id == "smoke-1"

    # ── Step 5: create the kev-triage overlay from the published transport ──
    overlay_root = tmp_path / "kev-triage-workspace"
    overlay = client.create_state_overlay(
        root_dir=str(overlay_root),
        transport_ref=f"file://{release_dir}",
        kit="kev-triage",
    )
    overlay_id = overlay.instance_id
    assert overlay_id and overlay_id != reference_id

    status = client.state_status(overlay_id)
    assert status.upstream is not None
    assert status.upstream.state_id == "kev-reference"
    assert status.upstream.release_id == "smoke-1"

    # ── Step 6: lock + build_local_state (preview) + apply on the overlay ──
    client.workflow_lock(overlay_id)
    _run_canonical_workflow(client, overlay_id, "build_local_state")

    # ── Step 7: propose_asset_products, inspect pending group, then approve ──
    proposed = client.propose_workflow(overlay_id, workflow_name="propose_asset_products")
    assert proposed.group_id, "propose_asset_products produced no pending group"
    group_id = proposed.group_id

    pending = client.list_groups(overlay_id, status="pending_review")
    assert any(item.get("group_id") == group_id for item in pending.items), (
        "proposed group is not listed as pending_review"
    )

    group = client.get_group(overlay_id, group_id)
    assert group.members, "pending group has no members to review"

    # The quickstart approves only with an explicit expected-pending-version
    # guard; read the live version rather than assuming it.
    bucket = client.get_group_status(overlay_id, group_id=group_id)
    assert bucket.pending_group_id == group_id
    assert bucket.pending_version is not None

    resolved = client.resolve_group(
        overlay_id,
        group_id,
        action="approve",
        expected_pending_version=bucket.pending_version,
        rationale="Smoke: reviewed proposed asset-product mappings.",
    )
    assert resolved.action == "approve"
    assert resolved.edges_created > 0, "approving the group created no governed edges"
    assert resolved.receipt_id
