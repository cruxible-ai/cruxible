"""A runnable local fixture only; this is not an audit of a real target."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .adapters import ScriptedAdapter
from .config import BenchmarkConfig
from .runner import run_benchmark

APP = "import yaml\n\n\ndef load(text):\n    return yaml.load(text)  # reachable\n"
REQUIREMENTS = "pyyaml==5.3\n"
FIX = (
    "--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1,2 +1,2 @@\n"
    "-pyyaml==5.3\n+pyyaml==5.4\n requests==2.19.0\n"
)
NEW_DEPENDENCY = (
    "--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1,2 @@\n"
    " pyyaml==5.3\n+requests==2.19.0\n"
)
KEV = '{"vulnerabilities": [{"cveID": "CVE-2020-14343", "dateAdded": "2026-01-01"}]}\n'


def write(path: str, text: str) -> list[str]:
    """A deterministic change command: write one file (no implicit shell)."""
    code = (
        "import sys; from pathlib import Path; p = Path(sys.argv[1]); "
        "p.parent.mkdir(parents=True, exist_ok=True); p.write_text(sys.argv[2])"
    )
    return [sys.executable, "-c", code, path, text]


def create_fixture(tmp_path: Path) -> BenchmarkConfig:
    target = tmp_path / "target"
    target.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=target, env=env).decode().strip()

    git("init", "--quiet")
    (target / "app.py").write_text(APP)
    (target / "requirements.txt").write_text(REQUIREMENTS)
    # Host-agent instructions in the target are stripped identically for every arm.
    (target / "CLAUDE.md").write_text("Project instructions no arm may inherit\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "Fixture base",
    )
    # Fixture setup mirrors the real shape without installing anything real:
    # A installs its skill into the arm's tooling directory (a live adapter
    # copies it into each fresh client config); B starts a helper (stand-in for
    # the scratch daemon), provisions approval keys and adopts the kit by agent.
    install_skill = write("{tooling}/skills/fixture-audit/SKILL.md", "fixture skill\n")
    helper = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(600)"
    arms: list[dict[str, object]] = [
        {
            "id": "A",
            "method": "cloudflare-skill",
            "skill": {"name": "cloudflare", "version": "fixture"},
            "memory_paths": ["findings.json"],
            "setup": [
                {"kind": "command", "description": "Install the skill", "argv": install_skill}
            ],
        },
        {
            "id": "B",
            "method": "cruxible-security-kit",
            "plugin": {"name": "shipped-cruxible-plugin", "version": "fixture"},
            "kit": {"name": "security", "version": "fixture"},
            "memory_paths": [".cruxible"],
            "cruxible": {"bin_dir": "{tooling}/bin"},
            "environment": {"CRUXIBLE_MCP_KEY_DIR": "{keys}/mcp"},
            "setup": [
                {
                    "kind": "command",
                    "description": "Start the fixture helper",
                    "argv": ["{python}", "-c", helper, "{run_root}/helper.ready"],
                    "background": True,
                    "wait_for": "{run_root}/helper.ready",
                    "timeout_seconds": 30,
                },
                {
                    "kind": "command",
                    "description": "Provision owner and reviewer approval keys",
                    "argv": [
                        "{python}",
                        "-c",
                        "import sys; from pathlib import Path; d = Path(sys.argv[1]); "
                        "d.mkdir(mode=0o700); [(d / f'{p}.ed25519').write_text(p) "
                        "for p in ('owner', 'reviewer')]",
                        "{keys}/mcp",
                    ],
                },
                {
                    "kind": "agent",
                    "description": "Adopt the kit and its automation template",
                    "prompt": "Adopt the security kit and its automation template.",
                },
            ],
        },
        {
            "id": "C",
            "method": "cruxible-no-kit",
            "plugin": {"name": "shipped-cruxible-plugin", "version": "fixture"},
            "memory_paths": [".cruxible"],
            "cruxible": {"mcp_tools": ["cruxible_orient", "cruxible_query"]},
            "setup": [
                {
                    "kind": "agent",
                    "description": "Initialize an empty instance",
                    "prompt": "Initialize Cruxible for this repository.",
                }
            ],
        },
    ]
    return BenchmarkConfig.model_validate(
        {
            "name": "fixture",
            "target": {"git_url": str(target), "base_commit": git("rev-parse", "HEAD")},
            "model": "scripted-fixture",
            "guard": {"max_seconds_per_invocation": 60, "max_cost_usd_per_invocation": 1.0},
            "repetitions": 2,
            "arms": arms,
            "sessions": [
                {"id": "baseline", "description": "Inspect the base", "changes": []},
                {
                    "id": "new-dependency",
                    "description": "A new dependency was added",
                    "changes": [
                        {"kind": "patch", "description": "Add requests", "text": NEW_DEPENDENCY}
                    ],
                },
                {
                    "id": "fix",
                    "description": "pyyaml was upgraded",
                    "changes": [{"kind": "patch", "description": "Bump pyyaml", "text": FIX}],
                },
                {
                    "id": "revert",
                    "description": "The upgrade was reverted",
                    "changes": [
                        {
                            "kind": "command",
                            "description": "Restore pyyaml 5.3",
                            "argv": write("requirements.txt", "pyyaml==5.3\nrequests==2.19.0\n"),
                        }
                    ],
                },
                {
                    "id": "refactor",
                    "description": "The loader now uses safe_load",
                    "changes": [
                        {
                            "kind": "command",
                            "description": "Remove the reachable unsafe load",
                            "argv": write("app.py", APP.replace("yaml.load", "yaml.safe_load")),
                        }
                    ],
                },
                {
                    "id": "intel-update",
                    "description": "Threat intelligence was updated",
                    "changes": [
                        {
                            "kind": "feed",
                            "description": "KEV lists the pyyaml CVE",
                            "name": "kev.json",
                            "text": KEV,
                        }
                    ],
                },
            ],
            "questions": [
                {
                    "id": "control",
                    "kind": "control",
                    "prompt": "Which pyyaml version does requirements.txt declare? Cite the line.",
                    "after_sessions": [1, 3, 4, 6],
                    "reviewer_context_by_session": {1: "5.3", 3: "5.4", 4: "5.3", 6: "5.3"},
                },
                {
                    "id": "contradiction",
                    "kind": "contradiction",
                    "prompt": "What is now contradicted, and what depends on it?",
                    "after_sessions": [4, 5, 6],
                    "reviewer_context_by_session": {
                        4: "The fix was reverted: the pyyaml finding is live again.",
                        5: "safe_load removed the reachable path.",
                    },
                },
            ],
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="New scratch directory")
    args = parser.parse_args(argv)
    if any(part.startswith(".cruxible") for part in args.root.absolute().parts):
        parser.error("choose a fresh scratch directory outside .cruxible state")
    args.root.mkdir(parents=True)
    config = create_fixture(args.root.resolve())
    (args.root / "fixture.json").write_text(config.model_dump_json(indent=2) + "\n")
    run_benchmark(config, output=args.root / "results", adapter=ScriptedAdapter())
    print(f"Wrote {args.root / 'results'}: synthetic no-op run, no model calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
