"""From a bare folder to authoring by tools alone: `cruxible_init` and submit by path.

`cruxible_init` makes the MCP workspace folder an instance (host, binding,
owner key outside the folder, folder identity record) over the daemon the
adapter finds, and the same server session then authors as the owner.
`cruxible_authoring_submit_file` submits a workspace file as `cruxible
authoring submit FILE` does, read beneath the workspace root without
following a link. The end-to-end tests drive one real scratch daemon
(`daemon_world` in this directory's conftest).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from mcp.shared.memory import create_connected_server_and_client_session

from cruxible_client import contracts
from cruxible_client.contracts.workspace_layout import WorkspaceDirectoryConflict
from cruxible_core.cli.main import cli
from cruxible_core.errors import ConfigError, DataValidationError, PermissionDeniedError
from cruxible_core.mcp import daemon, handlers
from cruxible_core.mcp.daemon import ensure_local_daemon as real_ensure_local_daemon
from cruxible_core.mcp.server import create_server
from cruxible_core.mcp.target import require_instance_id
from cruxible_core.runtime.permissions import reset_permissions
from tests.core_support._claim_type_support import claim_type_input_from_artifact
from tests.core_support._knowledge_loop_support import SUBJECT_KIND
from tests.core_support._subject_kind_support import create_by_slug_input
from tests.test_claims.test_claims import _claim_type
from tests.test_cli.test_playbill_knowledge_loop_smoke import CLAIM_SOURCE_ID


@pytest.fixture
def world(daemon_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    # The adapter finds the scratch daemon on the state root's default socket,
    # exactly as a plugin session does; nothing is started.
    monkeypatch.setattr(daemon, "ensure_local_daemon", real_ensure_local_daemon)
    daemon.forget_local_daemon()
    return daemon_world


def _no_key_material(result: object, key_dir: Path) -> None:
    text = json.dumps(result.model_dump(mode="json") if hasattr(result, "model_dump") else result)
    for key in key_dir.glob("*.ed25519"):
        assert key.read_text(encoding="utf-8").strip() not in text
    assert "PRIVATE KEY" not in text


def _subject_file(folder: Path, slug: str) -> str:
    path = folder / "changes" / f"{slug}.json"
    path.parent.mkdir(exist_ok=True)
    payload = create_by_slug_input(SUBJECT_KIND, slug, declare=True).model_dump(mode="json")  # type: ignore[attr-defined]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return f"changes/{slug}.json"


def test_init_by_tool_then_author_submit_and_approve_in_the_same_session(
    world: dict[str, Path],
) -> None:
    folder = world["folder"]
    initialized = handlers.handle_init("owner")

    assert initialized.status == "initialized"
    assert initialized.principal_id == "owner"
    assert initialized.approval_policy == "self_approval_allowed"
    key_dir = Path(initialized.key_dir or "")
    assert (
        key_dir
        == world["config"].resolve() / "cruxible" / "keys" / (initialized.instance_id) / "owner"
    )
    assert not key_dir.is_relative_to(folder)
    assert (key_dir / "owner.ed25519").is_file()
    assert initialized.folder_identity_path == str(folder / ".cruxible" / "identity.json")
    assert (folder / ".cruxible" / "coverage.json").is_file()
    _no_key_material(initialized, key_dir)

    # The same server session, no restart: it acts as the owner now.
    instance_id = require_instance_id()
    assert instance_id == initialized.instance_id
    whoami = handlers.handle_playbill_whoami(instance_id)
    assert (whoami.identity_source, whoami.identity.actor_id) == ("folder", "owner")
    assert whoami.identity.can_author is True
    _no_key_material(whoami, key_dir)

    # Submit by workspace path: lands at once under self_approval_allowed.
    submitted = handlers.handle_playbill_authoring_submit_file(
        instance_id, _subject_file(folder, "wi-1")
    )
    assert isinstance(submitted.outcome, contracts.AuthoringLanded), submitted.outcome
    _no_key_material(submitted, key_dir)

    # A held proposal, approved with the owner's key from the folder record.
    proposed = handlers.handle_playbill_propose_claim_type(
        instance_id,
        claim_type_input_from_artifact(_claim_type(), source_ids=(CLAIM_SOURCE_ID,)),
        "init-tool-claim-type",
        accept="never",
    )
    approved = handlers.handle_playbill_approve(
        instance_id,
        proposed.proposal.proposal["admission"]["proposal_id"],
        signer_id=None,
        candidate_digest=None,
    )
    assert isinstance(approved, contracts.ApprovalReceipt)
    assert approved.signer_id == "owner" and approved.activation is not None
    _no_key_material(approved, key_dir)


def test_init_again_reports_the_instance_and_changes_nothing(world: dict[str, Path]) -> None:
    folder = world["folder"]
    first = handlers.handle_init("owner")
    binding = (folder / ".cruxible" / "coverage.json").read_bytes()
    record = (folder / ".cruxible" / "identity.json").read_bytes()

    again = handlers.handle_init()

    assert again.status == "already_initialized"
    assert (again.instance_id, again.principal_id, again.key_dir) == (
        first.instance_id,
        "owner",
        first.key_dir,
    )
    assert again.approval_policy == "self_approval_allowed"
    assert (folder / ".cruxible" / "coverage.json").read_bytes() == binding
    assert (folder / ".cruxible" / "identity.json").read_bytes() == record


def test_init_refuses_the_home_directory_and_a_folder_inside_cruxible(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(world["home"])
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        handlers.handle_init("owner")
    assert refused.value.reason == "home"

    inside = world["folder"] / ".cruxible"
    inside.mkdir()
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(inside))
    with pytest.raises((WorkspaceDirectoryConflict, ConfigError)):
        handlers.handle_init("owner")
    assert not (inside / "coverage.json").exists()


def _call(name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    return asyncio.run(exercise())


def test_a_lower_tier_refuses_init_and_read_only_refuses_submit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    with pytest.raises(PermissionDeniedError, match="requires ADMIN"):
        handlers.handle_init("owner")
    is_error, text = _call("cruxible_init", {})
    assert is_error and text.startswith(
        "cruxible.mcp.mode_insufficient: tool 'cruxible_init' needs the admin tier; "
        "this MCP server runs at governed_write (CRUXIBLE_MODE)"
    )
    assert "Repair: set CRUXIBLE_MODE=admin (or higher)" in text

    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    is_error, text = _call("cruxible_authoring_submit_file", {"path": "x.json"})
    assert is_error and "needs the governed_write tier; this MCP server runs at read_only" in text
    reset_permissions()


def test_submit_by_path_leaves_a_proposal_under_independent_approval(
    world: dict[str, Path],
) -> None:
    folder = world["folder"]
    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(world["socket"]),
            "init",
            "--principal-id",
            "owner",
            "--reviewer-key-dir",
            str(world["config"] / "reviewer-keys"),
            "--require-independent-approval",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    handlers.reset_client_cache()
    instance_id = require_instance_id()

    submitted = handlers.handle_playbill_authoring_submit_file(
        instance_id, _subject_file(folder, "wi-2")
    )

    assert not isinstance(submitted.outcome, contracts.AuthoringLanded)
    assert submitted.outcome.proposal_id is not None
    assert handlers.handle_init().approval_policy == "independent_approval_required"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "ws"
    (folder / ".cruxible").mkdir(parents=True)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(folder))
    monkeypatch.setattr(
        handlers,
        "handle_playbill_authoring_submit",
        lambda *args, **kwargs: pytest.fail("a refused file reached the daemon"),
    )
    return folder


@pytest.mark.parametrize("path", ["../outside.json", "/etc/hosts", "./x.json", "a//b.json", ""])
def test_submit_by_path_refuses_a_path_outside_the_workspace(workspace: Path, path: str) -> None:
    (workspace.parent / "outside.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DataValidationError, match="not a relative path beneath the root"):
        handlers.handle_playbill_authoring_submit_file("inst_x", path)


def test_submit_by_path_refuses_a_symlink_and_an_oversize_file(workspace: Path) -> None:
    target = workspace / "real.json"
    target.write_text("{}", encoding="utf-8")
    (workspace / "link.json").symlink_to(target)
    os.symlink(workspace, workspace / "dir-link")
    for path in ("link.json", "dir-link/real.json"):
        with pytest.raises(DataValidationError, match="symbolic link|not a readable"):
            handlers.handle_playbill_authoring_submit_file("inst_x", path)

    big = workspace / "big.json"
    big.write_bytes(b" " * (handlers.AUTHORING_FILE_MAX_BYTES + 1))
    with pytest.raises(DataValidationError, match="larger than"):
        handlers.handle_playbill_authoring_submit_file("inst_x", "big.json")

    (workspace / "bad.yaml").write_text("kind: nothing-like-an-input\n", encoding="utf-8")
    with pytest.raises(DataValidationError, match="not an authoring input"):
        handlers.handle_playbill_authoring_submit_file("inst_x", "bad.yaml")


def test_init_with_reviewer_enables_mcp_only_independent_approval(
    world: dict[str, Path],
) -> None:
    folder = world["folder"]
    initialized = handlers.handle_init(
        "owner",
        reviewer_id="reviewer",
        require_independent_approval=True,
    )

    assert initialized.status == "initialized"
    assert initialized.principal_id == "owner"
    assert initialized.reviewer_id == "reviewer"
    assert initialized.approval_policy == "independent_approval_required"
    assert initialized.base_kit_proposal_id is not None
    owner_key_dir = Path(initialized.key_dir or "")
    reviewer_key_dir = Path(initialized.reviewer_key_dir or "")
    assert (owner_key_dir / "owner.ed25519").is_file()
    assert (reviewer_key_dir / "reviewer.ed25519").is_file()
    assert (reviewer_key_dir / "cruxible.env").is_file()
    assert not reviewer_key_dir.is_relative_to(folder)
    _no_key_material(initialized, owner_key_dir)
    _no_key_material(initialized, reviewer_key_dir)

    instance_id = require_instance_id()
    # The reviewer approves the base kit proposal over MCP with no shell and no key path arg.
    base_review = handlers.handle_playbill_review(
        instance_id, initialized.base_kit_proposal_id, include_body=None
    )
    assert isinstance(base_review, contracts.ProposalReviewSummary)
    base_approved = handlers.handle_playbill_approve(
        instance_id,
        initialized.base_kit_proposal_id,
        signer_id="reviewer",
        candidate_digest=base_review.candidate_digest,
    )
    assert isinstance(base_approved, contracts.ApprovalReceipt)
    assert base_approved.signer_id == "reviewer" and base_approved.activation is not None
    _no_key_material(base_approved, reviewer_key_dir)

    # An author's change awaits approval and is approved by reviewer over MCP alone.
    submitted = handlers.handle_playbill_authoring_submit_file(
        instance_id, _subject_file(folder, "wi-cosign")
    )
    assert not isinstance(submitted.outcome, contracts.AuthoringLanded)
    proposal_id = submitted.outcome.proposal_id
    assert proposal_id is not None
    review = handlers.handle_playbill_review(instance_id, proposal_id, include_body=None)
    assert isinstance(review, contracts.ProposalReviewSummary)
    approved = handlers.handle_playbill_approve(
        instance_id,
        proposal_id,
        signer_id="reviewer",
        candidate_digest=review.candidate_digest,
    )
    assert isinstance(approved, contracts.ApprovalReceipt)
    assert approved.signer_id == "reviewer" and approved.activation is not None

    # Re-calling init with the same reviewer/policy reports already_initialized.
    again = handlers.handle_init(reviewer_id="reviewer", require_independent_approval=True)
    assert again.status == "already_initialized"
    assert again.reviewer_id == "reviewer"
    assert again.reviewer_key_dir == str(reviewer_key_dir)


def test_init_refuses_adding_reviewer_to_already_initialized_instance_and_same_id(
    world: dict[str, Path],
) -> None:
    with pytest.raises(DataValidationError, match="must differ"):
        handlers.handle_init("owner", reviewer_id="owner")

    first = handlers.handle_init("owner")
    assert first.status == "initialized" and first.reviewer_id is None
    with pytest.raises(ConfigError, match="already initialized"):
        handlers.handle_init(reviewer_id="reviewer", require_independent_approval=True)
    # Never generates a reviewer key on an already-initialized instance.
    reviewer_key = (
        world["config"].resolve()
        / "cruxible"
        / "keys"
        / first.instance_id
        / "reviewer"
        / "reviewer.ed25519"
    )
    assert not reviewer_key.exists()


def test_codex_plugin_cache_cwd_uses_client_roots_and_refuses_without_roots(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcp import types as mcp_types
    from pydantic import AnyUrl

    from cruxible_core.mcp.workspace import PluginDirectoryConflict

    plugin_cache = tmp_path / "codex-cache" / "cruxible" / "0.5.0"
    plugin_cache.mkdir(parents=True)
    (plugin_cache / "plugin.json").write_text(
        json.dumps({"name": "cruxible", "version": "0.5.0"}), encoding="utf-8"
    )
    (plugin_cache / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    project_a = world["folder"]
    project_b = tmp_path / "project-b"
    project_b.mkdir()

    monkeypatch.delenv("CRUXIBLE_MCP_WORKSPACE_ROOT", raising=False)
    monkeypatch.chdir(plugin_cache)

    # Without client roots or CRUXIBLE_MCP_WORKSPACE_ROOT, never touch the plugin cache.
    with pytest.raises(PluginDirectoryConflict, match="CRUXIBLE_MCP_WORKSPACE_ROOT"):
        handlers.handle_init("owner")
    assert not (plugin_cache / ".cruxible").exists()
    with pytest.raises(PluginDirectoryConflict, match="CRUXIBLE_MCP_WORKSPACE_ROOT"):
        require_instance_id()

    # With MCP client roots, the server discovers the user's project despite plugin-cache cwd.
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    reset_permissions()
    server = create_server()
    active_roots: list[Path] = [project_a]

    async def list_roots_callback(_context: Any) -> mcp_types.ListRootsResult:
        return mcp_types.ListRootsResult(
            roots=[
                mcp_types.Root(uri=AnyUrl(path.as_uri()), name=path.name) for path in active_roots
            ]
        )

    async def exercise() -> tuple[str, str]:
        async with create_connected_server_and_client_session(
            server._mcp_server,
            list_roots_callback=list_roots_callback,
        ) as session:
            await session.initialize()
            init_a = await session.call_tool("cruxible_init", {"principal_id": "owner"})
            assert not init_a.isError, init_a
            payload_a = json.loads(init_a.content[0].text)  # type: ignore[union-attr]
            assert Path(payload_a["workspace_root"]) == project_a.resolve()
            assert not (plugin_cache / ".cruxible").exists()

            whoami_a = await session.call_tool("cruxible_whoami", {})
            assert not whoami_a.isError, whoami_a
            who_payload = json.loads(whoami_a.content[0].text)  # type: ignore[union-attr]
            assert who_payload["instance_id"] == payload_a["instance_id"]
            assert who_payload["identity"]["actor_id"] == "owner"

            # Switching roots via notification invalidates the cached roots.
            active_roots[:] = [project_b]
            await session.send_roots_list_changed()
            init_b = await session.call_tool("cruxible_init", {"principal_id": "owner"})
            assert not init_b.isError, init_b
            payload_b = json.loads(init_b.content[0].text)  # type: ignore[union-attr]
            assert Path(payload_b["workspace_root"]) == project_b.resolve()
            return payload_a["instance_id"], payload_b["instance_id"]

    inst_a, inst_b = asyncio.run(exercise())
    assert inst_a != inst_b
    assert (project_a / ".cruxible" / "coverage.json").is_file()
    assert (project_b / ".cruxible" / "coverage.json").is_file()
    assert not (plugin_cache / ".cruxible").exists()
