"""An MCP server started in a folder `cruxible init` set up acts as its owner.

`init` records ``.cruxible/identity.json`` (a principal ID, its instance and the
absolute key directory; never a key or a token). With no principal in its
environment the MCP server started there acts as that principal: attributed
writes, approval signing and Claim attestation. The environment still wins; a
record that cannot be used refuses with its typed error; a TCP daemon ignores it.

The end-to-end tests drive one real daemon on a scratch socket, started and
stopped here (`CRUXIBLE_NO_AUTOSTART=1`: nothing else is started).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from cruxible_client import contracts
from cruxible_client.authoring.folder_identity import (
    FOLDER_IDENTITY_PATH,
    FolderIdentityRefused,
    read_folder_identity,
)
from cruxible_core.cli.main import cli
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.mcp import handlers
from cruxible_core.mcp.daemon import DaemonTarget
from cruxible_core.mcp.identity import folder_candidate, local_identity
from cruxible_core.mcp.target import require_instance_id
from cruxible_core.workspace_root import WorkspaceBoundaryRefused
from tests.core_support._claim_type_support import claim_type_input_from_artifact
from tests.core_support._knowledge_loop_support import SUBJECT_KIND
from tests.core_support._subject_kind_support import create_by_slug_input
from tests.test_claims.test_claims import _claim_type
from tests.test_cli.test_playbill_knowledge_loop_smoke import CLAIM_SOURCE_ID, _claim_authoring

_IDENTITY_ENV = (
    "CRUXIBLE_PRINCIPAL_ID",
    "CRUXIBLE_PRINCIPAL_KEY",
    "CRUXIBLE_MCP_KEY_DIR",
    "CRUXIBLE_SERVER_BEARER_TOKEN",
)


# -- one real daemon on a scratch socket ---------------------------------------


@pytest.fixture
def world(daemon_world: dict[str, Path]) -> dict[str, Path]:
    return daemon_world


def _cli(*args: str) -> Result:
    result = CliRunner().invoke(cli, list(args))
    assert result.exit_code == 0, f"cruxible {' '.join(args)}\n{result.output}"
    return result


def _json(*args: str) -> Any:
    extra = () if args[:1] == ("authoring",) else ("--json",)
    return json.loads(_cli(*args, *extra).stdout)


def _write(path: Path, payload: Any) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _init(world: dict[str, Path], folder: Path, principal_id: str = "owner") -> dict[str, Any]:
    previous = Path.cwd()
    os.chdir(folder)
    try:
        initialized = _json(
            "--server-socket", str(world["socket"]), "init", "--principal-id", principal_id
        )
    finally:
        os.chdir(previous)
    handlers.reset_client_cache()
    return initialized


def _forget_cli_context(world: dict[str, Path]) -> None:
    """An agent session's CLI shares no remembered context with the person's."""

    (world["scratch"] / "context.json").unlink(missing_ok=True)


# -- end to end ------------------------------------------------------------------


def test_an_mcp_server_in_an_initialized_folder_acts_as_its_owner(
    world: dict[str, Path],
) -> None:
    folder = world["folder"]
    initialized = _init(world, folder)
    record_path = folder / FOLDER_IDENTITY_PATH
    assert initialized["folder_identity_path"] == str(record_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    instance_id = initialized["instance_id"]
    key_dir = world["config"].resolve() / "cruxible" / "keys" / instance_id / "owner"
    # The record names a principal, its instance and a local path; no key, no token.
    assert record == {
        "tag": "cruxible-folder-identity-v1",
        "instance_id": instance_id,
        "principal_id": "owner",
        "key_dir": str(key_dir),
    }
    assert (key_dir / "owner.ed25519").read_text() not in record_path.read_text()

    # No principal anywhere in the MCP server's environment.
    assert not any(os.environ.get(name) for name in _IDENTITY_ENV)
    assert require_instance_id() == instance_id
    whoami = handlers.handle_playbill_whoami(instance_id)
    assert whoami.identity_source == "folder"
    assert whoami.identity.actor_id == "owner"
    assert whoami.identity.can_author is True, whoami.identity.authoring_refusal

    # Submit: an attributed proposal, held for approval.
    proposed = handlers.handle_playbill_propose_claim_type(
        instance_id,
        claim_type_input_from_artifact(_claim_type(), source_ids=(CLAIM_SOURCE_ID,)),
        "folder-owner-claim-type",
        accept="never",
    )
    proposal = proposed.proposal.proposal
    assert proposed.proposal.status == "admitted"
    assert proposal["admission"]["actor_id"] == "owner"
    proposal_id = proposal["admission"]["proposal_id"]

    # Approve: signed with the folder owner's key, no signer or key directory named.
    approved = handlers.handle_playbill_approve(
        instance_id, proposal_id, signer_id=None, candidate_digest=None
    )
    assert isinstance(approved, contracts.ApprovalReceipt)
    assert (approved.signer_id, approved.submitted_by) == ("owner", "owner")
    assert approved.activation is not None and approved.activation.status == "accepted"

    # Attest: a Claim the CLI wrote (as the same folder owner), signed with its key.
    (folder / "claim-status.md").write_text("status: ready\n", encoding="utf-8")
    _forget_cli_context(world)
    assert _json("whoami")["actor_id"] == "owner"
    _cli("files", "add", CLAIM_SOURCE_ID, "claim-status.md")
    subject = _json(
        "authoring",
        "submit",
        _write(
            world["scratch"] / "subject.json",
            create_by_slug_input(SUBJECT_KIND, "wi-7", declare=True).model_dump(mode="json"),  # type: ignore[attr-defined]
        ),
        "--no-accept",
    )
    _json("proposal", "activate", str(subject["status"]["proposal_id"]))
    bound = _json(
        "authoring",
        "bind",
        "--file",
        str(folder / "claim-status.md"),
        "--anchor",
        "status: ready",
        "--payload-file",
        _write(
            world["scratch"] / "claim.json",
            _claim_authoring("wi-7", "ready").model_dump(mode="json"),
        ),
    )
    submitted = _json("authoring", "submit", "--intent-id", str(bound["certificate"]["intent_id"]))
    assert submitted["outcome"]["status"] == "landed", submitted["outcome"]
    claim_id = submitted["intent"]["semantic_identity"]
    attested = handlers.handle_playbill_claim_attest(
        instance_id, claim_id, "support", because="examined from the folder"
    )
    assert attested.submitted_by == "owner"


def test_environment_variables_override_the_folder_record(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_id = _init(world, world["folder"])["instance_id"]
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_ID", "someone-else")
    handlers.reset_client_cache()

    whoami = handlers.handle_playbill_whoami(instance_id)
    assert whoami.identity_source == "environment"
    assert whoami.identity.actor_id == "someone-else"
    assert whoami.identity.can_author is False


def test_without_a_record_the_server_is_the_unconfigured_operator(
    world: dict[str, Path],
) -> None:
    instance_id = _init(world, world["folder"])["instance_id"]
    (world["folder"] / FOLDER_IDENTITY_PATH).unlink()

    whoami = handlers.handle_playbill_whoami(instance_id)
    assert whoami.identity_source == "none"
    assert whoami.identity.actor_id == "operator"
    refusal = whoami.identity.authoring_refusal
    assert refusal is not None and refusal.code == "cruxible.identity.principal_unconfigured"
    assert "cruxible init" in refusal.detail


def test_a_second_projects_record_is_never_used_from_the_first(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _init(world, world["folder"])["instance_id"]
    second = _init(world, world["other"], principal_id="other-owner")["instance_id"]
    assert first != second
    (world["folder"] / FOLDER_IDENTITY_PATH).unlink()

    # From the first project only its own (now absent) record counts.
    monkeypatch.chdir(world["folder"])
    handlers.reset_client_cache()
    assert handlers.handle_playbill_whoami(first).identity_source == "none"
    # From the second project its own owner acts.
    monkeypatch.chdir(world["other"])
    handlers.reset_client_cache()
    whoami = handlers.handle_playbill_whoami(second)
    assert (whoami.identity_source, whoami.identity.actor_id) == ("folder", "other-owner")


def _rewrite_record(folder: Path, **changes: str) -> None:
    path = folder / FOLDER_IDENTITY_PATH
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, **changes}), encoding="utf-8")
    handlers.reset_client_cache()


def _refusal(instance_id: str) -> FolderIdentityRefused:
    with pytest.raises(FolderIdentityRefused) as refused:
        handlers.handle_playbill_whoami(instance_id)
    assert "cruxible init" in str(refused.value)
    return refused.value


def test_an_unusable_record_refuses_with_its_typed_error(
    world: dict[str, Path], tmp_path: Path
) -> None:
    folder = world["folder"]
    instance_id = _init(world, folder)["instance_id"]
    record = read_folder_identity(folder)
    assert record is not None

    _rewrite_record(folder, key_dir=str(tmp_path / "gone"))
    assert _refusal(instance_id).error_code == "cruxible.identity.folder_key_unavailable"

    stranger = tmp_path / "stranger-keys"
    generate_client_principal_key(
        stranger, principal_id="owner", kind="ordinary", forbidden_roots=()
    )
    _rewrite_record(folder, key_dir=str(stranger))
    assert _refusal(instance_id).error_code == "cruxible.identity.folder_key_mismatch"

    unknown = tmp_path / "unknown-keys"
    generate_client_principal_key(
        unknown, principal_id="nobody", kind="ordinary", forbidden_roots=()
    )
    _rewrite_record(folder, principal_id="nobody", key_dir=str(unknown))
    assert _refusal(instance_id).error_code == "cruxible.identity.folder_principal_unknown"

    _rewrite_record(folder, principal_id="owner", key_dir=record.key_dir, instance_id="inst_x")
    assert _refusal(instance_id).error_code == "cruxible.identity.folder_record_invalid"

    # The CLI in the folder warns and acts as no principal instead (a broken
    # record never blocks `server stop`).
    _forget_cli_context(world)
    result = CliRunner().invoke(cli, ["whoami", "--json"])
    assert result.exit_code == 0, result.output
    assert "names instance inst_x" in result.stderr
    assert json.loads(result.stdout)["actor_id"] == "operator"


def test_a_custody_change_mid_session_refuses_the_next_call(world: dict[str, Path]) -> None:
    instance_id = _init(world, world["folder"])["instance_id"]
    record = read_folder_identity(world["folder"])
    assert record is not None
    key, directory = record.private_key_path, record.private_key_path.parent
    assert handlers.handle_playbill_whoami(instance_id).identity_source == "folder"

    # Same MCP session, no cache reset: a chmod keeps the key's inode and mtime.
    for item, mode, restore in (
        (key, 0o000, 0o600),
        (key, 0o640, 0o600),
        (directory, 0o750, 0o700),
    ):
        item.chmod(mode)
        try:
            with pytest.raises(FolderIdentityRefused) as refused:
                handlers.handle_playbill_whoami(instance_id)
            assert refused.value.error_code == "cruxible.identity.folder_key_unavailable"
        finally:
            item.chmod(restore)
    assert handlers.handle_playbill_whoami(instance_id).identity.actor_id == "owner"


def test_the_cli_in_the_folder_acts_as_the_record_and_explicit_flags_win(
    world: dict[str, Path],
) -> None:
    _init(world, world["folder"])
    _forget_cli_context(world)
    assert _json("whoami")["actor_id"] == "owner"
    shown = _json("context", "show")
    assert (shown["principal_id"], shown["principal_source"]) == ("owner", "folder")
    assert _json("--principal-id", "reviewer", "whoami")["actor_id"] == "reviewer"


# -- the rules without a daemon ----------------------------------------------------


def _bound_folder(tmp_path: Path, socket: Path, *, instance_id: str = "inst_a") -> Path:
    folder = tmp_path / "bound"
    (folder / ".cruxible").mkdir(parents=True)
    (folder / ".cruxible" / "coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "instance_id": instance_id,
                "server_socket": str(socket),
            }
        ),
        encoding="utf-8",
    )
    keys = tmp_path / "keys"
    generate_client_principal_key(keys, principal_id="owner", kind="ordinary", forbidden_roots=())
    (folder / FOLDER_IDENTITY_PATH).write_text(
        json.dumps(
            {
                "tag": "cruxible-folder-identity-v1",
                "instance_id": instance_id,
                "principal_id": "owner",
                "key_dir": str(keys.resolve()),
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_a_tcp_daemon_ignores_the_folder_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "d.sock"
    folder = _bound_folder(tmp_path, socket)
    for name in _IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(folder))

    local = DaemonTarget(None, str(socket), "workspace")
    source, principal, candidate = local_identity(target=local, token=None)
    assert (source, principal) == ("folder", "owner") and candidate is not None

    tcp = DaemonTarget("http://127.0.0.1:9", None, "environment")
    assert local_identity(target=tcp, token=None) == ("none", None, None)
    # A bearer credential decides who acts, whatever the folder records.
    assert local_identity(target=local, token="token")[0] == "environment"
    # A daemon other than the binding's is not this folder's.
    other = DaemonTarget(None, str(tmp_path / "other.sock"), "environment")
    assert folder_candidate(dict(os.environ), target=other) is None


def test_an_other_owned_marker_refuses_rather_than_reading_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "d.sock"
    folder = _bound_folder(tmp_path, socket)
    nested = folder / "src"
    nested.mkdir()
    monkeypatch.delenv("CRUXIBLE_MCP_WORKSPACE_ROOT", raising=False)
    monkeypatch.chdir(nested)
    real_stat = Path.stat

    def foreign(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        status = real_stat(self, *args, **kwargs)
        if self == folder:
            values = list(status)
            values[4] = status.st_uid + 1
            return os.stat_result(values)
        return status

    monkeypatch.setattr(Path, "stat", foreign)
    with pytest.raises(WorkspaceBoundaryRefused):
        folder_candidate(dict(os.environ), target=DaemonTarget(None, str(socket), "workspace"))
