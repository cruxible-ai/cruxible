"""A new project is one bare `cruxible init`, and the CLI then acts as its owner.

With no instance selected, init creates the host and selects it; the owner
principal ID defaults to the OS username and the key directory to a per-user
config path. The context remembers which principal's settings to load, and
`cruxible context use --principal ID` switches. A process that names its own
principal keeps it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.cli.commands import _common, playbill
from cruxible_core.cli.context import (
    CliContextState,
    RememberedPrincipals,
    load_cli_context,
    principal_binding,
    save_cli_context,
)
from cruxible_core.cli.main import cli
from cruxible_core.cli.principal_settings import write_principal_settings
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.config import get_runtime_bearer_token
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry

URL = "http://cruxible-daemon"


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in (
        "CRUXIBLE_SERVER_AUTH",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_PRINCIPAL_ID",
        "CRUXIBLE_PRINCIPAL_KEY",
        "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(playbill.getpass, "getuser", lambda: "Alice")
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    app = create_app()

    def get_client() -> CruxibleClient:
        obj = _common._root_ctx_obj()
        cached = obj.get("_test_client")
        if isinstance(cached, CruxibleClient):
            return cached
        real = CruxibleClient(
            base_url=URL, token=get_runtime_bearer_token(), principal_id=obj.get("principal_id")
        )
        headers = dict(real._client._client.headers)
        real._client = TestClient(app, base_url=URL, headers=headers)  # type: ignore[assignment]
        obj["_test_client"] = real
        return real

    monkeypatch.setattr(_common, "_get_client", get_client)
    yield tmp_path
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, ["--server-url", URL, *args])


def _whoami(*args: str) -> dict[str, Any]:
    result = _run(*args, "whoami", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_a_bare_init_creates_the_host_and_the_cli_acts_as_its_owner(daemon: Path) -> None:
    result = _run("init")

    assert result.exit_code == 0, result.output
    context = load_cli_context()
    instance_id = context.instance_id
    assert instance_id is not None
    assert get_registry().get(instance_id) is not None
    assert "Created Cruxible host" in result.stderr
    key_dir = daemon / "config" / "cruxible" / "keys" / instance_id / "alice"
    assert (key_dir / "alice.ed25519").is_file()
    settings = key_dir / "cruxible.env"
    assert settings.is_file()
    binding = principal_binding(URL, instance_id)
    assert context.principals[binding].active == "alice"
    assert context.principals[binding].settings == {"alice": str(settings.resolve())}
    assert "The CLI now acts as alice" in result.stdout

    # No flag and no environment: the CLI loads the remembered owner itself.
    identity = _whoami()
    assert identity["actor_id"] == "alice"
    assert identity["principal_registration_status"] == "active"
    shown = _run("context", "show", "--json")
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)
    assert payload["principal_id"] == "alice"
    assert payload["principal_source"] == "remembered"


def test_context_use_principal_switches_and_an_explicit_principal_wins(daemon: Path) -> None:
    assert _run("init").exit_code == 0
    instance_id = load_cli_context().instance_id
    assert instance_id is not None
    agent_dir = daemon / "agent-b"
    added = _run(
        "principal",
        "add",
        "agent-b",
        "--key-dir",
        str(agent_dir),
        "--signer-key",
        str(daemon / "config" / "cruxible" / "keys" / instance_id / "alice" / "alice.ed25519"),
    )
    assert added.exit_code == 0, added.output
    # Remembered, not acted as.
    assert load_cli_context().principals[principal_binding(URL, instance_id)].active == "alice"
    assert _whoami()["actor_id"] == "alice"

    switched = _run("context", "use", "--principal", "agent-b")
    assert switched.exit_code == 0, switched.output
    assert "Active principal: agent-b" in switched.output
    assert _whoami()["actor_id"] == "agent-b"
    # A process naming its own principal is never overridden.
    assert _whoami("--principal-id", "alice")["actor_id"] == "alice"

    unknown = _run("context", "use", "--principal", "nobody")
    assert unknown.exit_code != 0
    assert "remembered: agent-b, alice" in unknown.output


def test_a_bare_init_refuses_a_username_that_is_no_principal_id(
    daemon: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playbill.getpass, "getuser", lambda: "9 lives")

    result = _run("init")

    assert result.exit_code != 0
    assert "is not a principal ID" in result.output
    assert "cruxible init --principal-id ID" in result.output
    # The refusal comes before any host is allocated.
    assert load_cli_context().instance_id is None


def test_the_default_key_directory_never_lands_in_the_daemon_state_root(
    daemon: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(daemon / "server-state" / "config"))

    result = _run("init", "--principal-id", "owner")

    assert result.exit_code != 0
    assert "keys stay outside every workspace and the daemon state root" in result.output
    assert "cruxible init --key-dir DIR" in result.output
    # The host was created and selected first, so the retry initializes that
    # same host instead of allocating another.
    created = load_cli_context().instance_id
    assert created is not None
    monkeypatch.setenv("XDG_CONFIG_HOME", str(daemon / "config"))
    retried = _run("init", "--principal-id", "owner")
    assert retried.exit_code == 0, retried.output
    assert "Created Cruxible host" not in retried.stderr
    assert load_cli_context().instance_id == created
    assert _whoami()["actor_id"] == "owner"


# -- F-001: a remembered credential never crosses to another daemon ------------

OTHER = "http://cruxible-daemon-b"
INSTANCE = "inst_same_name"


@pytest.fixture
def remembered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Alice's authenticated settings remembered for INSTANCE on URL; a recording client."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    for name in (
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_PRINCIPAL_ID",
        "CRUXIBLE_PRINCIPAL_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    key_dir = tmp_path / "alice"
    key_dir.mkdir(mode=0o700)
    settings = write_principal_settings(
        key_dir,
        ctx_obj={"server_url": URL},
        instance_id=INSTANCE,
        principal_id="alice",
        private_key_path=key_dir / "alice.ed25519",
        token="synthetic-token-for-daemon-a",
        written_by="test",
    )
    save_cli_context(
        CliContextState(
            server_url=URL,
            instance_id=INSTANCE,
            principals={
                principal_binding(URL, INSTANCE): RememberedPrincipals(
                    active="alice", settings={"alice": str(settings)}
                )
            },
        )
    )
    seen: list[dict[str, Any]] = []

    def record() -> None:
        obj = _common._root_ctx_obj()
        seen.append(
            {
                "url": obj.get("server_url"),
                "token": get_runtime_bearer_token(),
                "principal": obj.get("principal_id"),
            }
        )
        raise click.ClickException("recorded")

    monkeypatch.setattr(_common, "_get_client", record)
    return seen


def test_remembered_settings_load_on_their_own_daemon(remembered: list[dict[str, Any]]) -> None:
    CliRunner().invoke(cli, ["whoami"])
    assert remembered == [
        {"url": URL, "token": "synthetic-token-for-daemon-a", "principal": "alice"}
    ]


def _not_loaded(seen: list[dict[str, Any]], result: Any) -> None:
    assert seen == [{"url": OTHER, "token": None, "principal": None}], result.output
    assert f"remembered for {URL}, not {OTHER}" in result.stderr


def test_an_explicit_endpoint_never_receives_a_remembered_credential(
    remembered: list[dict[str, Any]],
) -> None:
    result = CliRunner().invoke(cli, ["--server-url", OTHER, "--instance-id", INSTANCE, "whoami"])
    _not_loaded(remembered, result)


def test_an_environment_endpoint_never_receives_a_remembered_credential(
    remembered: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", OTHER)
    result = CliRunner().invoke(cli, ["--instance-id", INSTANCE, "whoami"])
    _not_loaded(remembered, result)


def test_a_workspace_endpoint_never_receives_a_remembered_credential(
    remembered: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.authoring.workspace import write_workspace_config

    project = tmp_path / "project"
    project.mkdir()
    write_workspace_config(project, instance_id=INSTANCE, server_url=OTHER)
    monkeypatch.chdir(project)
    shown = CliRunner().invoke(cli, ["context", "show", "--json"])
    assert json.loads(shown.stdout)["transport_source"] == "workspace"
    remembered.clear()  # context show may probe the host registration
    result = CliRunner().invoke(cli, ["whoami"])
    _not_loaded(remembered, result)


def test_a_settings_file_naming_another_daemon_is_not_loaded(
    remembered: list[dict[str, Any]], tmp_path: Path
) -> None:
    settings = tmp_path / "alice" / "cruxible.env"
    settings.write_text(settings.read_text().replace(URL, OTHER))
    result = CliRunner().invoke(cli, ["whoami"])
    assert remembered == [{"url": URL, "token": None, "principal": None}]
    assert "acting as no principal" in result.stderr


# -- F-002: no init custody directory lands in a workspace or the state root -----


@pytest.mark.parametrize("flag", ["--key-dir", "--reviewer-key-dir", "--recovery-key-dir"])
@pytest.mark.parametrize("spelling", ["direct", "symlink", "cased"])
def test_every_init_custody_directory_is_refused_inside_the_state_root(
    daemon: Path, flag: str, spelling: str
) -> None:
    state_root = daemon / "server-state"
    state_root.mkdir(exist_ok=True)
    target = state_root / "keys"
    if spelling == "symlink":
        link = daemon / "innocent-looking"
        link.symlink_to(state_root, target_is_directory=True)
        target = link / "keys"
    elif spelling == "cased":
        target = daemon / "SERVER-STATE" / "keys"
    args = ["init", "--principal-id", "owner", flag, str(target)]

    result = _run(*args)

    assert result.exit_code == 2, result.output
    assert "keys stay outside every workspace and the daemon state root" in result.output
    assert f"cruxible init {flag} DIR" in result.output
    # Refused before a host is allocated or any key is generated.
    assert load_cli_context().instance_id is None
    assert not list(state_root.rglob("*.ed25519"))


def test_an_init_custody_directory_is_refused_inside_the_workspace(
    daemon: Path,
) -> None:
    import subprocess

    project = daemon / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)

    result = _run(
        "init",
        "--principal-id",
        "owner",
        "--workspace",
        str(project),
        "--recovery-key-dir",
        str(project / "recovery-keys"),
    )

    assert result.exit_code == 2, result.output
    assert f"lies inside {project.resolve()}" in result.output
    assert not list(project.rglob("*.ed25519"))


def test_a_bare_init_owner_attests_a_claim_with_no_extra_variables(
    daemon: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advertised workflow: init, then act as the remembered owner, signing locally.

    The settings file names the owner's key as CRUXIBLE_PRINCIPAL_KEY, the one
    variable the Claim attestation signer reads, so nothing else is set.
    """

    from cruxible_core.governance.keys import GeneratedKeyMaterial
    from tests.core_support import _knowledge_loop_support as seeding

    made = _run("init", "--principal-id", "owner", "--reviewer-key-dir", str(daemon / "rev"))
    assert made.exit_code == 0, made.output
    instance_id = load_cli_context().instance_id
    assert instance_id is not None
    instance = get_playbill_manager().get(instance_id)

    def material(principal_id: str, directory: Path) -> GeneratedKeyMaterial:
        return GeneratedKeyMaterial(
            principal=instance._recovered.head.principals.require_active(principal_id),  # noqa: SLF001
            private_key_path=directory / f"{principal_id}.ed25519",
            public_key_path=directory / f"{principal_id}.ed25519.pub",
        )

    # Seed a Claim the fixture way, approving with the reviewer init created.
    from tests.test_authoring import test_authoring_preflight as preflight

    reviewer = material("reviewer", daemon / "rev")
    for module in (seeding, preflight):
        monkeypatch.setattr(module, "client_material", lambda *_args, **_kwargs: reviewer)
    owner_dir = daemon / "config" / "cruxible" / "keys" / instance_id / "owner"
    seeding.seed_claims_into(instance, material("owner", owner_dir))
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        (claim,) = projection.typed.connection.execute(
            "SELECT identity FROM claims ORDER BY identity LIMIT 1"
        ).fetchone()

    result = _run("claim", "attest", str(claim).removeprefix("Claim:"), "--support", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["tag"] == "playbill-claim-attestation-append-result-v1"
    assert len(instance.claim_attestation_evidence_store().events()) == 1
