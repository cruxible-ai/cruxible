"""One workspace per folder: no command acts on another project's instance.

Root selection has one implementation (`cruxible_client.contracts.workspace_root`)
that the SDK/CLI context and the core/MCP resolver share; a binding is read
only inside the chosen root. Inside a ``.cruxible`` workspace that binds no
instance, a remembered instance recorded for another folder is not used (the
command refuses with the repair); a folder with no workspace directory keeps
the remembered context. ``init --workspace B`` acts on B, never on the folder
it runs from. The daemon-backed cases run one real daemon the CLI starts, and
stop it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client.authoring.context import resolve_context
from cruxible_core.cli.commands import playbill
from cruxible_core.cli.main import cli
from cruxible_core.workspace_root import resolve_workspace_root
from tests.test_cli.test_first_run_workspace import _json, _run, first_run  # noqa: F401


def _git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)


def _bind(root: Path, instance_id: str) -> None:
    (root / ".cruxible").mkdir(parents=True, exist_ok=True)
    (root / ".cruxible" / "coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "instance_id": instance_id,
                "server_url": f"https://{instance_id}.example.test",
            }
        ),
        encoding="utf-8",
    )


# -- parity: the client context and the core resolver choose the same root ----


def _layouts(tmp_path: Path) -> list[tuple[str, Path, Path]]:
    """(label, start, expected root), built under ``tmp_path``; HOME is ``tmp_path/home``."""

    home = tmp_path / "home"
    outside = tmp_path / "outside"
    cases: list[tuple[str, Path, Path]] = []
    for where, base in (("home", home), ("outside", outside)):
        plain = base / f"plain-{where}" / "a" / "b"
        plain.mkdir(parents=True)
        cases.append((f"{where}: plain folder", plain, plain))

        bound = base / f"bound-{where}"
        _bind(bound, f"inst-{where}")
        (bound / "src").mkdir()
        cases.append((f"{where}: bound folder", bound / "src", bound))

        git_in_cruxible = base / f"git-in-cruxible-{where}"
        _bind(git_in_cruxible, f"inst-gic-{where}")
        repository = git_in_cruxible / "vendor" / "repo"
        (repository / "src" / "pkg").mkdir(parents=True)
        _git(repository)
        cases.append(
            (f"{where}: git inside a cruxible folder", repository / "src" / "pkg", git_in_cruxible)
        )

        cruxible_in_git = base / f"cruxible-in-git-{where}"
        (cruxible_in_git / "docs" / "deep").mkdir(parents=True)
        (cruxible_in_git / "other").mkdir()
        _git(cruxible_in_git)
        (cruxible_in_git / "docs" / ".cruxible").mkdir()
        cases.append(
            (
                f"{where}: cruxible folder inside git",
                cruxible_in_git / "docs" / "deep",
                cruxible_in_git / "docs",
            )
        )
        cases.append(
            (
                f"{where}: git outside the cruxible folder",
                cruxible_in_git / "other",
                cruxible_in_git,
            )
        )

        empty_inner = base / f"empty-inner-{where}"
        _bind(empty_inner, f"inst-outer-{where}")
        (empty_inner / "inner" / ".cruxible").mkdir(parents=True)
        (empty_inner / "inner" / "x").mkdir()
        cases.append(
            (f"{where}: empty inner .cruxible", empty_inner / "inner" / "x", empty_inner / "inner")
        )

        state_marker = base / f"state-marker-{where}"
        _bind(state_marker, f"inst-state-{where}")
        (state_marker / ".cruxible" / "daemon").mkdir()
        (state_marker / "sub").mkdir()
        cases.append(
            (f"{where}: .cruxible holding daemon/", state_marker / "sub", state_marker / "sub")
        )

        link = base / f"link-{where}"
        link.symlink_to(bound, target_is_directory=True)
        cases.append((f"{where}: symlinked path", link / "src", bound))
    return cases


def test_client_context_and_core_resolver_choose_the_same_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    for label, start, expected in _layouts(tmp_path):
        core = resolve_workspace_root(start)
        assert core is not None, label
        client = resolve_context(environ={}, remembered={}, cwd=start, home=home)
        assert core.root == expected.resolve(), label
        assert client.workspace == expected.resolve(), label
        # A binding is read only at the chosen root.
        bound = (expected / ".cruxible" / "coverage.json").is_file() and not (
            expected / ".cruxible" / "daemon"
        ).exists()
        assert client.workspace_attached is bound, label


def test_a_state_root_shaped_cruxible_binds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    folder = tmp_path / "x"
    _bind(folder, "inst_planted")
    (folder / ".cruxible" / "daemon").mkdir()
    (folder / "sub").mkdir()

    resolved = resolve_context(environ={}, remembered={}, cwd=folder / "sub")

    assert resolved.instance_id is None
    assert resolved.workspace_binding_path is None


def test_an_unbound_workspace_never_borrows_another_folders_remembered_instance(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = home / "a"
    _bind(project, "inst_a")
    inner = project / "inner"
    (inner / ".cruxible").mkdir(parents=True)
    plain = home / "plain"
    plain.mkdir(parents=True)
    remembered = {
        "server_socket": "/tmp/daemon.sock",
        "instance_id": "inst_a",
        "instance_transport": f"unix://{Path('/tmp/daemon.sock').resolve()}",
        "instance_workspace": str(project),
    }

    inside = resolve_context(environ={}, remembered=remembered, cwd=inner, home=home)
    assert inside.workspace == inner
    assert inside.instance_id is None
    assert inside.instance_transport_mismatch is not None
    assert "workspace_unbound" in inside.instance_transport_mismatch
    assert "cruxible init" in inside.instance_transport_mismatch

    # No workspace directory at all: the remembered context applies, as before.
    elsewhere = resolve_context(environ={}, remembered=remembered, cwd=plain, home=home)
    assert elsewhere.instance_id == "inst_a"
    # A remembered instance with no recorded folder (a remote host) applies too.
    remote = {key: value for key, value in remembered.items() if key != "instance_workspace"}
    assert resolve_context(environ={}, remembered=remote, cwd=inner, home=home).instance_id == (
        "inst_a"
    )


# -- daemon-backed: two projects, one daemon ----------------------------------


def test_a_checkout_inside_project_b_acts_on_b_as_bobs(
    first_run: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = first_run["folder"]
    _run("init", "--principal-id", "alice")
    instance_a = json.loads((project_a / ".cruxible" / "coverage.json").read_text())["instance_id"]
    # B is a plain folder outside HOME, initialized as bob; A stays remembered.
    project_b = first_run["scratch"] / "outside" / "B"
    project_b.mkdir(parents=True)
    monkeypatch.chdir(project_b)
    _run("init", "--principal-id", "bob")
    instance_b = json.loads((project_b / ".cruxible" / "coverage.json").read_text())["instance_id"]
    assert instance_a != instance_b
    context = json.loads(first_run["context"].read_text())
    assert context["instance_id"] == instance_a

    repository = project_b / "vendor" / "repo"
    (repository / "src" / "pkg").mkdir(parents=True)
    _git(repository)
    monkeypatch.chdir(repository / "src" / "pkg")

    shown = _json("context", "show")
    assert (shown["instance_id"], shown["instance_source"]) == (instance_b, "workspace")
    assert shown["principal_id"] == "bob"
    identity = _json("whoami")
    assert identity["actor_id"] == "bob"


def test_an_empty_inner_cruxible_is_its_own_unbound_workspace(
    first_run: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = first_run["folder"]
    _run("init", "--principal-id", "alice")
    instance_a = json.loads((project / ".cruxible" / "coverage.json").read_text())["instance_id"]
    inner = project / "inner"
    (inner / ".cruxible").mkdir(parents=True)
    monkeypatch.chdir(inner)

    refused = CliRunner().invoke(cli, ["whoami", "--json"])
    assert refused.exit_code == 2, refused.output
    assert "workspace_unbound" in refused.output
    assert instance_a in refused.output

    initialized = _run("init", "--principal-id", "alice")
    assert "already initialized" not in initialized.stdout
    inner_binding = json.loads((inner / ".cruxible" / "coverage.json").read_text())
    assert inner_binding["instance_id"] != instance_a
    assert _json("whoami")["actor_id"] == "alice"


def test_init_workspace_b_from_a_initializes_b_and_leaves_a_alone(
    first_run: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = first_run["folder"]
    _run("init", "--principal-id", "alice")
    instance_a = json.loads((project_a / ".cruxible" / "coverage.json").read_text())["instance_id"]
    context_before = first_run["context"].read_bytes()
    project_b = first_run["scratch"] / "h" / "B"
    project_b.mkdir()
    # Without --principal-id the owner is the OS user, never A's remembered alice.
    monkeypatch.setattr(playbill.getpass, "getuser", lambda: "carol")

    created = _run("init", "--workspace", str(project_b))

    assert "already initialized" not in created.stdout
    binding_b = json.loads((project_b / ".cruxible" / "coverage.json").read_text())
    instance_b = binding_b["instance_id"]
    assert instance_b != instance_a
    assert "Owner principal: carol" in created.stdout
    context = json.loads(first_run["context"].read_text())
    before = json.loads(context_before)
    assert context["instance_id"] == instance_a
    assert (
        context["principals"][f"{instance_a}@{before['instance_transport']}"]
        == (before["principals"][f"{instance_a}@{before['instance_transport']}"])
    )
    assert context["principals"][f"{instance_b}@{before['instance_transport']}"]["active"] == (
        "carol"
    )

    # B already bound: init --workspace B from A reports B, not A.
    again = _run("init", "--workspace", str(project_b))
    assert f"already initialized here: host {instance_b}" in again.stdout
    monkeypatch.chdir(project_b)
    assert _json("whoami")["actor_id"] == "carol"
    monkeypatch.chdir(project_a)
    assert _json("whoami")["actor_id"] == "alice"
