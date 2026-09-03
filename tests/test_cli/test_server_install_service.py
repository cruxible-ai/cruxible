"""Service render/install contract for unattended daemon startup."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from cruxible_core.cli.main import cli
from cruxible_core.errors import ConfigError
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.credentials import RuntimeCredentialStore
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.server.service_install import (
    SERVICE_CONFIG_NAME,
    ServiceInstallConfigV1,
    install_service,
    load_service_config,
    render_launchd_service,
    render_systemd_service,
    service_destination,
)


def _config(tmp_path: Path, platform: str) -> ServiceInstallConfigV1:
    executable = tmp_path / "bin" / "cruxible"
    executable.parent.mkdir()
    executable.write_text("executable", encoding="utf-8")
    executable.chmod(0o700)
    return ServiceInstallConfigV1(
        platform=platform,
        executable=str(executable),
        state_root=str(tmp_path / "state"),
        socket_path=str(tmp_path / "daemon.sock"),
        capability_ceiling="governed_write",
        auth_enabled=False,
    )


def test_launchd_and_systemd_render_exact_start_flags_without_secrets(tmp_path: Path) -> None:
    darwin = _config(tmp_path, "darwin")
    plist = plistlib.loads(render_launchd_service(darwin))
    arguments = plist["ProgramArguments"]
    assert arguments[:3] == [darwin.executable, "server", "start"]
    assert arguments[arguments.index("--state-root") + 1] == darwin.state_root
    assert arguments[arguments.index("--socket") + 1] == darwin.socket_path
    assert plist["RunAtLoad"] is False
    assert plist["KeepAlive"] is False

    linux = darwin.model_copy(update={"platform": "linux"})
    unit = render_systemd_service(linux).decode("utf-8")
    assert f'ExecStart="{linux.executable}" "server" "start"' in unit
    assert '"--capability-ceiling" "governed_write"' in unit
    assert "Restart=on-failure" in unit
    for forbidden in ("bearer", "bootstrap", "password", "secret", "token"):
        assert forbidden.encode() not in (render_launchd_service(darwin) + unit.encode()).lower()


def test_service_destinations_are_canonical_user_paths(tmp_path: Path) -> None:
    assert service_destination("darwin", home=tmp_path) == (
        tmp_path / "Library" / "LaunchAgents" / "ai.cruxible.daemon.plist"
    )
    assert service_destination("linux", home=tmp_path) == (
        tmp_path / ".config" / "systemd" / "user" / "cruxible.service"
    )


def test_print_is_pure_and_auth_without_durable_credential_refuses(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path, "linux")
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.current_service_platform", lambda: "linux"
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.resolved_cruxible_executable",
        lambda: Path(config.executable),
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.install_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("install called")),
    )

    printed = CliRunner().invoke(
        cli,
        [
            "server",
            "install-service",
            "--state-root",
            config.state_root,
            "--socket",
            config.socket_path,
            "--print",
        ],
    )
    assert printed.exit_code == 0, printed.output
    assert "ExecStart=" in printed.output
    assert not (Path(config.state_root) / "daemon" / "service-install-v1.json").exists()

    refused = CliRunner().invoke(
        cli,
        [
            "server",
            "install-service",
            "--state-root",
            config.state_root,
            "--socket",
            config.socket_path,
            "--auth",
            "--print",
        ],
    )
    assert refused.exit_code == 1
    assert "cruxible server start --bootstrap-secret-file PATH" in refused.output


@pytest.mark.parametrize(
    ("platform", "expected_calls"),
    (
        (
            "darwin",
            lambda destination: [["launchctl", "load", "-w", str(destination)]],
        ),
        (
            "linux",
            lambda destination: [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", destination.name],
            ],
        ),
    ),
)
def test_install_records_then_enables_without_starting(
    tmp_path: Path,
    monkeypatch,
    platform: str,
    expected_calls,
) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path, platform)
    monkeypatch.setenv("HOME", str(tmp_path))
    destination = service_destination(platform)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "cruxible_core.server.service_install.subprocess.run",
        lambda command, **_kwargs: calls.append(command),
    )

    assert install_service(config) == destination
    assert load_service_config(Path(config.state_root)) == config
    assert calls == expected_calls(destination)
    assert not any("start" in command for command in calls)
    if platform == "darwin":
        plist = plistlib.loads(destination.read_bytes())
        assert plist["RunAtLoad"] is False
        assert plist["KeepAlive"] is False
    record = Path(config.state_root) / "daemon" / SERVICE_CONFIG_NAME
    for forbidden in ("bearer", "bootstrap", "password", "secret", "token"):
        assert forbidden.encode() not in record.read_bytes().lower()


def test_print_reuses_and_revalidates_recorded_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path, "linux")
    record = Path(config.state_root) / "daemon" / SERVICE_CONFIG_NAME
    record.parent.mkdir(parents=True)
    record.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.current_service_platform", lambda: "linux"
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.resolved_cruxible_executable",
        lambda: (_ for _ in ()).throw(AssertionError("recorded executable was replaced")),
    )

    result = CliRunner().invoke(
        cli,
        ["server", "install-service", "--state-root", config.state_root, "--print"],
    )

    assert result.exit_code == 0, result.output
    assert f'ExecStart="{config.executable}" "server" "start"' in result.output


@pytest.mark.parametrize(
    "hostile",
    (
        "socket\nExecStartPre=/bin/echo pwned",
        "socket\rEnvironment=INJECTED=true",
        "=unit-directive",
    ),
)
def test_service_config_refuses_unit_control_injection(
    tmp_path: Path,
    hostile: str,
) -> None:
    values = _config(tmp_path, "linux").model_dump()
    values["socket_path"] = hostile

    with pytest.raises(ValidationError, match="control characters|must not start"):
        ServiceInstallConfigV1.model_validate(values)


def test_systemd_escapes_percent_specifiers_in_argv(tmp_path: Path) -> None:
    config = _config(tmp_path, "linux").model_copy(
        update={"socket_path": str(tmp_path / "%h.sock")}
    )

    unit = render_systemd_service(config).decode("utf-8")

    assert "%%h.sock" in unit


def _latch_auth_required(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(root))
    reset_registry()
    get_registry().create_governed_instance_with_id("inst_auth")
    RuntimeCredentialStore(root / "daemon" / "runtime_credentials.db").create_credential(
        instance_id="inst_auth",
        label="service",
        permission_mode=PermissionMode.ADMIN,
    )


def test_existing_or_dangling_link_unit_requires_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "cruxible_core.server.service_install.subprocess.run",
        lambda *_args, **_kwargs: None,
    )
    destination = service_destination("linux")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing-unit")

    with pytest.raises(ConfigError, match="--replace"):
        install_service(config)
    assert destination.read_bytes() == b"existing-unit"
    assert not (Path(config.state_root) / "daemon" / SERVICE_CONFIG_NAME).exists()

    install_service(config, replace=True)
    assert destination.read_bytes() != b"existing-unit"
    destination.unlink()
    destination.symlink_to(tmp_path / "missing-unit")
    with pytest.raises(ConfigError, match="--replace"):
        install_service(config)
    assert destination.is_symlink()


@pytest.mark.parametrize("auth_required", (False, True))
def test_install_service_defaults_to_the_state_root_auth_latch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_required: bool,
) -> None:
    config = _config(tmp_path, "linux")
    root = Path(config.state_root)
    if auth_required:
        _latch_auth_required(root, monkeypatch)
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.current_service_platform", lambda: "linux"
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.resolved_cruxible_executable",
        lambda: Path(config.executable),
    )

    result = CliRunner().invoke(
        cli,
        [
            "server",
            "install-service",
            "--state-root",
            str(root),
            "--socket",
            str(config.socket_path),
            "--print",
        ],
    )

    assert result.exit_code == 0, result.output
    expected = "true" if auth_required else "false"
    assert f"CRUXIBLE_SERVER_AUTH={expected}" in result.output
    reset_registry()


@pytest.mark.parametrize("auth_required", (False, True))
def test_install_service_refuses_an_explicit_auth_latch_disagreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_required: bool,
) -> None:
    config = _config(tmp_path, "linux")
    root = Path(config.state_root)
    if auth_required:
        _latch_auth_required(root, monkeypatch)
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.current_service_platform", lambda: "linux"
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.server.resolved_cruxible_executable",
        lambda: Path(config.executable),
    )
    flag = "--no-auth" if auth_required else "--auth"

    result = CliRunner().invoke(
        cli,
        [
            "server",
            "install-service",
            "--state-root",
            str(root),
            "--socket",
            str(config.socket_path),
            flag,
            "--print",
        ],
    )

    assert result.exit_code != 0
    assert "service_install.auth_posture_mismatch" in result.output
    assert "repair:" in result.output
    reset_registry()
