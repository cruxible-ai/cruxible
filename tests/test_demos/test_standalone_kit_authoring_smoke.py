"""End-to-end smoke for Walkthrough 1: authoring a standalone kit from scratch.

``docs/kit-walkthroughs.md`` documents two authoring paths. Path 2 (customize an
overlay kit) is exercised by ``tests/test_demos/test_kev_quickstart_smoke.py`` and
``tests/test_cli/test_state_command_flow_smoke.py``. Path 1 — authoring a brand new
*standalone* kit from scratch — had no test. This is that test.

It authors a minimal standalone kit in a temp directory using **built-in workflow
steps only — no custom Python provider**:

* a ``cruxible-kit.yaml`` manifest (``role: standalone``),
* a ``config.yaml`` with two entity types, one relationship, and one named query,
* a tiny CSV under ``data/`` (the source of truth for the local state model),
* a ``build_local_state`` canonical workflow that loads the CSV with the *built-in*
  ``cruxible_core.providers.common.tabular.load_tabular_artifact_bundle`` provider,
  shapes the rows, then materializes entities + relationships via
  ``make_entities`` / ``make_relationships`` / ``apply_all`` — all built-in step
  kinds, zero kit-authored Python,
* a ``README.md`` with the ``CRUXIBLE:BEGIN/END`` marker blocks ``config views``
  refreshes.

Then it drives the documented command sequence (``docs/kit-walkthroughs.md`` lines
~116-134) end-to-end through the *real CLI command surface* against a fresh,
ephemeral in-process daemon (``click`` ``CliRunner`` over ``cli`` whose transport is
a real ``CruxibleClient`` bound to a FastAPI ``TestClient`` over ``create_app()`` —
the same client the CLI uses in server mode, no socket, no network). The harness is
borrowed wholesale from ``test_kev_quickstart_smoke`` / ``test_state_command_flow_smoke``.

Each documented step is asserted:

1. ``validate --config <kit>/config.yaml``        -> config reported valid
2. ``init --kit file://<kit>``                     -> instance created
3. ``lock``                                        -> lock file written with hashes
4. ``run --workflow build_local_state --save-preview preview.json`` -> apply_digest
5. ``apply --preview-file preview.json``           -> entities + relationships committed
6. ``query run asset_owner --param asset_id=...``  -> expected rows + a receipt_id
7. ``explain --receipt <id> --format markdown``    -> markdown render with trace detail
8. ``config views --config <kit>/config.yaml --runtime --update-readme <kit>/README.md``
                                                   -> README CRUXIBLE blocks refreshed

Documentation drift this smoke surfaces (the test works around each; these are
findings about ``docs/kit-walkthroughs.md``, not the kit):

* The walkthrough's example config (lines ~53-54) opens with
  ``schema_version: cruxible.config.v1`` and ``name``/``version`` keys. The real
  ``CoreConfig`` schema is ``extra: forbid`` and rejects ``schema_version`` — a
  config copied verbatim fails ``validate``. Real configs use only ``version`` /
  ``name``.
* The walkthrough command order is ``validate -> init -> lock -> run`` (lines
  ~116-118), but ``init --kit file://<dir>`` refuses a kit bundle with no
  ``cruxible.lock.yaml`` ("run ``cruxible lock`` before publishing"). A brand-new
  standalone kit must generate a lock *before* ``init``; the ``init``-then-``lock``
  order cannot be followed literally for from-scratch authoring.
* ``config views --update-readme`` (line ~133) *replaces existing* CRUXIBLE marker
  blocks; it does not create them. The README must already contain a
  BEGIN/END block per rendered view or it raises ``MissingReadmeMarkersError``.
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
from cruxible_core.config.loader import load_config
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from cruxible_core.workflow.compiler import (
    LOCK_FILE_NAME,
    build_lock,
    compute_path_sha256,
    write_lock,
)

# Base URL is a label only; the real transport is the in-process TestClient.
_SERVER_URL = "http://cruxible-daemon"
_INSTANCE_ID_RE = re.compile(r"Instance ID:\s*(\S+)")

# The standalone kit's tiny source CSV: two assets, two owners, one asset-owner
# edge per row. Authored once here so the smoke owns its own fixture data.
_ASSETS_CSV = (
    "asset_id,hostname,owner_id,owner_name\n"
    "ASSET-1,web-01.example.com,OWNER-1,Platform Team\n"
    "ASSET-2,db-01.example.com,OWNER-2,Data Team\n"
)

# The canonical-view marker keys `config views --view all` refreshes (kept in
# sync with DEFAULT_VIEW_ORDER). The README must already contain each BEGIN/END
# block — `--update-readme` *replaces* existing marker blocks, it does not
# create them (see drift note in the report).
_README_VIEW_KEYS = (
    "ontology",
    "schema-catalog",
    "workflow-pipeline",
    "workflow-summary",
    "provider-contracts",
    "governance-table",
    "mutation-guards",
    "signal-policy-catalog",
    "query-catalog",
    "quality-rules",
    "learning-loops",
)


def _kit_manifest() -> str:
    return (
        "schema_version: cruxible.kit.v1\n"
        "kit_id: my-risk-kit\n"
        "version: 0.2.0\n"
        "role: standalone\n"
        "entry_config: config.yaml\n"
        "provider_paths: []\n"
        "copy_paths:\n"
        "  - data\n"
        "  - README.md\n"
        "requires_extras: []\n"
    )


def _kit_config(*, artifact_digest: str) -> str:
    """The standalone config: 2 entity types, 1 relationship, 1 query, 1 workflow.

    The ``build_local_state`` workflow uses only built-in step kinds: a ``provider``
    step bound to the *built-in* tabular loader, ``shape_items`` to project the
    parsed rows, ``make_entities`` / ``make_relationships`` to build canonical
    objects, and ``apply_all`` to materialize them. No kit-authored provider Python.
    """
    return f"""version: "0.2.0"
name: my_risk_kit
description: >
  Minimal standalone risk kit authored from scratch for the kit-authoring
  walkthrough smoke. Tracks assets and their owners.

entity_types:
  Asset:
    description: A tracked asset that must have an accountable owner.
    properties:
      asset_id:
        type: string
        primary_key: true
      hostname:
        type: string
  Owner:
    description: The team or person accountable for an asset.
    properties:
      owner_id:
        type: string
        primary_key: true
      name:
        type: string

relationships:
  - name: asset_owned_by
    from: Asset
    to: Owner
    cardinality: many_to_one

named_queries:
  asset_owner:
    mode: traversal
    description: Starting from an asset, return its accountable owner.
    entry_point: Asset
    returns: Owner
    result_shape: path
    traversal:
      - as: owner
        relationship: asset_owned_by
        direction: outgoing

artifacts:
  asset_seed:
    kind: directory
    uri: ./data
    digest: {artifact_digest}

providers:
  parse_asset_seed:
    description: >
      Parse the pinned local asset CSV into generic provenance-rich tabular
      rows using the built-in tabular loader (no kit-authored provider).
    contract_in: cruxible.JsonObject
    contract_out: cruxible.ParsedTabularBundle
    ref: cruxible_core.providers.common.tabular.load_tabular_artifact_bundle
    version: "1.0.0"
    deterministic: true
    artifact: asset_seed

workflows:
  build_local_state:
    type: canonical
    description: >
      Build canonical asset/owner state from the bundled CSV using only
      built-in workflow steps.
    steps:
      - id: raw_tables
        provider: parse_asset_seed
        input:
          expected_tables:
            - assets
        as: raw_tables
      - id: rows
        shape_items:
          items: $steps.raw_tables.tables.assets.rows
          include_input: true
        as: rows
      - id: require_rows
        assert_count:
          step: rows
          count: items
          op: gt
          value: 0
          message: Asset seed produced no rows.
      - id: assets
        make_entities:
          entity_type: Asset
          items: $steps.rows.items
          entity_id: $item.asset_id
          properties:
            asset_id: $item.asset_id
            hostname: $item.hostname
        as: assets
      - id: owners
        make_entities:
          entity_type: Owner
          items: $steps.rows.items
          entity_id: $item.owner_id
          properties:
            owner_id: $item.owner_id
            name: $item.owner_name
        as: owners
      - id: asset_owner_edges
        make_relationships:
          relationship_type: asset_owned_by
          items: $steps.rows.items
          from_type: Asset
          from_id: $item.asset_id
          to_type: Owner
          to_id: $item.owner_id
        as: asset_owner_edges
      - id: apply_local_state
        apply_all:
          entities_from:
            - assets
            - owners
          relationships_from:
            - asset_owner_edges
        as: apply_local_state
    returns: apply_local_state
"""


def _kit_readme() -> str:
    """A README pre-seeded with the marker blocks `config views` refreshes."""
    lines = [
        "# my-risk-kit",
        "",
        "Authored prose lives outside the CRUXIBLE marker blocks.",
        "",
    ]
    for key in _README_VIEW_KEYS:
        lines.append(f"<!-- CRUXIBLE:BEGIN {key} -->")
        lines.append(f"<!-- CRUXIBLE:END {key} -->")
        lines.append("")
    return "\n".join(lines)


def _author_standalone_kit(kit_dir: Path) -> None:
    """Materialize the standalone kit source tree (Walkthrough 1, steps 1-3)."""
    data_dir = kit_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "assets.csv").write_text(_ASSETS_CSV)

    # The artifact digest is content-pinned: `lock` recomputes the directory hash
    # and refuses to lock if the declared digest disagrees. Compute it the same
    # way the compiler does so the authored config locks cleanly.
    artifact_digest = compute_path_sha256(data_dir)

    (kit_dir / "cruxible-kit.yaml").write_text(_kit_manifest())
    config_path = kit_dir / "config.yaml"
    config_path.write_text(_kit_config(artifact_digest=artifact_digest))
    (kit_dir / "README.md").write_text(_kit_readme())

    # Bootstrap the bundled lock. `init --kit file://<dir>` refuses to materialize
    # a kit bundle that has no `cruxible.lock.yaml` ("run `cruxible lock` before
    # publishing"), so a from-scratch standalone kit must ship a lock *before* the
    # documented `init`. This reproduces the bootstrap the overlay walkthrough
    # spells out (materialize the kit, run `cruxible lock`, copy the lock back) via
    # the same `build_lock`/`write_lock` the `lock` command uses. See the drift
    # note in the test module docstring: the walkthrough's `init`-then-`lock` order
    # cannot be followed literally for a brand-new standalone kit.
    config = load_config(config_path)
    lock = build_lock(config, config_base_path=kit_dir)
    write_lock(lock, kit_dir / LOCK_FILE_NAME)


@pytest.fixture
def cli_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., Result]]:
    """A ``cruxible ...`` invoker bound to a fresh ephemeral in-process daemon.

    Mirrors ``tests/test_demos/test_kev_quickstart_smoke.py`` /
    ``tests/test_cli/test_state_command_flow_smoke.py``: a real ``CruxibleClient``
    whose HTTP transport is swapped for a FastAPI ``TestClient`` over a freshly
    created ``create_app()``, seeded onto the CLI root context so every invocation
    exercises the real CLI wiring and FastAPI routes without binding a socket.
    Default permission mode is ADMIN (``CRUXIBLE_MODE`` unset), matching the
    bootstrap/canonical-apply surface the walkthrough assumes.
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


def test_standalone_kit_authoring_walkthrough_via_cli(
    cli_runner: Callable[..., Result],
    tmp_path: Path,
) -> None:
    invoke = cli_runner

    # ── Author the standalone kit on disk (Walkthrough 1, steps 1-3) ──
    kit_dir = tmp_path / "my-risk-kit"
    _author_standalone_kit(kit_dir)
    config_path = kit_dir / "config.yaml"
    readme_path = kit_dir / "README.md"

    # ── Step 1: `validate --config <kit>/config.yaml` → reported valid ──
    validated = _ok(
        invoke("validate", "--config", str(config_path)),
        "validate config",
    )
    assert "is valid" in validated.output
    # The summary line reports the model shape we authored.
    assert "2 entity types" in validated.output
    assert "1 relationships" in validated.output
    assert "1 queries" in validated.output

    # ── Step 2: `init --kit file://<kit>` → instance created ──
    init = _ok(
        invoke("init", "--kit", f"file://{kit_dir}"),
        "init from file:// kit",
    )
    instance_id = _instance_id_from_init(init)
    assert instance_id

    # ── Step 3: `lock` → lock file written (config + artifact content hashes) ──
    locked = _ok(invoke("lock", instance_id=instance_id), "lock")
    # The lock summary echoes the config digest and the locked artifact count;
    # the artifact's content hash is what makes lock content-pinned.
    assert "digest=sha256:" in locked.output
    assert "artifacts=1" in locked.output

    # ── Step 4: `run --workflow build_local_state --save-preview preview.json` ──
    preview_file = tmp_path / "preview.json"
    run = _ok(
        invoke(
            "run",
            "--workflow",
            "build_local_state",
            "--save-preview",
            str(preview_file),
            "--json",
            instance_id=instance_id,
        ),
        "run build_local_state",
    )
    run_payload = json.loads(run.stdout)
    assert run_payload["canonical"] is True
    assert run_payload["apply_digest"], "run produced no apply_digest"
    assert preview_file.exists(), "--save-preview did not write the preview file"
    saved_preview = json.loads(preview_file.read_text())
    assert saved_preview["apply_digest"] == run_payload["apply_digest"]

    # ── Step 5: `apply --preview-file preview.json` → entities + edges committed ──
    applied = _ok(
        invoke(
            "apply",
            "--preview-file",
            str(preview_file),
            "--json",
            instance_id=instance_id,
        ),
        "apply preview",
    )
    apply_payload = json.loads(applied.stdout)
    assert apply_payload["committed_snapshot_id"], "apply committed no snapshot"

    # ── Step 6: `query run asset_owner --param asset_id=ASSET-1` → rows + receipt ──
    queried = _ok(
        invoke(
            "query",
            "run",
            "asset_owner",
            "--param",
            "asset_id=ASSET-1",
            "--json",
            instance_id=instance_id,
        ),
        "query asset_owner",
    )
    query_payload = json.loads(queried.stdout)
    assert query_payload["receipt_id"], "query returned no receipt to inspect"
    items = query_payload["items"]
    assert items, "asset_owner query for ASSET-1 returned no owner"
    # The traversal reaches OWNER-1 (Platform Team), the owner of ASSET-1 in the CSV.
    flat = json.dumps(query_payload)
    assert "OWNER-1" in flat, f"expected ASSET-1's owner OWNER-1 in result:\n{flat}"
    receipt_id = query_payload["receipt_id"]

    # ── Step 7: `explain --receipt <id> --format markdown` → markdown + trace ──
    explained = _ok(
        invoke(
            "explain",
            "--receipt",
            receipt_id,
            "--format",
            "markdown",
            instance_id=instance_id,
        ),
        "explain receipt",
    )
    explain_out = explained.output
    # Markdown render: a heading plus the named query the receipt traces.
    assert "#" in explain_out, f"explain did not render markdown headings:\n{explain_out}"
    assert "asset_owner" in explain_out, (
        f"explain markdown is missing the traced query name:\n{explain_out}"
    )
    # Trace detail: the receipt renders the concrete entry point and the traversed
    # edge (Asset:ASSET-1 --[asset_owned_by]--> Owner:OWNER-1), not just a heading.
    assert "ASSET-1" in explain_out and "OWNER-1" in explain_out, (
        f"explain markdown is missing the traversal trace detail:\n{explain_out}"
    )
    assert "asset_owned_by" in explain_out, (
        f"explain markdown is missing the traversed relationship:\n{explain_out}"
    )

    # ── Step 8: `config views --runtime --update-readme <kit>/README.md` ──
    # This is a local (no-daemon) command operating on the config file directly.
    before = readme_path.read_text()
    views = _ok(
        invoke(
            "config",
            "views",
            "--config",
            str(config_path),
            "--runtime",
            "--update-readme",
            str(readme_path),
        ),
        "config views --update-readme",
    )
    assert f"Updated {readme_path}" in views.output
    after = readme_path.read_text()
    assert after != before, "config views did not modify the README"
    # The marker blocks survive and now wrap generated content (the ontology view
    # names our entity types).
    assert "<!-- CRUXIBLE:BEGIN ontology -->" in after
    assert "<!-- CRUXIBLE:END ontology -->" in after
    assert "Asset" in after and "Owner" in after, (
        "refreshed README ontology block is missing the authored entity types"
    )
