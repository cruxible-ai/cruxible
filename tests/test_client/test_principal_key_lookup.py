"""Which key signs as principal X: one lookup for the CLI, the SDK and the MCP server.

An explicit key; ``CRUXIBLE_PRINCIPAL_KEY`` unless the process acts as another
principal; the settings remembered for X on this instance and endpoint; the
folder record when it names X; the default key directory. Never another
principal's key (sec N2/N3/N5, coldcli-01, coldmcp-03).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client import AccessProfile
from cruxible_client.authoring.folder_identity import write_folder_identity
from cruxible_client.authoring.principal_settings import principal_key, principal_key_missing
from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.errors import SigningKeyError
from cruxible_core.governance.keys import generate_client_principal_key

INSTANCE = "inst_1"


def _settings(directory: Path, principal: str, socket: Path, instance: str = INSTANCE) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cruxible.env"
    path.write_text(
        f"export CRUXIBLE_SERVER_SOCKET={socket}\n"
        f"export CRUXIBLE_INSTANCE_ID={instance}\n"
        f"export CRUXIBLE_PRINCIPAL_ID={principal}\n"
        f"export CRUXIBLE_PRINCIPAL_KEY={directory / f'{principal}.ed25519'}\n"
    )
    return path


def _remembered(socket: Path, **settings: Path) -> dict[str, object]:
    binding = f"{INSTANCE}@unix://{socket.resolve()}"
    return {
        "principals": {
            binding: {
                "active": "owner",
                "settings": {name: str(path) for name, path in settings.items()},
            }
        }
    }


def _lookup(tmp_path: Path, principal: str, **kwargs: Any) -> Any:
    kwargs.setdefault("environ", {"XDG_CONFIG_HOME": str(tmp_path / "config")})
    kwargs.setdefault("remembered", {})
    return principal_key(
        principal,
        instance_id=INSTANCE,
        server_url=None,
        server_socket=str(tmp_path / "daemon.sock"),
        **kwargs,
    )


def test_the_lookup_order_and_that_it_is_never_another_principals_key(tmp_path: Path) -> None:
    socket = tmp_path / "daemon.sock"
    config = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    owner_env = {
        **config,
        "CRUXIBLE_PRINCIPAL_ID": "owner",
        "CRUXIBLE_PRINCIPAL_KEY": "/keys/owner.ed25519",
    }
    remembered = _remembered(socket, reviewer=_settings(tmp_path / "rev", "reviewer", socket))

    # Nothing names a key: none, and never the owner's that the environment holds.
    assert _lookup(tmp_path, "reviewer") is None
    assert _lookup(tmp_path, "reviewer", environ=owner_env) is None

    default = tmp_path / "config" / "cruxible" / "keys" / INSTANCE / "reviewer"
    default.mkdir(parents=True)
    (default / "reviewer.ed25519").write_text("x")
    found = _lookup(tmp_path, "reviewer", environ=owner_env)
    assert (found.source, found.private_key_path) == (
        "default_key_dir",
        (default / "reviewer.ed25519").resolve(),
    )

    workspace = tmp_path / "project"
    (workspace / ".cruxible").mkdir(parents=True)
    write_folder_identity(
        workspace, instance_id=INSTANCE, principal_id="reviewer", key_dir=tmp_path / "folder-keys"
    )
    found = _lookup(tmp_path, "reviewer", environ=owner_env, folder=workspace)
    assert (found.source, found.private_key_path) == (
        "folder",
        tmp_path / "folder-keys" / "reviewer.ed25519",
    )
    # The record names the reviewer only: it is no key for anyone else.
    assert _lookup(tmp_path, "alice", environ=owner_env, folder=workspace) is None

    found = _lookup(
        tmp_path, "reviewer", environ=owner_env, folder=workspace, remembered=remembered
    )
    assert found.source == "remembered_settings"
    assert found.private_key_path == tmp_path / "rev" / "reviewer.ed25519"
    assert found.settings_path == tmp_path / "rev" / "cruxible.env"

    # The environment's key is the acting principal's, or an unnamed process's own.
    as_reviewer = {**owner_env, "CRUXIBLE_PRINCIPAL_ID": "reviewer"}
    assert _lookup(tmp_path, "reviewer", environ=as_reviewer, remembered=remembered).source == (
        "environment"
    )
    unnamed = {**config, "CRUXIBLE_PRINCIPAL_KEY": "/keys/any.ed25519"}
    assert _lookup(tmp_path, "reviewer", environ=unnamed, remembered=remembered).source == (
        "environment"
    )

    found = _lookup(
        tmp_path, "reviewer", environ=as_reviewer, remembered=remembered, explicit="/k/r.key"
    )
    assert (found.source, found.private_key_path) == ("option", Path("/k/r.key"))


def test_remembered_settings_for_another_instance_or_endpoint_are_not_offered(
    tmp_path: Path,
) -> None:
    socket = tmp_path / "daemon.sock"
    elsewhere = _settings(tmp_path / "rev", "reviewer", tmp_path / "other.sock")
    assert _lookup(tmp_path, "reviewer", remembered=_remembered(socket, reviewer=elsewhere)) is None
    other = _settings(tmp_path / "rev2", "reviewer", socket, instance="inst_2")
    assert _lookup(tmp_path, "reviewer", remembered=_remembered(socket, reviewer=other)) is None
    # A file remembered under one principal's name that names another is not that key.
    owners = _settings(tmp_path / "own", "owner", socket)
    assert _lookup(tmp_path, "reviewer", remembered=_remembered(socket, reviewer=owners)) is None


@pytest.mark.parametrize(
    ("client", "names", "never"),
    [
        ("cli", ("cruxible context use --principal reviewer", "cruxible.env", "--key FILE"), ()),
        ("sdk", ("cx.signer('reviewer', key=FILE)", "cruxible.env"), ("cruxible context use",)),
        (
            "mcp",
            ("CRUXIBLE_PRINCIPAL_KEY", "CRUXIBLE_MCP_KEY_DIR", "cruxible_proposal_approve_prepare"),
            ("cruxible context use", "--key"),
        ),
    ],
)
def test_the_refusal_names_a_repair_of_the_client_that_prints_it(
    client: Any, names: tuple[str, ...], never: tuple[str, ...]
) -> None:
    message = principal_key_missing("reviewer", client=client)

    assert message.startswith("no signing key for principal reviewer here; repair: ")
    assert all(name in message for name in names)
    assert not any(name in message for name in never)


def _cx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *principals: str) -> tuple[Cruxible, Path]:
    keys = tmp_path / "keys"
    socket = tmp_path / "daemon.sock"
    records = []
    remembered: dict[str, Path] = {}
    for principal in principals:
        directory = keys / principal
        records.append(
            generate_client_principal_key(
                directory, principal_id=principal, kind="ordinary", forbidden_roots=()
            ).principal
        )
        remembered[principal] = _settings(directory, principal, socket)
    context = tmp_path / "context.json"
    context.write_text(json.dumps(_remembered(socket, **remembered)))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_KEY", raising=False)
    client = SimpleNamespace(
        base_url=None,
        socket_path=str(socket),
        whoami=lambda instance: SimpleNamespace(actor_id="owner"),
        orient=lambda instance, **_: SimpleNamespace(
            principals=records, truncated=False, next_cursor=None
        ),
    )
    workspace = tmp_path / "project"
    workspace.mkdir()
    cx = Cruxible(
        client=client,  # type: ignore[arg-type]
        instance_id=INSTANCE,
        workspace=workspace,
        access_profile=AccessProfile("test", (), False),
        clock=lambda: None,
    )
    return cx, keys


def test_the_sdk_builds_a_signer_in_one_line_from_the_same_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sec N5: the SDK's next said `signer=<reviewer signer>` and building one took a
    four-argument `LocalEd25519ApprovalSigner.open`."""

    cx, keys = _cx(tmp_path, monkeypatch, "owner", "reviewer")

    reviewer = cx.signer("reviewer")
    assert reviewer.signer_id == "reviewer"
    assert reviewer.private_key_path == (keys / "reviewer" / "reviewer.ed25519").resolve()
    # No argument: the principal this connection acts as.
    assert cx.signer().signer_id == "owner"

    # The owner's key in the environment is never the reviewer's.
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_ID", "owner")
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_KEY", str(keys / "owner" / "owner.ed25519"))
    assert cx.signer("reviewer").private_key_path == reviewer.private_key_path
    # An explicit key that is not the principal's is refused by its public half.
    with pytest.raises(SigningKeyError, match="does not match"):
        cx.signer("reviewer", key=keys / "owner" / "owner.ed25519")


def test_the_sdk_refuses_a_signer_with_no_key_naming_the_sdk_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cx, _keys = _cx(tmp_path, monkeypatch, "owner")

    with pytest.raises(SigningKeyError) as refused:
        cx.signer("reviewer")

    assert "cx.signer('reviewer', key=FILE)" in str(refused.value)
    assert "cruxible context use" not in str(refused.value)
