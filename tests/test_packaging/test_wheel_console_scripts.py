"""End-to-end checks that the built+installed wheel exposes its console scripts.

The work item this test backs (``wi-verify-installed-wheel-aliases``) exists to
catch packaging regressions that the in-repo ``uv run`` developer flow hides:

* a wheel that cannot be built at all (e.g. a redundant ``force-include`` that
  double-adds files into the archive), and
* declared ``[project.scripts]`` aliases that are missing or broken once the
  package is actually installed into a fresh interpreter.

The test is hermetic: it builds the wheels into ``tmp_path``, installs them into
a throwaway venv with ``--find-links`` (so the workspace-local ``cruxible-client``
dependency resolves without touching PyPI), and asserts every declared console
script exists, resolves its entry-point target, and -- for the user-facing
``cruxible`` CLI -- actually runs. It is marked ``wheel`` so it stays out of the
default suite, and it ``pytest.skip``s cleanly when the build/install tooling is
unavailable in the sandbox rather than failing spuriously.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.wheel

# Aliases that are runnable with only the base install. The ``cruxible-mcp``
# script imports optional dependencies (the ``mcp`` extra) and starts a
# long-lived daemon, so it is exercised at the import-resolution level rather
# than invoked. The Cruxible daemon launches via ``cruxible server start`` (a
# subcommand of the runnable ``cruxible`` CLI), not a console-script of its own.
_BASE_RUNNABLE_ALIASES = ("cruxible",)


def _declared_scripts() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert isinstance(scripts, dict)
    return {str(name): str(target) for name, target in scripts.items()}


def _require_uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not available; cannot build/install the wheel")
    return uv


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    skip_on_failure: bool = False,
    skip_reason: str = "",
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (
            f"command: {' '.join(args)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        if skip_on_failure:
            pytest.skip(f"{skip_reason}\n{detail}")
        raise AssertionError(f"command failed\n{detail}")
    return completed


def _build_wheels(uv: str, out_dir: Path) -> None:
    """Build the core and workspace-client wheels into ``out_dir``.

    Build failures here are the *point* of the test (a broken wheel is a real
    finding), so they assert rather than skip. Environmental inability to build
    -- no network for build isolation, etc. -- surfaces as a build error too, so
    the message is kept verbose to make the cause obvious in CI logs.
    """
    _run([uv, "build", "--wheel", "--out-dir", str(out_dir)], cwd=REPO_ROOT)
    _run(
        [uv, "build", "--wheel", "--package", "cruxible-client", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
    )


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin")


def _script_path(venv: Path, name: str) -> Path:
    bin_dir = _venv_bin(venv)
    candidate = bin_dir / name
    if sys.platform == "win32" and not candidate.exists():
        candidate = bin_dir / f"{name}.exe"
    return candidate


@pytest.fixture(scope="module")
def installed_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheels and install ``cruxible[mcp,server]`` into a fresh venv."""
    uv = _require_uv()
    work = tmp_path_factory.mktemp("wheel-install")
    wheel_dir = work / "wheels"
    wheel_dir.mkdir()

    _build_wheels(uv, wheel_dir)

    core_wheels = sorted(wheel_dir.glob("cruxible-*.whl"))
    assert core_wheels, f"no cruxible wheel was produced in {wheel_dir}"
    core_wheel = core_wheels[-1]

    venv = work / "venv"
    _run([uv, "venv", str(venv)], skip_on_failure=True, skip_reason="could not create venv")

    # Install with all extras so the optional MCP/server entry points are
    # importable; --find-links lets the workspace-local cruxible-client resolve
    # offline from the wheel we just built.
    install = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(_venv_bin(venv) / "python"),
            "--find-links",
            str(wheel_dir),
            f"cruxible[mcp,server] @ {core_wheel}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if install.returncode != 0:
        pytest.skip(
            "could not install the built wheel into the throwaway venv "
            "(likely no network for dependency resolution)\n"
            f"stdout:\n{install.stdout}\nstderr:\n{install.stderr}"
        )
    return venv


def test_pyproject_declares_expected_aliases() -> None:
    """Guard against silent removal/rename of the public console scripts.

    There is deliberately no ``cruxible-server`` script: the daemon launches only
    via ``cruxible server start`` (wi-server-cli-verb-consistency). This assertion
    pins that — a re-added ``cruxible-server`` entry point would fail here.
    """
    scripts = _declared_scripts()
    assert scripts == {
        "cruxible": "cruxible_core.cli.main:cli",
        "cruxible-mcp": "cruxible_core.mcp.server:main",
    }


def test_installed_wheel_exposes_all_declared_scripts(installed_venv: Path) -> None:
    """Every ``[project.scripts]`` alias is installed as an executable script."""
    for name in _declared_scripts():
        script = _script_path(installed_venv, name)
        assert script.exists(), f"console script {name!r} was not installed at {script}"


def test_installed_entry_point_targets_resolve(installed_venv: Path) -> None:
    """Each declared ``module:attr`` entry-point target imports and is callable.

    This covers ``cruxible-mcp`` without launching its daemon (it starts a
    long-lived server rather than honoring ``--help``). The Cruxible daemon has
    no console-script of its own — it launches via ``cruxible server start``.
    """
    python = _venv_bin(installed_venv) / "python"
    checker = (
        "import importlib, importlib.metadata as md\n"
        "declared = {e.name: e.value for e in "
        "md.entry_points(group='console_scripts') if e.name.startswith('cruxible')}\n"
        "for name, value in declared.items():\n"
        "    mod, _, attr = value.partition(':')\n"
        "    obj = getattr(importlib.import_module(mod), attr)\n"
        "    assert callable(obj), f'{name} target {value} is not callable'\n"
        "print('\\n'.join(f'{n}={v}' for n, v in sorted(declared.items())))\n"
    )
    completed = _run([str(python), "-c", checker])
    resolved = dict(
        line.split("=", 1) for line in completed.stdout.strip().splitlines() if "=" in line
    )
    assert resolved == _declared_scripts()


@pytest.mark.parametrize("alias", _BASE_RUNNABLE_ALIASES)
def test_base_runnable_alias_responds(installed_venv: Path, alias: str) -> None:
    """The user-facing ``cruxible`` CLI actually runs from the installed wheel."""
    script = _script_path(installed_venv, alias)
    completed = _run([str(script), "--version"])
    assert "cruxible" in completed.stdout.lower()
