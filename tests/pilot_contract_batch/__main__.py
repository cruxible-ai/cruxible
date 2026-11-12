"""Run the pilot scenarios and print their transcripts.

    python -m tests.pilot_contract_batch [SCENARIO ...] [--serial] [--keep] [--quiet]

``SCENARIO`` is ``baseline``, ``s1`` ... ``s8`` or a full name (``s4-binding``);
none means all. Run from the core checkout, under the shared CPU lock::

    pytest-lock.sh nice -n 15 env PILOT_OUT=... CRUXIBLE_KITS_CHECKOUT=... \\
        CRUXIBLE_PROVIDERS_CHECKOUT=... PYTHON -m tests.pilot_contract_batch

The run gets its own scratch directory for ``HOME``, ``CRUXIBLE_STATE_ROOT`` and
pytest's temporary tree (deleted afterwards unless ``--keep``), ``PYTHONPATH``
pointing at this checkout, the providers checkout's own runtime and the kit
gate's directory (``kits/security/source/pilot``), and
``PILOT_REQUIRED=1`` so a missing checkout fails instead of skipping.
Transcripts and ``results.tsv`` go to ``$PILOT_OUT`` (a new temporary directory
when unset); its path is printed last.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _files(names: list[str]) -> list[str]:
    if not names:
        return [str(HERE)]
    chosen = []
    for name in names:
        short = name.split("-", 1)[0]
        pattern = "test_baseline.py" if short == "baseline" else f"test_{short}_*.py"
        found = sorted(HERE.glob(pattern))
        if not found:
            raise SystemExit(f"no scenario named {name!r} (baseline, s1 ... s8)")
        chosen.extend(str(path) for path in found)
    return chosen


def main(argv: list[str]) -> int:
    flags = {arg for arg in argv if arg.startswith("--")}
    names = [arg for arg in argv if not arg.startswith("--")]
    unknown = flags - {"--serial", "--keep", "--quiet"}
    if unknown:
        raise SystemExit(f"unknown option(s): {sorted(unknown)}")

    out = Path(os.environ.get("PILOT_OUT") or tempfile.mkdtemp(prefix="pilot-out-"))
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="pilot-run-"))
    home = scratch / "home"
    home.mkdir()

    paths = [str(REPO / "src"), str(REPO / "packages/cruxible-client/src"), str(REPO)]
    providers = os.environ.get("CRUXIBLE_PROVIDERS_CHECKOUT")
    if providers:
        paths.append(str(Path(providers) / "packages/cruxible-provider-runtime/src"))
    kits = os.environ.get("CRUXIBLE_KITS_CHECKOUT")
    if kits:
        paths.append(str(Path(kits) / "kits/security/source/pilot"))
    env = {
        **os.environ,
        "HOME": str(home),
        "CRUXIBLE_STATE_ROOT": str(home / "state"),
        "PYTHONPATH": os.pathsep.join(paths),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PILOT_OUT": str(out),
        "PILOT_REQUIRED": "1",
        "CRUXIBLE_PROVIDERS_REQUIRED": "1",
    }
    env.pop("CRUXIBLE_SERVER_BEARER_TOKEN", None)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "-rxXfE",
        f"--basetemp={scratch / 'pytest'}",
        *([] if "--serial" in flags else ["-n", "4", "--dist", "loadfile"]),
        *_files(names),
    ]
    try:
        status = subprocess.run(command, cwd=REPO, env=env, check=False).returncode
    finally:
        if "--keep" not in flags:
            shutil.rmtree(scratch, ignore_errors=True)
    if "--quiet" not in flags:
        for transcript in sorted(out.glob("*.txt")):
            print(f"\n===== {transcript.name} =====")
            print(transcript.read_text(), end="")
    print(f"\nPILOT_OUT={out}")
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
