"""Falsifiability smoke for the documented agent recipes.

``docs/for-ai-agents.md`` ships a set of "recipes" — copy-pasteable command
sequences an agent is told to run. This test executes each non-promote
lifecycle recipe's documented command sequence end-to-end and asserts it
behaves as the docs claim, so the docs cannot silently drift from the code.

Coverage (recipe -> docs anchor):

* Recipe: Validate And Lock          (``for-ai-agents.md`` :111-131)
* Recipe: Refresh Canonical State    (:133-162)
* Recipe: Debug Provider Failure     (:249-272)
* Recipe: Update Source Data Safely  (:273-286)
* Recipe: Regenerate Kit Docs        (:288-305)

Plus a thin "commands resolve" check over the KEV daily-triage SKILL
(``kits/kev-triage/skills/kev-triage/SKILL.md`` :98-282) — that the documented
command surface (``state pull-preview/pull-apply``, the propose chain, group
review entry points) exists and accepts the documented arguments.

The propose -> resolve -> trust "promote" flow is intentionally NOT re-covered
here; it is owned by ``test_promote_command_flow`` / a sibling smoke. Where a
recipe overlaps the published quickstart, the daemon-backed lifecycle setup is
deliberately reused (mirroring ``test_kev_quickstart_smoke``) rather than
duplicated.

The recipes split across two real execution surfaces, and the test exercises
each through the surface an agent actually uses:

* ``validate`` / ``lock`` / ``run`` / ``apply`` / ``decision-record events``
  are daemon HTTP calls, driven through the real ``CruxibleClient`` over an
  in-process FastAPI ``TestClient`` (no socket, no network).
* ``config views`` (``--view``/``--update-readme``) is a *local* CLI command
  that operates on config/README files directly; it is driven through the real
  Click ``cli`` entry point.

No live daemon, no sockets: the KEV reference and triage data both build from
bundled, digest-pinned local artifacts.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
KEV_REFERENCE_CONFIG = REPO_ROOT / "kits" / "kev-reference" / "config.yaml"
KEV_TRIAGE_CONFIG = REPO_ROOT / "kits" / "kev-triage" / "config.yaml"
KEV_TRIAGE_README = REPO_ROOT / "kits" / "kev-triage" / "README.md"


@pytest.fixture
def daemon_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CruxibleClient]:
    """A real CruxibleClient bound to a fresh ephemeral in-process daemon.

    Mirrors ``test_kev_quickstart_smoke``: the client's sync HTTP transport is
    a FastAPI ``TestClient`` over a freshly created ``create_app()``. State
    lives under a per-test temp dir; default permission mode is ADMIN
    (``CRUXIBLE_MODE`` unset), matching the admin surface the recipes assume
    for bootstrap, canonical apply, and trace inspection.
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
    client._client = test_client
    try:
        yield client
    finally:
        test_client.close()
        get_manager().clear()


def _preview_and_apply(
    client: CruxibleClient,
    instance_id: str,
    workflow_name: str,
) -> str:
    """Recipe: Refresh Canonical State — ``run --save-preview`` then ``apply``.

    Returns the committed snapshot id. This is the exact two-step shape the
    docs prescribe: a canonical ``run`` produces an ``apply_digest`` (preview
    only), and ``apply`` commits it under optimistic-concurrency guards
    (``expected_apply_digest`` + ``expected_head_snapshot_id``).
    """
    preview = client.workflow_run(instance_id, workflow_name=workflow_name)
    assert preview.canonical is True, f"{workflow_name} is not a canonical workflow"
    assert preview.apply_digest, f"{workflow_name} run produced no apply_digest"
    assert preview.receipt_id

    applied = client.workflow_apply(
        instance_id,
        workflow_name=workflow_name,
        expected_apply_digest=preview.apply_digest,
        expected_head_snapshot_id=preview.head_snapshot_id,
    )
    assert applied.canonical is True
    assert applied.committed_snapshot_id, f"{workflow_name} apply committed no snapshot"
    assert applied.receipt_id
    return applied.committed_snapshot_id


def test_recipe_validate_and_lock(daemon_client: CruxibleClient, tmp_path: Path) -> None:
    """Recipe: Validate And Lock After Edits (``for-ai-agents.md`` :111-131).

    Documented sequence: ``validate --config config.yaml`` then ``lock``.
    Asserts validate reports the config valid, and lock writes/updates the
    lock file the recipe says it produces.
    """
    client = daemon_client

    # ``validate --config <config>`` — the recipe's first command.
    validated = client.validate(config_path=str(KEV_REFERENCE_CONFIG))
    assert validated.valid is True, "kev-reference config did not validate"
    assert validated.name

    init = client.init(str(tmp_path / "kev-reference-workspace"), kit="kev-reference")
    instance_id = init.instance_id

    # ``lock`` — the recipe's second command. The lock file must now exist.
    locked = client.workflow_lock(instance_id)
    assert locked.config_digest, "lock returned no config digest"
    assert locked.lock_path, "lock returned no lock path"
    assert Path(locked.lock_path).exists(), "lock did not write a lock file"

    # The recipe says re-lock after edits; a second lock must keep the file and
    # report a digest (idempotent on an unchanged config).
    relocked = client.workflow_lock(instance_id)
    assert relocked.config_digest == locked.config_digest
    assert Path(relocked.lock_path).exists()


def test_recipe_refresh_canonical_state(daemon_client: CruxibleClient, tmp_path: Path) -> None:
    """Recipe: Refresh Canonical State (``for-ai-agents.md`` :133-162).

    Documented sequence: ``lock`` -> ``run --workflow <canonical> --save-preview``
    (capture ``apply_digest``) -> ``apply --preview-file``. Asserts the preview
    yields an apply digest and the apply commits a snapshot that materializes
    entities and edges.
    """
    client = daemon_client

    init = client.init(str(tmp_path / "kev-reference-workspace"), kit="kev-reference")
    instance_id = init.instance_id
    client.workflow_lock(instance_id)

    committed = _preview_and_apply(client, instance_id, "build_public_kev_reference")
    assert committed

    # "summarize the changed entities/relationships": the canonical apply must
    # have actually materialized state, not committed an empty snapshot.
    stats = client.stats(instance_id)
    assert stats.entity_count > 0, "canonical apply created no entities"
    assert stats.edge_count > 0, "canonical apply created no edges"
    assert stats.head_snapshot_id == committed


def test_recipe_debug_provider_failure(daemon_client: CruxibleClient, tmp_path: Path) -> None:
    """Recipe: Debug Provider Failure (``for-ai-agents.md`` :249-272).

    The recipe lists read-only trace-inspection commands. Two surfaces:

    1. ``config views --config <cfg> --runtime --view workflow-steps`` — a local
       CLI render of the provider/step pipeline (driven via the Click ``cli``).
    2. ``decision-record events --trace <trace-id>`` — a daemon read of the
       execution record for a real run.

    DOC DRIFT (asserted below): the docs say execution traces "prove what
    provider ran, with which provider version, artifact hash, inputs, outputs,
    status, error, and timing", and point at ``decision-record events
    --trace``. But for an ordinary canonical run that surface is *empty* — the
    provider-execution evidence the docs describe lives on the ``traces``
    endpoint (``list_traces`` / ``get_trace``), not on decision-record events.
    This test pins the real behavior so the drift stays visible.
    """
    client = daemon_client

    # (1) Local render of the workflow-steps view — must succeed and emit a
    # provider/step diagram an agent can read.
    runner = CliRunner()
    rendered = runner.invoke(
        cli,
        [
            "config",
            "views",
            "--config",
            str(KEV_REFERENCE_CONFIG),
            "--runtime",
            "--view",
            "workflow-steps",
        ],
    )
    assert rendered.exit_code == 0, rendered.output
    assert "mermaid" in rendered.output, "workflow-steps view rendered no diagram"

    # Run a real canonical workflow so there is execution evidence to inspect.
    init = client.init(str(tmp_path / "kev-reference-workspace"), kit="kev-reference")
    instance_id = init.instance_id
    client.workflow_lock(instance_id)
    preview = client.workflow_run(instance_id, workflow_name="build_public_kev_reference")
    assert preview.apply_digest, "canonical run produced no apply_digest to apply"
    applied = client.workflow_apply(
        instance_id,
        workflow_name="build_public_kev_reference",
        expected_apply_digest=preview.apply_digest,
        expected_head_snapshot_id=preview.head_snapshot_id,
    )
    trace_ids = list(applied.trace_ids or preview.trace_ids or [])
    assert trace_ids, "a real canonical run produced no execution traces"
    trace_id = trace_ids[0]

    # (2a) The documented ``decision-record events --trace`` surface. Pin the
    # real (empty) result so the doc/code drift is falsifiable: if events ever
    # start being recorded here for plain runs, this assertion fails loudly and
    # the doc claim becomes true.
    events = client.list_decision_events(instance_id, trace_id=trace_id)
    assert events.total == 0, (
        "decision-record events now returns rows for an ordinary canonical run; "
        "the for-ai-agents.md Debug Provider Failure recipe should be updated to "
        "match (it currently points at decision-record events for trace data)"
    )

    # (2b) Where the provider-execution evidence the recipe promises actually
    # lives: the traces endpoint. This is what an agent must read to "prove what
    # provider ran, with which provider version, ... status, error, and timing".
    listed = client.list_traces(instance_id)
    assert listed.total > 0, "no execution traces recorded for the run"
    trace = client.get_trace(instance_id, trace_id)
    assert trace["provider_name"], "trace is missing the provider name"
    assert trace["provider_version"], "trace is missing the provider version"
    assert trace["status"], "trace is missing execution status"
    assert "input_payload" in trace and "output_payload" in trace
    assert "duration_ms" in trace


def test_recipe_update_source_data(daemon_client: CruxibleClient, tmp_path: Path) -> None:
    """Recipe: Update Source Data Safely (``for-ai-agents.md`` :273-286).

    Documented cycle after a source-data change: validate -> (re)lock ->
    run canonical in preview -> apply only after review. Asserts the cycle
    re-applies cleanly and is idempotent — a second validate/lock/run/apply
    pass over unchanged source data commits again without drift in the config
    digest.
    """
    client = daemon_client

    init = client.init(str(tmp_path / "kev-reference-workspace"), kit="kev-reference")
    instance_id = init.instance_id

    # First full cycle.
    assert client.validate(config_path=str(KEV_REFERENCE_CONFIG)).valid is True
    first_lock = client.workflow_lock(instance_id)
    first_commit = _preview_and_apply(client, instance_id, "build_public_kev_reference")

    # Re-run the documented cycle (the "source data changed" path, here with
    # unchanged source so the smoke is deterministic). It must re-apply cleanly.
    assert client.validate(config_path=str(KEV_REFERENCE_CONFIG)).valid is True
    second_lock = client.workflow_lock(instance_id)
    assert second_lock.config_digest == first_lock.config_digest, (
        "re-locking unchanged config produced a different digest"
    )
    second_commit = _preview_and_apply(client, instance_id, "build_public_kev_reference")
    assert second_commit, "re-applying the canonical workflow committed no snapshot"
    assert second_commit != first_commit, "re-apply did not advance the snapshot head"


def test_recipe_regenerate_kit_docs(tmp_path: Path) -> None:
    """Recipe: Regenerate Kit Docs (``for-ai-agents.md`` :288-305).

    Documented command:
    ``config views --config <kit>/config.yaml --runtime --update-readme <README>``.
    Asserts the generated ``CRUXIBLE:BEGIN/END`` marker blocks are written into
    the README, the markers survive, and the regeneration is idempotent (a
    second run is a no-op on already-current content). Runs against a *copy* of
    the kev-triage README so the repo file is never mutated.
    """
    runner = CliRunner()

    readme_copy = tmp_path / "README.md"
    shutil.copy(KEV_TRIAGE_README, readme_copy)
    original_text = readme_copy.read_text()
    assert "<!-- CRUXIBLE:BEGIN ontology -->" in original_text, (
        "fixture README is missing the marker blocks the recipe targets"
    )

    first = runner.invoke(
        cli,
        [
            "config",
            "views",
            "--config",
            str(KEV_TRIAGE_CONFIG),
            "--runtime",
            "--update-readme",
            str(readme_copy),
        ],
    )
    assert first.exit_code == 0, first.output
    assert f"Updated {readme_copy}" in first.output

    regenerated = readme_copy.read_text()
    # The marker blocks must survive a regeneration.
    for marker in (
        "<!-- CRUXIBLE:BEGIN ontology -->",
        "<!-- CRUXIBLE:END ontology -->",
        "<!-- CRUXIBLE:BEGIN workflow-pipeline -->",
        "<!-- CRUXIBLE:BEGIN query-map -->",
    ):
        assert marker in regenerated, f"regeneration dropped marker {marker!r}"
    # Generated diagram content must actually be present inside the blocks.
    assert "```mermaid" in regenerated

    # Idempotence: re-running on the now-current README must not change it.
    second = runner.invoke(
        cli,
        [
            "config",
            "views",
            "--config",
            str(KEV_TRIAGE_CONFIG),
            "--runtime",
            "--update-readme",
            str(readme_copy),
        ],
    )
    assert second.exit_code == 0, second.output
    assert readme_copy.read_text() == regenerated, "regenerating kit docs twice is not idempotent"


def test_recipe_regenerate_kit_docs_changes_stale_content() -> None:
    """The Regenerate Kit Docs recipe actually rewrites stale marker blocks.

    Proves the recipe does real work (not a silent no-op) WITHOUT depending on
    whether the committed README happens to be in sync (that is owned by the
    stale-docs work item). We inject a stale sentinel between a marker pair and
    assert the recipe overwrites it with the live rendering, and that the result
    differs from the staled input and is idempotent.
    """
    import re

    from cruxible_core.canonical_views.config import (
        load_config_for_rendering,
        render_readme_update,
        selected_view_keys,
    )

    config = load_config_for_rendering(KEV_TRIAGE_CONFIG, runtime=True)
    current = KEV_TRIAGE_README.read_text()
    sentinel = "STALE_PLACEHOLDER_THAT_MUST_BE_OVERWRITTEN"
    staled, n = re.subn(
        r"(<!-- CRUXIBLE:BEGIN ontology -->).*?(<!-- CRUXIBLE:END ontology -->)",
        rf"\1\n{sentinel}\n\2",
        current,
        flags=re.DOTALL,
    )
    assert n == 1, "ontology marker pair not found in the committed README"

    regenerated = render_readme_update(staled, config, selected_view_keys("all"))
    # The recipe REWRITES the marker block: stale content is gone, live render present.
    assert sentinel not in regenerated, "recipe did not overwrite stale marker content"
    assert regenerated != staled, "recipe was a silent no-op on stale input"
    assert "<!-- CRUXIBLE:BEGIN ontology -->" in regenerated
    # Re-rendering the just-regenerated text is a fixed point (idempotent).
    twice = render_readme_update(regenerated, config, selected_view_keys("all"))
    assert twice == regenerated


def test_kev_triage_skill_command_surface_resolves() -> None:
    """KEV daily-triage SKILL command surface resolves (thin "wired" check).

    ``kits/kev-triage/skills/kev-triage/SKILL.md`` (:98-282) tells the triage
    agent to run ``state pull-preview`` / ``state pull-apply --apply-digest``,
    the ``propose`` chain, and ``group list/propose`` review entry points. This
    asserts those commands exist and accept the documented arguments — a
    "recipe commands resolve" check, not a full triage run.

    DOC DRIFT (asserted): the SKILL's read commands are written as
    ``cruxible query --query <name> --param ...`` and
    ``cruxible query describe --query <name>``. The real runnable surface is
    ``cruxible query run <QUERY_NAME>`` (positional). ``query`` is a command
    group, not a runnable command, and ``query run`` takes the query name as a
    positional argument, not a ``--query`` option. This test pins both the real
    surface and the broken SKILL form so the drift is visible.
    """
    runner = CliRunner()

    def options_of(args: list[str]) -> set[str]:
        result = runner.invoke(cli, [*args, "--help"])
        assert result.exit_code == 0, f"{' '.join(args)} did not resolve: {result.output}"
        return {tok.strip(",.") for tok in result.output.split() if tok.startswith("--")}

    # Daily-refresh path: state pull-preview / pull-apply --apply-digest.
    options_of(["state", "pull-preview"])
    assert "--apply-digest" in options_of(["state", "pull-apply"]), (
        "state pull-apply lost its --apply-digest option"
    )

    # Local proposal chain entry point: propose --workflow.
    assert "--workflow" in options_of(["propose"]), "propose lost its --workflow option"

    # Group review entry points used by the triage + intake tasks.
    assert "--status" in options_of(["group", "list"])
    group_propose_opts = options_of(["group", "propose"])
    assert {"--relationship", "--members", "--thesis"} <= group_propose_opts, (
        "group propose lost a documented option (--relationship/--members/--thesis)"
    )

    # The real query surface the SKILL *should* document: `query run <name>`.
    run_result = runner.invoke(cli, ["query", "run", "--help"])
    assert run_result.exit_code == 0
    assert "QUERY_NAME" in run_result.output, "query run no longer takes a positional QUERY_NAME"

    # Pin the SKILL's broken form: `query run --query <name>` must NOT parse.
    broken = runner.invoke(cli, ["query", "run", "--query", "vendor_products"])
    assert broken.exit_code != 0
    assert "No such option: --query" in broken.output, (
        "the SKILL's `query --query` form now parses; update SKILL.md drift note"
    )
