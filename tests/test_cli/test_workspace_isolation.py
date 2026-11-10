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

from cruxible_client.authoring.context import ContextResolutionError, resolve_context
from cruxible_client.contracts import workspace_root as workspace_root_contract
from cruxible_client.contracts.workspace_root import WorkspaceBoundaryRefused
from cruxible_core.cli.commands import playbill
from cruxible_core.cli.main import cli
from cruxible_core.mcp.daemon import default_socket_path
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


def test_an_unbound_workspace_never_borrows_a_remembered_local_instance(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = home / "a"
    _bind(project, "inst_a")
    inner = project / "inner"
    (inner / ".cruxible").mkdir(parents=True)
    plain = home / "plain"
    plain.mkdir(parents=True)
    # The previous build's context shape: no folder recorded for the instance.
    local = {
        "server_socket": "/tmp/daemon.sock",
        "instance_id": "inst_a",
        "instance_transport": f"unix://{Path('/tmp/daemon.sock').resolve()}",
    }

    inside = resolve_context(environ={}, remembered=local, cwd=inner, home=home)
    assert inside.workspace == inner
    assert inside.instance_id is None
    assert inside.instance_transport_mismatch is not None
    assert "workspace_unbound" in inside.instance_transport_mismatch
    assert "cruxible init" in inside.instance_transport_mismatch

    # No workspace directory at all: the remembered context applies, as before.
    elsewhere = resolve_context(environ={}, remembered=local, cwd=plain, home=home)
    assert elsewhere.instance_id == "inst_a"
    # A remembered remote host applies in the unbound workspace (a local catalog).
    remote = {"server_url": "https://remote.example.test", "instance_id": "inst_remote"}
    in_remote = resolve_context(environ={}, remembered=remote, cwd=inner, home=home)
    assert (in_remote.instance_id, in_remote.instance_source) == ("inst_remote", "remembered")


# -- the invariant, row by row --------------------------------------------------
#
# A remembered instance is used only when discovery found no workspace marker,
# or the remembered instance is remote. Discovery never stops short: it finds a
# trusted marker, refuses at an untrusted one (another user's directory) or at
# one it cannot read, or completes with none.

_REMEMBERED = {
    "none": {},
    "local": {
        "server_socket": "/tmp/daemon.sock",
        "instance_id": "inst_remembered",
        "instance_transport": f"unix://{Path('/tmp/daemon.sock').resolve()}",
    },
    "remote": {"server_url": "https://remote.example.test", "instance_id": "inst_remembered"},
}
_ROWS = [
    (marker, binding, remembered, marker_owner, between)
    for marker, bindings in (
        ("none", ("none",)),
        ("git", ("none",)),
        ("cruxible", ("none", "attached", "invalid")),
    )
    for binding in bindings
    for remembered in _REMEMBERED
    for marker_owner in (("self",) if marker == "none" else ("self", "other"))
    # An other-owned, marker-free folder between the start and the marker.
    for between in ("no", "yes")
]


def _expected(marker: str, binding: str, remembered: str, marker_owner: str) -> str:
    """The outcome the invariant allows; the folder in between never changes it."""

    if marker != "none" and marker_owner == "other":
        return "refused"
    if marker != "cruxible":
        # Discovery completed and found no .cruxible marker.
        return "none" if remembered == "none" else "remembered"
    if binding == "invalid":
        return "refused"
    if binding == "attached":
        return "workspace"
    return {"none": "none", "local": "refused", "remote": "remembered"}[remembered]


def _outcome(**kwargs: object) -> str:
    try:
        resolved = resolve_context(**kwargs)  # type: ignore[arg-type]
    except (ContextResolutionError, WorkspaceBoundaryRefused):
        return "refused"
    if resolved.instance_source in {"workspace", "remembered"}:
        return resolved.instance_source
    mismatch = resolved.instance_transport_mismatch or ""
    return "refused" if mismatch.startswith("workspace_unbound") else "none"


@pytest.mark.parametrize(("marker", "binding", "remembered", "marker_owner", "between"), _ROWS)
def test_the_remembered_instance_invariant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
    binding: str,
    remembered: str,
    marker_owner: str,
    between: str,
) -> None:
    home = tmp_path / "home"
    project = home / "project"
    start = project / "mid" / "sub"
    start.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    if marker == "git":
        _git(project)
    if marker == "cruxible":
        (project / ".cruxible").mkdir()
        if binding == "attached":
            _bind(project, "inst_bound")
        if binding == "invalid":
            (project / ".cruxible" / "coverage.json").write_text("{not json", encoding="utf-8")
    other_owned = set()
    if marker_owner == "other":
        other_owned.add(project.resolve())
    if between == "yes":
        other_owned.add((project / "mid").resolve())
    monkeypatch.setattr(
        workspace_root_contract, "_owned_by_user", lambda path, _status: path not in other_owned
    )

    outcome = _outcome(environ={}, remembered=_REMEMBERED[remembered], cwd=start, home=home)

    assert outcome == _expected(marker, binding, remembered, marker_owner)
    if outcome == "remembered":
        assert marker != "cruxible" or remembered == "remote"


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


# -- review round 2 repros ------------------------------------------------------


def _binding(folder: Path) -> dict[str, str]:
    return json.loads((folder / ".cruxible" / "coverage.json").read_text(encoding="utf-8"))


def test_a_marked_unbound_folder_never_acts_as_the_remembered_local_instance(
    first_run: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The previous build's context shape (nothing records A's folder) still refuses in B."""

    _run("init", "--principal-id", "alice")
    instance_a = _binding(first_run["folder"])["instance_id"]
    context = json.loads(first_run["context"].read_text(encoding="utf-8"))
    assert context["instance_id"] == instance_a
    assert set(context) <= {
        "server_url",
        "server_socket",
        "instance_id",
        "instance_transport",
        "principals",
    }
    project_b = first_run["scratch"] / "outside" / "B"
    (project_b / ".cruxible").mkdir(parents=True)
    monkeypatch.chdir(project_b)

    refused = CliRunner().invoke(cli, ["whoami", "--json"])

    assert refused.exit_code == 2, refused.output
    assert "workspace_unbound" in refused.output
    assert "alice" not in refused.stdout


def test_an_other_owned_workspace_is_refused_by_name_and_used_once_named(
    first_run: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = first_run["folder"]
    _run("init", "--principal-id", "alice")
    project_b = first_run["scratch"] / "h" / "B"
    (project_b / "sub").mkdir(parents=True)
    monkeypatch.chdir(project_b)
    _run("init", "--principal-id", "bob")
    instance_b = _binding(project_b)["instance_id"]
    repository = first_run["scratch"] / "h" / "G"
    (repository / "sub").mkdir(parents=True)
    _git(repository)
    # Model a root-owned mount: B and the Git worktree G belong to another user.
    other_owned = {project_b.resolve(), repository.resolve()}
    monkeypatch.setattr(
        workspace_root_contract, "_owned_by_user", lambda path, _status: path not in other_owned
    )

    def refused_from(folder: Path) -> None:
        monkeypatch.chdir(folder)
        result = CliRunner().invoke(cli, ["whoami", "--json"])
        assert result.exit_code == 2, result.output
        assert "cruxible.workspace.boundary_untrusted" in result.output
        assert "owned by another user" in result.output
        assert "CRUXIBLE_WORKSPACE=" in result.output
        assert "alice" not in result.stdout

    for folder in (project_b, project_b / "sub", repository / "sub"):
        refused_from(folder)
    # Without a remembered A it is the same named refusal, not "--instance-id is required".
    context = json.loads(first_run["context"].read_text(encoding="utf-8"))
    context.pop("instance_id")
    context.pop("instance_transport", None)
    first_run["context"].write_text(json.dumps(context), encoding="utf-8")
    refused_from(project_b)

    # Named explicitly, B is used: its own instance, as bob.
    monkeypatch.setenv("CRUXIBLE_WORKSPACE", str(project_b))
    monkeypatch.chdir(project_b / "sub")
    shown = _json("context", "show")
    assert (shown["instance_id"], shown["instance_source"]) == (instance_b, "workspace")
    assert _json("whoami")["actor_id"] == "bob"
    monkeypatch.delenv("CRUXIBLE_WORKSPACE")
    monkeypatch.chdir(project_a)
    assert _json("whoami")["actor_id"] == "alice"


def test_init_workspace_b_already_bound_from_an_unrelated_folder_with_no_context(
    first_run: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_b = first_run["folder"]
    _run("init", "--principal-id", "bob")
    instance_b = _binding(project_b)["instance_id"]
    coverage = (project_b / ".cruxible" / "coverage.json").read_bytes()
    first_run["context"].unlink()
    unrelated = first_run["scratch"] / "h" / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    socket = str(default_socket_path(first_run["state"]))

    again = _run("--server-socket", socket, "init", "--workspace", str(project_b))

    assert f"already initialized here: host {instance_b}" in again.stdout
    assert "Created Cruxible host" not in again.stderr
    assert (project_b / ".cruxible" / "coverage.json").read_bytes() == coverage
    assert not first_run["context"].exists()


def test_an_other_owned_folder_inside_b_never_hands_commands_to_a(
    first_run: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3: a root-created B/mid has no marker, so it never ends the walk."""

    _run("init", "--principal-id", "alice")
    project_b = first_run["scratch"] / "outside" / "B"
    (project_b / "mid" / "sub").mkdir(parents=True)
    monkeypatch.chdir(project_b)
    _run("init", "--principal-id", "bob")
    instance_b = _binding(project_b)["instance_id"]
    other_owned = {(project_b / "mid").resolve()}
    monkeypatch.setattr(
        workspace_root_contract, "_owned_by_user", lambda path, _status: path not in other_owned
    )
    monkeypatch.chdir(project_b / "mid" / "sub")

    shown = _json("context", "show")
    assert (shown["instance_id"], shown["instance_source"]) == (instance_b, "workspace")
    assert _json("whoami")["actor_id"] == "bob"
