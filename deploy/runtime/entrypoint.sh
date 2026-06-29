#!/bin/sh
set -eu

state_dir="${CRUXIBLE_SERVER_STATE_DIR:-/var/lib/cruxible/server}"

python - "$state_dir" <<'PY'
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

state_dir = Path(sys.argv[1])

if not state_dir.exists():
    print(
        f"cruxible-runtime: external state mount required; {state_dir} does not exist",
        file=sys.stderr,
    )
    sys.exit(1)

if not state_dir.is_dir():
    print(
        f"cruxible-runtime: external state mount required; {state_dir} is not a directory",
        file=sys.stderr,
    )
    sys.exit(1)

if not os.path.ismount(state_dir):
    print(
        f"cruxible-runtime: external state mount required at {state_dir}",
        file=sys.stderr,
    )
    sys.exit(1)

probe_path: str | None = None
try:
    with tempfile.NamedTemporaryFile(
        prefix=".cruxible-mount-check-",
        dir=state_dir,
        delete=False,
    ) as probe:
        probe.write(b"ok\n")
        probe_path = probe.name
except OSError as exc:
    print(
        f"cruxible-runtime: external state mount is not writable at {state_dir}: {exc}",
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
