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

BASE = 'danger = True\nversion = "1.0"\n'
FIX = (
    "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
    '-danger = True\n+danger = False\n version = "1.0"\n'
)


def create_fixture(tmp_path: Path) -> BenchmarkConfig:
    target = tmp_path / "target"
    target.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=target, env=env).decode().strip()

    git("init", "--quiet")
    (target / "app.py").write_text(BASE)
    (target / "requirements.txt").write_text("example==1.0\n")
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
    arm_a: dict[str, object] = {
        "id": "A",
        "method": "cloudflare-skill",
        "skill": {"name": "cloudflare", "version": "fixture"},
        "memory_paths": ["findings.json"],
    }
    arms: list[dict[str, object]] = [arm_a]
    for key in ("B", "C"):
        arms.append(
            {
                "id": key,
                "method": "cruxible-security-kit" if key == "B" else "cruxible-no-kit",
                "plugin": {"name": "shipped-cruxible-plugin", "version": "fixture"},
                "kit": {"name": "security", "version": "fixture"} if key == "B" else None,
                "memory_paths": [".cruxible"],
            }
        )
    return BenchmarkConfig.model_validate(
        {
            "name": "fixture",
            "target": {"git_url": str(target), "base_commit": git("rev-parse", "HEAD")},
            "model": "scripted-fixture",
            "budget": {"max_tokens": 1000, "max_turns": 10, "max_seconds": 30},
            "repetitions": 2,
            "arms": arms,
            "sessions": [
                {"id": "baseline", "description": "Inspect the base", "changes": []},
                {
                    "id": "fix",
                    "description": "Remove the unsafe path",
                    "changes": [
                        {
                            "kind": "patch",
                            "description": "Disable the fixture danger flag",
                            "text": FIX,
                        }
                    ],
                },
                {
                    "id": "revert",
                    "description": "Revert the fix",
                    "changes": [
                        {
                            "kind": "command",
                            "description": "Restore the fixture base source",
                            "argv": [
                                sys.executable,
                                "-c",
                                f"from pathlib import Path; Path('app.py').write_text({BASE!r})",
                            ],
                        }
                    ],
                },
            ],
            "questions": [
                {
                    "id": "control",
                    "kind": "control",
                    "prompt": "Read the version and current danger flag from app.py.",
                    "criteria": [
                        {
                            "id": "current",
                            "description": "Gives the current flag",
                            "expected_by_session": {1: "live", 2: "fixed", 3: "live"},
                        }
                    ],
                    "after_sessions": [1, 2, 3],
                },
                {
                    "id": "contradiction",
                    "kind": "contradiction",
                    "prompt": "What is now contradicted, and what depends on it?",
                    "criteria": [
                        {"id": "judgment", "description": "Identifies the superseded judgment"},
                        {"id": "dependencies", "description": "Identifies dependent findings"},
                    ],
                    "after_sessions": [2, 3],
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
