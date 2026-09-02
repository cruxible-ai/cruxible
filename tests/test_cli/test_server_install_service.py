"""Service render/install contract for unattended daemon startup."""

from __future__ import annotations

import plistlib
from pathlib import Path

from click.testing import CliRunner

from cruxible_core.cli.main import cli
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

    linux = darwin.model_copy(update={"platform": "linux"})
    unit = render_systemd_service(linux).decode("utf-8")
    assert f"ExecStart={linux.executable} server start" in unit
    assert "--capability-ceiling governed_write" in unit
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
    assert refused.exit_code == 2
    assert "cruxible server start --bootstrap-secret-file PATH" in refused.output


def test_install_records_then_enables_without_starting(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path, "linux")
    destination = tmp_path / "units" / "cruxible.service"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "cruxible_core.server.service_install.service_destination",
        lambda _platform: destination,
    )
    monkeypatch.setattr(
        "cruxible_core.server.service_install.subprocess.run",
        lambda command, **_kwargs: calls.append(command),
    )

    assert install_service(config) == destination
    assert load_service_config(Path(config.state_root)) == config
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "cruxible.service"],
    ]
    assert not any("start" in command for command in calls)
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
    assert f"ExecStart={config.executable} server start" in result.output
