"""Smoke tests for the hosted runtime container image."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DOCKERFILE = REPO_ROOT / "deploy" / "runtime" / "Dockerfile"
RUNTIME_STATE_DIR = "/var/lib/cruxible/server"
BOOTSTRAP_ENV = "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=bootstrap-secret"
pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def runtime_image() -> Iterator[str]:
    _require_docker()
    suffix = uuid.uuid4().hex[:12]
    image_tag = f"cruxible-core-runtime:test-{suffix}"

    try:
        _docker(
            [
                "build",
                "-f",
                str(RUNTIME_DOCKERFILE),
                "-t",
                image_tag,
                str(REPO_ROOT),
            ],
            timeout=600,
        )
        yield image_tag
    finally:
        _docker(["image", "rm", "-f", image_tag], check=False, timeout=60)


def test_hosted_runtime_image_builds_starts_and_runs_non_root(
    runtime_image: str,
    tmp_path: Path,
) -> None:
    container_name = _container_name()
    host_port = _free_port()
    state_dir = _create_state_dir(tmp_path)

    try:
        _docker(
            [
                "run",
                "-d",
                "--name",
                container_name,
                "-e",
                BOOTSTRAP_ENV,
                "-v",
                f"{state_dir}:{RUNTIME_STATE_DIR}",
                "-p",
                f"127.0.0.1:{host_port}:8100",
                runtime_image,
            ],
            timeout=60,
        )

        assert _wait_for_health(host_port, container_name) == {
            "status": "ok",
            "capability_ceiling": "admin",
        }
        assert _container_config_user(container_name) == "cruxible"
        assert _container_uid(container_name) != "0"
    finally:
        _docker(["rm", "-f", container_name], check=False, timeout=30)


def test_hosted_runtime_image_requires_external_state_mount(runtime_image: str) -> None:
    completed = _docker(
        [
            "run",
            "--rm",
            "-e",
            BOOTSTRAP_ENV,
            runtime_image,
        ],
        check=False,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "external state mount required" in _combined_output(completed)


def test_hosted_runtime_image_requires_writable_state_mount(
    runtime_image: str,
    tmp_path: Path,
) -> None:
    state_dir = _create_state_dir(tmp_path)
    completed = _docker(
        [
            "run",
            "--rm",
            "-e",
            BOOTSTRAP_ENV,
            "-v",
            f"{state_dir}:{RUNTIME_STATE_DIR}:ro",
            runtime_image,
        ],
        check=False,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "not writable" in _combined_output(completed)


def test_hosted_runtime_state_mount_survives_container_replacement(
    runtime_image: str,
    tmp_path: Path,
) -> None:
    first_container = _container_name()
    second_container = _container_name()
    state_dir = _create_state_dir(tmp_path)
    sentinel = state_dir / "replacement-check.txt"

    try:
        _start_runtime_container(
            image_tag=runtime_image,
            container_name=first_container,
            state_dir=state_dir,
            host_port=_free_port(),
        )
        _docker(
            [
                "exec",
                first_container,
                "python",
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path('{RUNTIME_STATE_DIR}/replacement-check.txt').write_text('persisted')"
                ),
            ],
            timeout=30,
        )
        assert sentinel.read_text() == "persisted"

        _docker(["rm", "-f", first_container], check=False, timeout=30)

        second_port = _free_port()
        _start_runtime_container(
            image_tag=runtime_image,
            container_name=second_container,
            state_dir=state_dir,
            host_port=second_port,
        )
        assert _wait_for_health(second_port, second_container) == {
            "status": "ok",
            "capability_ceiling": "admin",
        }
        read_sentinel = (
            "from pathlib import Path; "
            f"print(Path('{RUNTIME_STATE_DIR}/replacement-check.txt').read_text())"
        )
        completed = _docker(
            [
                "exec",
                second_container,
                "python",
                "-c",
                read_sentinel,
            ],
            timeout=30,
        )
        assert completed.stdout.strip() == "persisted"
    finally:
        _docker(["rm", "-f", first_container], check=False, timeout=30)
        _docker(["rm", "-f", second_container], check=False, timeout=30)


def test_hosted_runtime_private_network_has_no_published_ports(
    runtime_image: str,
    tmp_path: Path,
) -> None:
    runtime_container = _container_name()
    network_name = f"cruxible-runtime-net-{uuid.uuid4().hex[:12]}"
    state_dir = _create_state_dir(tmp_path)

    try:
        _docker(["network", "create", network_name], timeout=30)
        _docker(
            [
                "run",
                "-d",
                "--name",
                runtime_container,
                "--network",
                network_name,
                "--network-alias",
                "runtime",
                "-e",
                BOOTSTRAP_ENV,
                "-v",
                f"{state_dir}:{RUNTIME_STATE_DIR}",
                runtime_image,
            ],
            timeout=60,
        )

        assert _container_port_bindings(runtime_container) == {}
        assert _container_private_ports(runtime_container).get("8100/tcp") is None
        assert _probe_runtime_health(network_name) == {
            "status": "ok",
            "capability_ceiling": "admin",
        }
    finally:
        _docker(["rm", "-f", runtime_container], check=False, timeout=30)
        _docker(["network", "rm", network_name], check=False, timeout=30)


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not available")
    completed = _docker(["info"], check=False, timeout=30)
    if completed.returncode != 0:
        pytest.skip("docker daemon is not available")


def _docker(
    args: list[str],
    *,
    check: bool = True,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    completed = subprocess.run(
        ["docker", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            "docker command failed\n"
            f"command: docker {' '.join(args)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def _start_runtime_container(
    *,
    image_tag: str,
    container_name: str,
    state_dir: Path,
    host_port: int,
) -> None:
    _docker(
        [
            "run",
            "-d",
            "--name",
            container_name,
            "-e",
            BOOTSTRAP_ENV,
            "-v",
            f"{state_dir}:{RUNTIME_STATE_DIR}",
            "-p",
            f"127.0.0.1:{host_port}:8100",
            image_tag,
        ],
        timeout=60,
    )
    assert _wait_for_health(host_port, container_name) == {
        "status": "ok",
        "capability_ceiling": "admin",
    }


def _wait_for_health(host_port: int, container_name: str) -> dict[str, str]:
    deadline = time.monotonic() + 60
    url = f"http://127.0.0.1:{host_port}/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict):
                return {str(key): str(value) for key, value in payload.items()}
        except (OSError, URLError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.5)
    logs = _docker(["logs", container_name], check=False, timeout=30)
    raise AssertionError(
        "runtime image health check did not pass\n"
        f"last error: {last_error}\n"
        f"container stdout:\n{logs.stdout}\n"
        f"container stderr:\n{logs.stderr}"
    )


def _container_config_user(container_name: str) -> str:
    completed = _docker(
        ["inspect", "--format", "{{.Config.User}}", container_name],
        timeout=30,
    )
    return completed.stdout.strip()


def _container_uid(container_name: str) -> str:
    completed = _docker(["exec", container_name, "id", "-u"], timeout=30)
    return completed.stdout.strip()


def _container_port_bindings(container_name: str) -> dict[str, object]:
    completed = _docker(
        ["inspect", "--format", "{{json .HostConfig.PortBindings}}", container_name],
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _container_private_ports(container_name: str) -> dict[str, object]:
    completed = _docker(
        ["inspect", "--format", "{{json .NetworkSettings.Ports}}", container_name],
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _probe_runtime_health(network_name: str) -> dict[str, str]:
    probe_script = """
import json
import time
from urllib.request import urlopen

deadline = time.monotonic() + 60
last_error = None
while time.monotonic() < deadline:
    try:
        with urlopen("http://runtime:8100/health", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        print(json.dumps(payload))
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(0.5)
raise SystemExit(f"runtime private health check failed: {last_error}")
""".strip()
    completed = _docker(
        [
            "run",
            "--rm",
            "--network",
            network_name,
            "python:3.11-slim",
            "python",
            "-c",
            probe_script,
        ],
        timeout=90,
    )
    payload = json.loads(completed.stdout)
    return {str(key): str(value) for key, value in payload.items()}


def _create_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / f"server-state-{uuid.uuid4().hex[:8]}"
    state_dir.mkdir()
    state_dir.chmod(0o777)
    return state_dir


def _container_name() -> str:
    return f"cruxible-runtime-test-{uuid.uuid4().hex[:12]}"


def _combined_output(completed: subprocess.CompletedProcess[str]) -> str:
    return f"{completed.stdout}\n{completed.stderr}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
