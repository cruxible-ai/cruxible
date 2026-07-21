"""CLI-surface smoke for the review-promotion ("promote") flow end-to-end.

In Cruxible the "promote" command flow is the governed review-promotion cycle:
``propose -> group list/get -> group resolve(approve) -> group trust -> query``.
``group resolve --action approve`` is what promotes a pending-review candidate
group into live, accepted edges; ``group trust`` then grades the resulting
resolution.

This smoke drives that flow through the *actual CLI command surface* against a
fresh, ephemeral in-process daemon. Every step is a real ``cruxible ...``
invocation (``click`` ``CliRunner`` over ``cli``) whose transport is a real
``CruxibleClient`` bound to a FastAPI ``TestClient`` over ``create_app()`` — the
same client the CLI uses in server mode, with no socket and no network. State
lives under a per-test temp dir.

A focused CLI smoke for exactly this flow was lost in a disk-full incident
(``tests/test_cli/test_state_command_flow_smoke.py`` referenced in
``docs/dev/overnight-batch-2026-06-21.md``); this reconstructs it.

What this adds over neighboring coverage:

* ``tests/test_demos/test_kev_quickstart_smoke.py`` exercises propose -> group
  list/get -> resolve, but through the *client/HTTP* surface and stops at
  approve — it never drives the CLI verbs and never reaches trust or the
  now-live query.
* ``tests/test_cli/test_commands.py::TestGroupResolveCLI`` /
  ``TestGroupTrustCLI`` unit-test ``group resolve`` / ``group trust``, but only
  assert that *local* mutation is refused — they never run the real
  promote->trust->query flow through a daemon.

This smoke is the CLI-surface end-to-end glue: it ties ``propose`` ->
``group list`` -> ``group get`` -> ``group resolve --action approve`` ->
``group trust`` -> ``query run`` into one asserted flow over real CLI commands.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry

# Base URL is a label only; the real transport is the in-process TestClient.
_SERVER_URL = "http://cruxible-daemon"
_INSTANCE_ID_RE = re.compile(r"Instance ID:\s*(\S+)")


@pytest.fixture
def cli_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., Result]]:
    """A ``cruxible ...`` invoker bound to a fresh ephemeral in-process daemon.

    Mirrors ``tests/test_demos/test_kev_quickstart_smoke.py``: a real
    ``CruxibleClient`` whose HTTP transport is swapped for a FastAPI
    ``TestClient`` over a freshly created ``create_app()``. The client is seeded
    onto the CLI root context (``obj["_client"]``) and ``--server-url`` is
    passed so the CLI resolves server mode and reuses that client — every
    invocation therefore exercises the actual CLI command wiring and the real
    FastAPI routes without binding a socket. Default permission mode is ADMIN
    (``CRUXIBLE_MODE`` unset), matching the bootstrap/canonical-apply surface.
    """
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
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
    client = CruxibleClient(base_url=_SERVER_URL)
    # The real CLI client speaks to the daemon over its own httpx.Client; swap in
    # the in-process TestClient so every CLI command hits the real FastAPI routes.
    client._client = test_client
    runner = CliRunner()

    def invoke(*args: str, instance_id: str | None = None) -> Result:
        base = ["--server-url", _SERVER_URL]
        if instance_id is not None:
            base += ["--instance-id", instance_id]
        return runner.invoke(
            cli,
            base + list(args),
            # Seed the resolved client + transport onto the CLI root context so
            # _get_client() returns our in-process-bound client instead of
            # constructing a real network client.
            obj={"_client": client, "server_url": _SERVER_URL},
        )

    try:
        yield invoke
    finally:
        test_client.close()
        get_manager().clear()


def _ok(result: Result, label: str) -> Result:
    assert result.exit_code == 0, f"{label} failed (exit {result.exit_code}):\n{result.output}"
    return result


def _instance_id_from_init(result: Result) -> str:
    match = _INSTANCE_ID_RE.search(result.output)
    assert match is not None, f"init did not print an instance id:\n{result.output}"
    return match.group(1)


def _run_canonical_workflow(
    invoke: Callable[..., Result],
    instance_id: str,
    workflow: str,
) -> None:
    """Drive ``cruxible run --workflow ... --json`` then ``apply`` over the CLI.

    A canonical ``run`` produces an ``apply_digest`` (preview only); ``apply``
    commits it under the optimistic-concurrency guards the daemon enforces.
    """
    run = _ok(
        invoke("run", "--workflow", workflow, "--json", instance_id=instance_id),
        f"run {workflow}",
    )
    preview = json.loads(run.stdout)
    assert preview["canonical"] is True
    assert preview["apply_digest"], f"{workflow} run produced no apply_digest"

    apply = _ok(
        invoke(
            "apply",
            "--workflow",
            workflow,
            "--apply-digest",
            preview["apply_digest"],
            "--head-snapshot",
            preview["head_snapshot_id"],
            "--json",
            instance_id=instance_id,
        ),
        f"apply {workflow}",
    )
    applied = json.loads(apply.stdout)
    assert applied["committed_snapshot_id"], f"{workflow} apply committed no snapshot"


def test_promote_command_flow_end_to_end_via_cli(
    cli_runner: Callable[..., Result],
    tmp_path: Path,
) -> None:
    invoke = cli_runner

    # ── Set up: KEV reference world (init + build) so there is data to map ──
    ref_init = _ok(
        invoke("init", "--root-dir", str(tmp_path / "kev-reference"), "--kit", "kev-reference"),
        "init kev-reference",
    )
    reference_id = _instance_id_from_init(ref_init)
    _ok(invoke("lock", instance_id=reference_id), "lock reference")
    _run_canonical_workflow(invoke, reference_id, "build_public_kev_reference")

    # Publish the reference to a file:// transport and overlay the triage kit on
    # top, then build local canonical state — the documented onboarding path that
    # produces the software inventory the proposal maps over.
    release_dir = tmp_path / "releases" / "current"
    _ok(
        invoke(
            "state",
            "publish",
            "--transport-ref",
            f"file://{release_dir}",
            "--state-id",
            "kev-reference",
            "--release-id",
            "smoke-1",
            "--compatibility",
            "data_only",
            instance_id=reference_id,
        ),
        "state publish",
    )
    overlay_init = _ok(
        invoke(
            "state",
            "create-overlay",
            "--root-dir",
            str(tmp_path / "kev-triage"),
            "--transport-ref",
            f"file://{release_dir}",
            "--kit",
            "kev-triage",
        ),
        "state create-overlay",
    )
    overlay_id = _instance_id_from_init(overlay_init)
    _ok(invoke("lock", instance_id=overlay_id), "lock overlay")
    _run_canonical_workflow(invoke, overlay_id, "build_local_state")

    # ── Step 1: `propose` a relationship workflow → pending_review group ──
    proposed = _ok(
        invoke(
            "propose",
            "--workflow",
            "propose_asset_products",
            "--json",
            instance_id=overlay_id,
        ),
        "propose propose_asset_products",
    )
    propose_payload = json.loads(proposed.stdout)
    group_id = propose_payload["group_id"]
    assert group_id, f"propose produced no candidate group:\n{proposed.output}"
    assert propose_payload["group_status"] == "pending_review"
    relationship_type = "asset_runs_product"

    # ── Step 2: `group list --status pending_review` → group appears ──
    listed = _ok(
        invoke("group", "list", "--status", "pending_review", "--json", instance_id=overlay_id),
        "group list",
    )
    list_payload = json.loads(listed.output)
    assert any(item["group_id"] == group_id for item in list_payload["items"]), (
        f"proposed group {group_id} not listed as pending_review:\n{listed.output}"
    )

    # ── Step 3: `group get --group <id>` → members + signals + thesis present ──
    got = _ok(
        invoke("group", "get", "--group", group_id, "--json", instance_id=overlay_id),
        "group get",
    )
    get_payload = json.loads(got.output)
    assert get_payload["group"]["group_id"] == group_id
    assert get_payload["group"]["relationship_type"] == relationship_type
    members = get_payload["members"]
    assert members, "pending group has no members to review"
    assert members[0]["signals"], "pending group member carries no signals"
    assert get_payload["group"]["thesis_text"], "pending group has no thesis text"
    member0 = members[0]
    asset_id = member0["from_id"]
    product_id = member0["to_id"]

    # The bucket is pending (no accepted tuples yet); read the live pending
    # version so resolve is guarded against the version the reviewer saw.
    status_before = _ok(
        invoke("group", "status", "--group", group_id, "--json", instance_id=overlay_id),
        "group status (pre-resolve)",
    )
    status_before_payload = json.loads(status_before.output)
    pending_version = status_before_payload["pending_version"]
    assert pending_version is not None
    assert status_before_payload["accepted_tuple_count"] == 0
    assert status_before_payload["pending_delta_count"] > 0

    # ── Step 4: `group resolve --action approve` → edges promoted pending→live ──
    resolved = _ok(
        invoke(
            "group",
            "resolve",
            "--group",
            group_id,
            "--action",
            "approve",
            "--expected-pending-version",
            str(pending_version),
            "--rationale",
            "Smoke: reviewed proposed asset-product mappings.",
            "--json",
            instance_id=overlay_id,
        ),
        "group resolve approve",
    )
    resolve_payload = json.loads(resolved.stdout)
    assert resolve_payload["action"] == "approve"
    assert resolve_payload["edges_created"] > 0, "approving the group created no governed edges"
    resolution_id = resolve_payload["resolution_id"]
    assert resolution_id, "resolve recorded no resolution_id"
    assert resolve_payload["receipt_id"], "resolve recorded no receipt_id"

    # The pending bucket is now promoted to accepted (live) tuples.
    status_after = _ok(
        invoke("group", "status", "--group", group_id, "--json", instance_id=overlay_id),
        "group status (post-resolve)",
    )
    status_after_payload = json.loads(status_after.output)
    assert status_after_payload["accepted_tuple_count"] == resolve_payload["edges_created"]
    assert status_after_payload["pending_delta_count"] == 0
    assert status_after_payload["pending_group_id"] is None

    # ── Step 5: `group trust --resolution <id> --status trusted` → trust grades ──
    trusted = _ok(
        invoke(
            "group",
            "trust",
            "--resolution",
            resolution_id,
            "--status",
            "trusted",
            "--reason",
            "Smoke: high-confidence vendor/product match accepted.",
            instance_id=overlay_id,
        ),
        "group trust",
    )
    assert "trust status set to 'trusted'" in trusted.output

    status_trusted = _ok(
        invoke("group", "status", "--group", group_id, "--json", instance_id=overlay_id),
        "group status (post-trust)",
    )
    assert json.loads(status_trusted.output)["latest_trust_status"] == "trusted"

    # The promoted resolution is also discoverable via `group resolutions`.
    resolutions = _ok(
        invoke(
            "group",
            "resolutions",
            "--relationship",
            relationship_type,
            "--json",
            instance_id=overlay_id,
        ),
        "group resolutions",
    )
    resolutions_payload = json.loads(resolutions.output)
    assert any(item["resolution_id"] == resolution_id for item in resolutions_payload["items"]), (
        f"approved resolution {resolution_id} missing from resolutions list:\n{resolutions.output}"
    )

    # ── Step 6: query the now-live relationships → live/accepted, not pending ──
    # `relationship lineage` proves the specific edge is live and governed: it was
    # created by this group's approved resolution, so its review status is
    # `approved` and its provenance points back at the group.
    lineage = _ok(
        invoke(
            "relationship",
            "lineage",
            "--from-type",
            "Asset",
            "--from-id",
            asset_id,
            "--relationship",
            relationship_type,
            "--to-type",
            "Product",
            "--to-id",
            product_id,
            "--json",
            instance_id=overlay_id,
        ),
        "relationship lineage",
    )
    lineage_payload = json.loads(lineage.output)
    assert lineage_payload["found"] is True, "promoted edge not found as a live relationship"
    assert lineage_payload["group"]["group_id"] == group_id
    assertion = lineage_payload["relationship"]["metadata"]["assertion"]
    assert assertion["review"]["status"] == "approved"
    assert assertion["review"]["source"] == "group"

    # And a named query that traverses the governed relationship returns the
    # now-live edge: starting from the mapped product, the approved asset is
    # reachable through `asset_runs_product`.
    queried = _ok(
        invoke(
            "query",
            "run",
            "product_asset_context",
            "--param",
            f"product_id={product_id}",
            "--json",
            instance_id=overlay_id,
        ),
        "query product_asset_context",
    )
    query_payload = json.loads(queried.output)
    assert query_payload["receipt_id"], "query returned no receipt to inspect"
    assert query_payload["items"], "query for the now-live relationship returned nothing"
