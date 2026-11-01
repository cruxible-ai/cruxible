#!/usr/bin/env bash
# Usage: PYTHON=/path/to/python scripts/regen_pins.sh /path/to/scratch [--succession id]
set -euo pipefail

if [[ $# -lt 1 || -z "$1" || "$1" == -* ]]; then
    echo "Usage: $0 SCRATCH_DIR [--succession REGISTER_ENTRY]" >&2
    exit 2
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
real_home="$(cd -- "$HOME" && pwd -P)"
mkdir -p -- "$1"
scratch_root="$(cd -- "$1" && pwd -P)"
shift

if [[ "$scratch_root" == "$real_home" ]]; then
    echo "Refusing to use HOME as the scratch directory: $scratch_root" >&2
    exit 2
fi

# Refuse both a state root itself and a scratch HOME containing a previous root.
# Check markers before any generator can create or open operational state.
for candidate in "$scratch_root" "$scratch_root/state" "$scratch_root/.cruxible"; do
    if [[ -e "$candidate/daemon" || -e "$candidate/instances" ||
          -e "$candidate/state.db" || -e "$candidate/registry.db" ||
          -e "$candidate/instance.json" || -e "$candidate/ledger.git" ]]; then
        echo "Refusing an existing Cruxible state root: $candidate" >&2
        exit 2
    fi
done
if [[ -n "${CRUXIBLE_STATE_ROOT:-}" && -d "$CRUXIBLE_STATE_ROOT" ]]; then
    configured_root="$(cd -- "$CRUXIBLE_STATE_ROOT" && pwd -P)"
    if [[ "$scratch_root" == "$configured_root" || "$scratch_root/state" == "$configured_root" ]]; then
        echo "Refusing the configured Cruxible state root: $configured_root" >&2
        exit 2
    fi
fi

export HOME="$scratch_root"
export CRUXIBLE_STATE_ROOT="$scratch_root/state"
export PYTHONPATH="$repo_root/src:$repo_root/packages/cruxible-client/src${PYTHONPATH:+:$PYTHONPATH}"
if [[ -x "$repo_root/.venv/bin/python" ]]; then
    default_python="$repo_root/.venv/bin/python"
else
    default_python="python3"
fi
python_bin="${PYTHON:-$default_python}"

"$python_bin" "$repo_root/scripts/update_client_contract_snapshot.py"
"$python_bin" "$repo_root/scripts/update_authoring_wire_catalog.py"
"$python_bin" "$repo_root/scripts/update_http_surface_snapshot.py"
"$python_bin" "$repo_root/scripts/update_playbill_served_surface.py" "$@"
