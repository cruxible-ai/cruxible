#!/bin/sh
set -eu

state_root="${CRUXIBLE_STATE_ROOT:-/var/lib/cruxible}"

python - "$state_root" <<'PY'
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

state_root = Path(sys.argv[1])

if not state_root.exists():
    print(
        f"cruxible-runtime: external state mount required; {state_root} does not exist",
        file=sys.stderr,
    )
    sys.exit(1)

if not state_root.is_dir():
    print(
        f"cruxible-runtime: external state mount required; {state_root} is not a directory",
        file=sys.stderr,
    )
    sys.exit(1)

if not os.path.ismount(state_root):
    print(
        f"cruxible-runtime: external state mount required at {state_root}",
        file=sys.stderr,
    )
    sys.exit(1)

probe_path: str | None = None
try:
    with tempfile.NamedTemporaryFile(
        prefix=".cruxible-mount-check-",
        dir=state_root,
        delete=False,
    ) as probe:
        probe.write(b"ok\n")
        probe_path = probe.name
except OSError as exc:
    print(
        f"cruxible-runtime: external state mount is not writable at {state_root}: {exc}",
        file=sys.stderr,
    )
    sys.exit(1)
finally:
    if probe_path is not None:
        try:
            os.unlink(probe_path)
        except OSError:
            pass
PY

exec "$@"
