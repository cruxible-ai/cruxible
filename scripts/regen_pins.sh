#!/usr/bin/env bash
# Usage: PYTHON=/path/to/python scripts/regen_pins.sh /path/to/scratch [--succession id]
set -euo pipefail

if [[ $# -lt 1 || -z "$1" || "$1" == -* ]]; then
    echo "Usage: $0 SCRATCH_DIR [--succession REGISTER_ENTRY]" >&2
    exit 2
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "$1"
scratch_root="$(cd -- "$1" && pwd)"
shift
export HOME="$scratch_root"
export CRUXIBLE_STATE_ROOT="$scratch_root/state"
export PYTHONPATH="$repo_root/src:$repo_root/packages/cruxible-client/src${PYTHONPATH:+:$PYTHONPATH}"
python_bin="${PYTHON:-python3}"

"$python_bin" "$repo_root/scripts/update_client_contract_snapshot.py"
"$python_bin" "$repo_root/scripts/update_authoring_wire_catalog.py"
"$python_bin" "$repo_root/scripts/update_http_surface_snapshot.py"
"$python_bin" "$repo_root/scripts/update_playbill_served_surface.py" "$@"
