"""First run from a bare folder: the folder is the workspace, Git optional.

The shared resolver (`cruxible_core.workspace_root`) picks the nearest folder
holding ``.cruxible/``, else the Git worktree, else the current directory, and
refuses the home directory, a filesystem root, the state root and anything
inside a ``.cruxible`` directory. The end-to-end test drives one real daemon,
started by the CLI itself, from an empty non-Git folder: init (twice), files
add, floor export, a Claim citing a catalogued file, an edit that ``next``
reports as drift, and ``git init`` followed by an attach that turns the ledger
mirror on. Every daemon it starts it stops.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.workspace_layout import WorkspaceDirectoryConflict
from cruxible_core.cli.commands.playbill import LEDGER_MIRROR_OFF, _explicit_workspace_root
from cruxible_core.cli.main import cli
from cruxible_core.mcp import daemon
from cruxible_core.mcp.daemon import _cruxible_executable, default_socket_path
from cruxible_core.mcp.daemon import ensure_local_daemon as real_ensure_local_daemon
from cruxible_core.mcp.workspace import mcp_workspace_root
from cruxible_core.server.state_lock import read_state_lock, state_lock_holder_is_alive
from cruxible_core.workspace_root import resolve_workspace_root
from tests.core_support._claim_type_support import claim_type_input_from_artifact
from tests.core_support._knowledge_loop_support import SUBJECT_KIND
from tests.core_support._subject_kind_support import create_by_slug_input
from tests.support.short_temporary_root import short_temporary_directory
from tests.test_claims.test_claims import _claim_type
from tests.test_cli.test_playbill_knowledge_loop_smoke import CLAIM_SOURCE_ID, _claim_authoring

# -- the resolver -------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    return home.resolve()


def _found(start: Path) -> tuple[Path, str]:
    resolved = resolve_workspace_root(start)
    assert resolved is not None
    return resolved.root, resolved.source


def test_a_plain_folder_is_its_own_workspace_until_one_holds_cruxible(home: Path) -> None:
    folder = home / "project"
    sub = folder / "src" / "pkg"
    sub.mkdir(parents=True)

    assert _found(folder) == (folder, "directory")
    assert _found(sub) == (sub, "directory")
    assert resolve_workspace_root(sub, fallback_to_start=False) is None

    (folder / ".cruxible").mkdir()
    # The nearest ancestor holding .cruxible/ wins from any depth.
    assert _found(sub) == (folder, "cruxible_directory")
    assert _explicit_workspace_root(str(sub)) == folder


def test_a_git_worktree_is_found_and_a_nearer_cruxible_folder_wins(home: Path) -> None:
    repo = home / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    assert _found(deep) == (repo, "git")
    resolved = resolve_workspace_root(deep)
    assert resolved is not None and resolved.is_git

    (repo / "a" / ".cruxible").mkdir()
    assert _found(deep) == (repo / "a", "cruxible_directory")


def test_home_state_root_and_control_directories_are_refused(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        resolve_workspace_root(home)
    assert refused.value.reason == "home"
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        resolve_workspace_root(Path("/"))
    assert refused.value.reason == "filesystem_root"

    # The home directory's .cruxible is the state root, never a workspace: a
    # project under home with none of its own is its own folder.
    (home / ".cruxible").mkdir()
    project = home / "project"
    project.mkdir()
    assert _found(project) == (project, "directory")

    state_root = tmp_path / "state"
    inside = state_root / "instances" / "inst_x"
    inside.mkdir(parents=True)
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        resolve_workspace_root(inside)
    assert refused.value.reason == "state_root"

    # Inside a workspace's own .cruxible the folder holding it is selected; a
    # .cruxible nested inside another .cruxible is refused.
    floor = project / ".cruxible" / "floor"
    floor.mkdir(parents=True)
    assert _found(floor) == (project, "cruxible_directory")
    (floor / ".cruxible").mkdir()
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        resolve_workspace_root(floor)
    assert refused.value.reason == "control_directory"

    # The MCP adapter resolves its implicit root the same way.
    monkeypatch.delenv("CRUXIBLE_MCP_WORKSPACE_ROOT", raising=False)
    nested = project / "docs"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert mcp_workspace_root({}) == project


def test_cli_commands_that_need_a_workspace_name_why_home_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    for command in (
        ["--server-socket", str(home.parent / "d.sock"), "workspace", "attach"],
        ["files", "add", "x", "notes.md"],
    ):
        result = CliRunner().invoke(cli, command)
        assert result.exit_code == 2, result.output
        assert "needs a workspace folder" in result.output
        assert "project folder" in result.output


# -- end to end: one real daemon, started by the CLI --------------------------


@pytest.fixture
def first_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    if _cruxible_executable() is None:
        pytest.skip("no cruxible executable beside this interpreter to start a daemon")
    # The state root holds the socket, so it needs a short path; the folder is
    # under tmp_path, where the suite lets a test write a workspace binding.
    scratch = short_temporary_directory("cxf").resolve()
    base = tmp_path.resolve()
    paths = {
        "scratch": base,
        "state": scratch / "s",
        "home": base / "h",
        "folder": base / "h" / "project",
        "context": base / "context.json",
        "config": base / "config",
    }
    paths["folder"].mkdir(parents=True)
    for name in (
        "CRUXIBLE_NO_AUTOSTART",
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_PRINCIPAL_ID",
        "CRUXIBLE_PRINCIPAL_KEY",
        "CRUXIBLE_INSTANCE_ID",
        "CRUXIBLE_WORKSPACE",
        "CRUXIBLE_NO_WORKSPACE",
        "CRUXIBLE_SERVER_AUTH",
        "CRUXIBLE_MODE",
        "GIT_DIR",
        "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(paths["home"]))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(paths["state"]))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(paths["context"]))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(paths["config"]))
    # The daemon this test starts runs this checkout's code.
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            str(Path(item).resolve())
            for item in os.environ.get("PYTHONPATH", "").split(os.pathsep)
            if item
        ),
    )
    monkeypatch.setattr(daemon, "ensure_local_daemon", real_ensure_local_daemon)
    monkeypatch.chdir(paths["folder"])
    try:
        yield paths
    finally:
        holder = read_state_lock(paths["state"])
        if holder is not None and state_lock_holder_is_alive(paths["state"]):
            CliRunner().invoke(cli, ["--no-autostart", "server", "stop"])
            _wait_for_exit(holder.pid)
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)


def _wait_for_exit(pid: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                return
        except ChildProcessError:
            # Not this process's child (or already reaped): poll for it to go.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)


def _run(*args: str) -> Result:
    result = CliRunner().invoke(cli, list(args))
    assert result.exit_code == 0, f"cruxible {' '.join(args)}\n{result.output}"
    return result


def _json(*args: str) -> Any:
    extra = () if args[:1] == ("authoring",) else ("--json",)
    return json.loads(_run(*args, *extra).stdout)


def _write(path: Path, payload: Any) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_first_run_from_an_empty_folder_needs_no_git_and_no_flags(
    first_run: dict[str, Path],
) -> None:
    folder, scratch = first_run["folder"], first_run["scratch"]
    socket = default_socket_path(first_run["state"])
    # Another project is already remembered: init must add to it, never replace it.
    other_binding = f"inst_other@unix://{socket.resolve()}"
    first_run["context"].write_text(
        json.dumps(
            {
                "server_socket": str(socket),
                "instance_id": "inst_other",
                "instance_transport": f"unix://{socket.resolve()}",
                "principals": {other_binding: {"settings": {"bob": "/elsewhere/cruxible.env"}}},
            }
        ),
        encoding="utf-8",
    )

    # 1. A bare init starts the daemon and attaches the plain folder.
    initialized = _run("init", "--principal-id", "owner")
    assert "Started a Cruxible daemon on unix socket" in initialized.stderr
    assert "`cruxible server stop` stops it" in initialized.stderr
    coverage_path = folder / ".cruxible" / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    instance_id = coverage["instance_id"]
    assert coverage["server_socket"] == str(socket)
    assert (folder / ".cruxible" / "world.pyi").is_file()
    assert "Workspace refs: not_git" in initialized.stdout
    assert LEDGER_MIRROR_OFF in initialized.stdout
    assert not (folder / ".git").exists()
    context = json.loads(first_run["context"].read_text(encoding="utf-8"))
    assert context["instance_id"] == "inst_other"
    assert context["principals"][other_binding] == {"settings": {"bob": "/elsewhere/cruxible.env"}}
    assert context["principals"][f"{instance_id}@unix://{socket.resolve()}"]["active"] == "owner"
    assert "still remembers host inst_other" in initialized.stderr

    # 2. Init again is safe: it reports the instance and changes nothing.
    coverage_before = coverage_path.read_bytes()
    context_before = first_run["context"].read_bytes()
    again = _run("init")
    assert f"already initialized here: host {instance_id}" in again.stdout
    assert "nothing changed" in again.stdout
    assert coverage_path.read_bytes() == coverage_before
    assert first_run["context"].read_bytes() == context_before
    assert _json("whoami")["actor_id"] == "owner"

    # 3. The floor and the file catalog work in the plain folder.
    exported = _run("floor", "export")
    assert (folder / ".cruxible" / "floor" / "manifest.json").is_file(), exported.output
    source_file = folder / "claim-status.md"
    source_file.write_text("status: ready\n", encoding="utf-8")
    _run("files", "add", CLAIM_SOURCE_ID, "claim-status.md")
    assert CLAIM_SOURCE_ID in (folder / ".cruxible" / "files.yaml").read_text(encoding="utf-8")

    # 4. A Claim cites the catalogued file.
    source_contract = foreign_source_capture_contract(CLAIM_SOURCE_ID)
    claim_type = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="coordinator-self-source",
                        claim_roles=("normative", "observation"),
                        capture_contracts=(source_contract.identity.name,),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    proposed = _json(
        "claim-type",
        "propose",
        "--input",
        _write(
            scratch / "claim-type.json",
            claim_type_input_from_artifact(claim_type, source_ids=(CLAIM_SOURCE_ID,)),
        ),
        "--name",
        "seed-claim-type",
    )
    assert proposed["proposal"]["outcome"]["status"] == "landed", proposed
    subject = _json(
        "authoring",
        "submit",
        _write(
            scratch / "subject.json",
            create_by_slug_input(SUBJECT_KIND, "wi-42", declare=True).model_dump(mode="json"),  # type: ignore[attr-defined]
        ),
        "--no-accept",
    )
    activated = _json("proposal", "activate", str(subject["status"]["proposal_id"]))
    assert activated["status"] == "accepted", activated
    bound = _json(
        "authoring",
        "bind",
        "--file",
        str(source_file),
        "--anchor",
        "status: ready",
        "--payload-file",
        _write(
            scratch / "claim.json",
            _claim_authoring("wi-42", "ready").model_dump(mode="json"),
        ),
    )
    submitted = _run(
        "authoring", "submit", "--intent-id", str(bound["certificate"]["intent_id"]), "--brief"
    )
    assert "outcome: accepted" in submitted.stdout

    # 5. Editing the cited file is drift, and next says so.
    assert not [item for item in _json("next")["items"] if item["reason"] == "citation_drifted"]
    source_file.write_text("status: blocked\n", encoding="utf-8")
    drifted = [item for item in _json("next")["items"] if item["reason"] == "citation_drifted"]
    assert drifted, "next reports no drift after the cited file changed"

    # 6. `git init` and attach again turn the ledger mirror on.
    subprocess.run(["git", "init", "-q", "-b", "main", str(folder)], check=True)
    attached = _run("workspace", "attach")
    assert LEDGER_MIRROR_OFF not in attached.stdout
    refs = subprocess.run(
        ["git", "-C", str(folder), "for-each-ref", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "refs/remotes/cruxible-ledger/accepted" in refs

    # `cruxible server stop` stops the daemon the CLI started.
    holder = read_state_lock(first_run["state"])
    assert holder is not None
    _run("server", "stop")
    _wait_for_exit(holder.pid)
    assert not state_lock_holder_is_alive(first_run["state"])
