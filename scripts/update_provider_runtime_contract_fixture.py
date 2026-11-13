"""Vendor the provider-runtime protocol 2.0 wire contract from a pinned checkout.

cruxible-providers produces the fixture (``scripts/export_runtime_contract.py``
there); core copies its exact bytes at the commit it pins
(``PROVIDER_RUNTIME_CONTRACT_COMMIT``) and holds its own mirror to it
(``tests/test_guardrails/test_provider_runtime_contract_mirror.py``). This
updater is intentionally not part of ordinary CI, and core never imports the
provider package.

    CRUXIBLE_PROVIDERS_CHECKOUT=<checkout> uv run python \
        scripts/update_provider_runtime_contract_fixture.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "packages/cruxible-client/src"))

# The same variable the provider-runtime tests read (tests/support/provider_checkout.py).
CHECKOUT_ENV = "CRUXIBLE_PROVIDERS_CHECKOUT"
SOURCE = "packages/cruxible-provider-runtime/tests/fixtures/provider_runtime_contract_v2.json"
DESTINATION = REPO_ROOT / "tests/fixtures/provider_runtime_contract_v2.json"


def main() -> None:
    from cruxible_core.providers.provider_runtime_contract import (
        PROVIDER_RUNTIME_CONTRACT_COMMIT,
    )

    checkout = os.environ.get(CHECKOUT_ENV)
    if not checkout:
        raise SystemExit(f"set {CHECKOUT_ENV} to a cruxible-providers checkout")
    # The bytes at the pinned commit, whatever the checkout's working tree holds.
    content = subprocess.check_output(
        ["git", "show", f"{PROVIDER_RUNTIME_CONTRACT_COMMIT}:{SOURCE}"],
        cwd=Path(checkout).resolve(),
    )
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_bytes(content)
    print(f"Wrote {DESTINATION.relative_to(REPO_ROOT)} from {PROVIDER_RUNTIME_CONTRACT_COMMIT}")


if __name__ == "__main__":
    main()
