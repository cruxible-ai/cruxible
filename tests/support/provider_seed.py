"""Test support for the real compiler-pinned workspace Provider checkout."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cruxible_core.playbill.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_PROVIDER_ID,
    WORKSPACE_FILE_SEED_MANIFEST,
)
from cruxible_core.runtime.provider_runtime import (
    PROVIDER_RUNTIME_CONFIG_PATH,
    ProviderRuntimeOperationalConfigV1,
    ProviderSeedMaterializationConfigV1,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: The environment variable CI sets to the checked-out adapter repository. The
#: providers repository is public, so this is an ordinary second checkout rather
#: than anything credential-bearing.
PROVIDERS_CHECKOUT_ENV = "CRUXIBLE_PROVIDERS_CHECKOUT"

#: Named in every skip so a green run on a host without the adapter checkout still
#: points at what would make it real coverage rather than reading as coverage.
MISSING_CHECKOUT_SKIP_REASON = (
    "the cruxible-providers checkout is absent, so the real local materialization cannot be "
    f"reproduced; point {PROVIDERS_CHECKOUT_ENV} at a checkout of cruxible-providers at the "
    "commit the seed manifest pins, or place one beside this repository"
)


def find_workspace_provider_checkout() -> Path | None:
    """Locate the separately governed adapter checkout without a developer path literal."""

    configured = os.environ.get(PROVIDERS_CHECKOUT_ENV, "").strip()
    candidates = (
        *((Path(configured),) if configured else ()),
        REPOSITORY_ROOT.parent / "cruxible-providers",
        REPOSITORY_ROOT.parent.parent / "cruxible-providers",
    )
    for candidate in candidates:
        if (candidate / "scripts" / "seed_pins.py").is_file():
            return candidate.resolve(strict=True)
    return None


def workspace_provider_checkout() -> Path:
    """Return the real adapter checkout, or skip naming the follow-on card."""

    located = find_workspace_provider_checkout()
    if located is None:
        pytest.skip(MISSING_CHECKOUT_SKIP_REASON)
    return located


def workspace_seed_materialization(
    checkout: Path | None = None,
) -> ProviderSeedMaterializationConfigV1:
    checkout = workspace_provider_checkout() if checkout is None else checkout.resolve(strict=True)
    pin_key, materialization_digest = WORKSPACE_FILE_SEED_MANIFEST.materialization_digests[0]
    return ProviderSeedMaterializationConfigV1(
        provider_id=WORKSPACE_FILE_PROVIDER_ID,
        checkout_path=str(checkout),
        provider_commit=WORKSPACE_FILE_SEED_MANIFEST.provider_commit,
        environment_pin_key=pin_key,
        materialization_digest=materialization_digest,
    )


def write_workspace_seed_config(state_root: Path, checkout: Path | None = None) -> Path:
    config = ProviderRuntimeOperationalConfigV1(
        seed_materializations=(workspace_seed_materialization(checkout),)
    )
    path = state_root / PROVIDER_RUNTIME_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "MISSING_CHECKOUT_SKIP_REASON",
    "PROVIDERS_CHECKOUT_ENV",
    "find_workspace_provider_checkout",
    "workspace_provider_checkout",
    "workspace_seed_materialization",
    "write_workspace_seed_config",
]
