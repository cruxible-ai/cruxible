"""Verify that every version a release stamps agrees before anything publishes.

Usage:
    uv run python scripts/check_version_lockstep.py          # local / pre-push
    python scripts/check_version_lockstep.py --tag v0.3.1    # release workflow

The publish workflow refuses to build unless the core package version, the
``cruxible-client`` package version, and the core dependency pin on that client
all name the same version. Those checks used to live only in the workflow, so the
first thing that could see a mismatch was a pushed tag: 0.3.1 tagged with the
client still at 0.3.0 and the publish failed at the gate. This script is the
single implementation, invoked by both the workflow (with ``--tag``) and
``scripts/ci_parity.sh`` (without one, since a local tree has no release tag),
so a lockstep break fails on the workstation instead of at the tag.

Stdlib only on purpose: the release workflow's verify-versions job runs it on a
bare checkout with no dependency install.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CORE_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CLIENT_PYPROJECT = _REPO_ROOT / "packages" / "cruxible-client" / "pyproject.toml"


def _project_table(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"{path} has no [project] table")
    return project


def _version(project: dict[str, Any], path: Path) -> str:
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{path} has no non-empty project.version")
    return version


def check_version_lockstep(
    *,
    core_pyproject: Path = _CORE_PYPROJECT,
    client_pyproject: Path = _CLIENT_PYPROJECT,
    tag: str | None = None,
) -> list[str]:
    """Return every lockstep violation, empty when the tree is releasable.

    All checks run: a release stamps several files, and reporting only the
    first mismatch would hide the rest until the next attempt.
    """
    failures: list[str] = []

    core = _project_table(core_pyproject)
    client = _project_table(client_pyproject)
    core_version = _version(core, core_pyproject)
    client_version = _version(client, client_pyproject)

    if core_version != client_version:
        failures.append(
            f"Version mismatch: cruxible={core_version} cruxible-client={client_version}"
        )

    dependencies = core.get("dependencies")
    if not isinstance(dependencies, list):
        failures.append(f"{core_pyproject} has no project.dependencies list")
    else:
        expected_pin = f"cruxible-client=={client_version}"
        if expected_pin not in dependencies:
            actual = [
                str(dep)
                for dep in dependencies
                if isinstance(dep, str) and dep.replace("_", "-").startswith("cruxible-client")
            ]
            found = ", ".join(actual) if actual else "no cruxible-client dependency at all"
            failures.append(f"Missing exact dependency pin: {expected_pin} (found: {found})")

    # Only the release workflow knows the tag; locally there is none to check.
    if tag is not None and tag != f"v{core_version}":
        failures.append(f"Tag {tag} does not match package version {core_version}")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--core-pyproject", type=Path, default=_CORE_PYPROJECT)
    parser.add_argument("--client-pyproject", type=Path, default=_CLIENT_PYPROJECT)
    parser.add_argument(
        "--tag",
        default=None,
        help="Release tag to require equal to v<core version>. Omit outside the release workflow.",
    )
    args = parser.parse_args(argv)

    try:
        failures = check_version_lockstep(
            core_pyproject=args.core_pyproject,
            client_pyproject=args.client_pyproject,
            tag=args.tag,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1

    core_version = _version(_project_table(args.core_pyproject), args.core_pyproject)
    print(f"version lockstep ok: core, client, and dependency pin all {core_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
