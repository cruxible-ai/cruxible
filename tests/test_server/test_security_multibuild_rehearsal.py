"""Opt-in rehearsal with the actual security 1.0.1/1.0.2 release sources and wheels.

The release artifacts must be rebuilt for compiler 34 (their old pins name legacy
Provider identities). Only copies under the designated scratch root are built.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not os.environ.get("CRUXIBLE_SECURITY_REHEARSAL_ROOT"),
    reason="requires the real security release checkouts",
)
def test_real_security_101_to_102_is_one_kit_add() -> None:
    scratch = Path(os.environ["CRUXIBLE_SECURITY_REHEARSAL_ROOT"])
    old = Path(os.environ["CRUXIBLE_SECURITY_101"])
    new = Path(os.environ["CRUXIBLE_SECURITY_102"])
    root = Path(tempfile.mkdtemp(prefix="security-", dir=scratch))
    binary = root / "cruxible"
    binary.write_text(
        f"#!/bin/bash\nexec {sys.executable} -c "
        '"from cruxible_core.cli.main import cli; cli()" "$@"\n'
    )
    binary.chmod(0o700)
    env = {key: value for key, value in os.environ.items() if not key.startswith("CRUXIBLE_")}
    env["CRUXIBLE_BIN"] = str(binary)
    env["UV_CACHE_DIR"] = str(scratch / "uv-cache")
    releases = []
    for version, source in (("1.0.1", old), ("1.0.2", new)):
        kit = root / f"kit-{version}"
        shutil.copytree(source, kit, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
        command = [
            sys.executable,
            str(kit / "source/build_release.py"),
            str(root / f"build-{version}"),
        ]
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=240)
        (root / f"build-{version}.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, f"{root}: {result.stdout[-4000:]}{result.stderr[-4000:]}"
        releases.append(kit)

    home, state, workspace = (root / name for name in ("home", "state", "workspace"))
    for path in (home, state, workspace):
        path.mkdir()
    socket_dir = Path(tempfile.mkdtemp(prefix="mb-", dir=os.environ.get("TMPDIR")))
    socket_dir.chmod(0o700)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        CRUXIBLE_STATE_ROOT=str(state),
        CRUXIBLE_SERVER_SOCKET=str(socket_dir / "d.sock"),
        CRUXIBLE_CLIENT_TIMEOUT_S="240",
        GIT_AUTHOR_NAME="multibuild",
        GIT_AUTHOR_EMAIL="multibuild@example.invalid",
        GIT_COMMITTER_NAME="multibuild",
        GIT_COMMITTER_EMAIL="multibuild@example.invalid",
    )
    log = (root / "rehearsal.log").open("w")

    def cx(*arguments: str) -> str:
        result = subprocess.run(
            [str(binary), *arguments],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
        log.write(f"$ cruxible {' '.join(arguments)}\n{result.stdout}{result.stderr}\n")
        log.flush()
        assert result.returncode == 0, f"{root}: {result.stdout[-4000:]}{result.stderr[-4000:]}"
        return result.stdout

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=workspace, env=env, check=True
    )
    daemon = subprocess.Popen(
        [
            str(binary),
            "server",
            "start",
            "--socket",
            env["CRUXIBLE_SERVER_SOCKET"],
            "--state-root",
            str(state),
        ],
        env=env,
        cwd=root,
        stdout=(root / "daemon.log").open("w"),
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            if Path(env["CRUXIBLE_SERVER_SOCKET"]).exists():
                break
            time.sleep(0.1)
        cx("init", "--principal-id", "multibuild-owner")
        (settings,) = (home / ".config/cruxible/keys").glob("*/*/cruxible.env")
        for line in settings.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                env[key.removeprefix("export ").strip()] = value.strip().strip("'\"")
        first = json.loads(cx("kit", "add", str(releases[0]), "--commit", "--json"))
        assert first["status"] == "accepted", first
        because = "Analyst review: only packaged templates are compiled."
        subject = {
            "subject_kind": "security.assessment",
            "key": {"release_id": "app--1.0.0", "cve_id": "CVE-2024-56201"},
        }

        def claim(predicate: str, value: str) -> dict:
            return {
                "kind": "claim",
                "subject": subject,
                "predicate": predicate,
                "object": {"kind": "literal", "value": value},
                "role": "normative",
                "rationale": because,
                "source": {"kind": "self_source", "body": because + "\n"},
            }

        payload = root / "v101-operator.json"
        payload.write_text(
            json.dumps(
                {
                    "kind": "change_set",
                    "rationale": "Exercise the 1.0.1 operator state during upgrade.",
                    "members": [
                        {
                            "kind": "subject",
                            "subject_kind": subject["subject_kind"],
                            "key": subject["key"],
                            "rationale": because,
                        },
                        claim("security.assessment.status", "not_affected"),
                        claim(
                            "security.assessment.justification",
                            "vulnerable_code_not_in_execute_path",
                        ),
                        claim(
                            "security.assessment.judgment",
                            "The filename never reaches the template compiler.",
                        ),
                        {
                            "kind": "blueprint_instance",
                            "name": "user.osv-applicability",
                            "blueprint": "security.osv-applicability",
                            "bindings": {"osv": "security-sources-provider"},
                            "acquisition_policy": "security.acquisition",
                        },
                    ],
                }
            )
        )
        cx("authoring", "submit", str(payload), "--and-activate")
        preview = json.loads(
            cx(
                "kit",
                "add",
                str(releases[1]),
                "--retire-dependents",
                "ClaimType:security.assessment.judgment",
                "--json",
            )
        )
        assert preview["status"] == "would_propose", preview
        upgraded = json.loads(
            cx(
                "kit",
                "add",
                str(releases[1]),
                "--retire-dependents",
                "ClaimType:security.assessment.judgment",
                "--commit",
                "--at",
                preview["coordinate"]["git_oid"],
                "--json",
            )
        )
        assert upgraded["status"] == "accepted", upgraded
        assert any(
            item["identity"] == "Procedure:user.osv-applicability" and item["action"] == "successor"
            for item in upgraded["repins"]
        )
        dropped = next(
            item
            for item in upgraded["plan"]
            if item["identity"] == "ClaimType:security.assessment.judgment"
        )
        assert dropped["action"] == "retire" and dropped["dependent_count"] == 1, dropped
        status = json.loads(cx("kit", "status", "--offline", "--json"))
        assert (
            next(kit for kit in status["kits"] if kit["kit_id"] == "security")["version"] == "1.0.2"
        )
        (root / "result.json").write_text(
            json.dumps({"preview": preview, "upgrade": upgraded, "status": status}, indent=2)
        )
    finally:
        try:
            cx("server", "stop")
        finally:
            try:
                daemon.wait(timeout=20)
            except subprocess.TimeoutExpired:
                daemon.terminate()
                daemon.wait(timeout=10)
            shutil.rmtree(socket_dir)
            log.close()
