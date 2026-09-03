"""Test support for the real compiler-pinned workspace Provider checkout."""

from __future__ import annotations

import json
from pathlib import Path

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


def workspace_provider_checkout() -> Path:
    """Locate the separately governed adapter checkout without a developer path literal."""

    candidates = (
        REPOSITORY_ROOT.parent / "cruxible-providers",
        REPOSITORY_ROOT.parent.parent / "cruxible-providers",
    )
    for candidate in candidates:
        if (candidate / "scripts" / "seed_pins.py").is_file():
            return candidate.resolve(strict=True)
    raise AssertionError("the cruxible-providers checkout is required for workspace seed tests")


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
    "workspace_provider_checkout",
    "workspace_seed_materialization",
    "write_workspace_seed_config",
]
