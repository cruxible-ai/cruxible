"""The cruxible-providers checkout the provider-runtime tests run against.

CI checks out cruxible-providers at ``CRUXIBLE_PROVIDERS_COMMIT``
(``.github/workflows/ci.yml``), installs ``cruxible-provider-runtime`` from it
into the test environment and exports ``CRUXIBLE_PROVIDERS_CHECKOUT``. Local
runs find it the same way: point the variable at a checkout (at the CI commit
for CI's results) and install the runtime from it::

    uv pip install "$CRUXIBLE_PROVIDERS_CHECKOUT/packages/cruxible-provider-runtime"

Without the variable these tests skip. With it, a missing checkout or runtime
is a failure, never a skip. The provider wheels are built from the checkout
once per test session (shared by xdist workers).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

CHECKOUT_ENV = "CRUXIBLE_PROVIDERS_CHECKOUT"
# The provider distributions the installation tests transfer or index.
WHEEL_PACKAGES = ("runtime", "workspace", "web")


@dataclass(frozen=True)
class ProviderCheckout:
    repository: Path
    wheels: Path


def provider_checkout_path() -> Path | None:
    value = os.environ.get(CHECKOUT_ENV)
    return Path(value).resolve() if value else None


def _require_checkout() -> Path:
    checkout = provider_checkout_path()
    if checkout is None:
        pytest.skip(
            f"set {CHECKOUT_ENV} to a cruxible-providers checkout to run the "
            "provider-runtime tests (tests/support/provider_checkout.py)"
        )
    if not (checkout / "packages/cruxible-provider-runtime").is_dir():
        pytest.fail(f"{CHECKOUT_ENV}={checkout} is not a cruxible-providers checkout")
    try:
        import cruxible_provider_runtime  # noqa: F401
    except ModuleNotFoundError:
        pytest.fail(
            f"{CHECKOUT_ENV} is set but cruxible_provider_runtime is not importable; run "
            f'uv pip install "{checkout}/packages/cruxible-provider-runtime"'
        )
    return checkout


def _build_wheels(checkout: Path, root: Path) -> Path:
    """Build every provider wheel once, publishing the directory atomically."""

    target = root / "provider-wheels"
    if target.is_dir():
        return target
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("building the provider wheels requires uv on PATH")
    staging = root / f"provider-wheels-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    for package in WHEEL_PACKAGES:
        subprocess.run(
            [
                uv,
                "build",
                "--wheel",
                "--out-dir",
                str(staging),
                str(checkout / f"packages/cruxible-provider-{package}"),
            ],
            check=True,
            capture_output=True,
        )
    try:
        staging.rename(target)
    except OSError:
        # Another xdist worker published first; its wheels are the same build.
        shutil.rmtree(staging, ignore_errors=True)
    return target


@pytest.fixture(scope="session")
def provider_runtime() -> Path:
    """The checkout, with its runtime importable in this environment."""

    return _require_checkout()


@pytest.fixture(scope="session")
def provider_checkout(
    provider_runtime: Path, tmp_path_factory: pytest.TempPathFactory
) -> ProviderCheckout:
    """The checkout plus its provider wheels, built for this session."""

    base = tmp_path_factory.getbasetemp()
    # Under xdist every worker's base shares one parent for this run.
    root = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    return ProviderCheckout(
        repository=provider_runtime, wheels=_build_wheels(provider_runtime, root)
    )


def checkout_predates_embedded_wheel_locks() -> bool:
    """Wheels built before cruxible-providers 5fcef6d carry no ``uv.lock``."""

    checkout = provider_checkout_path()
    if checkout is None:
        return False
    pyproject = checkout / "packages/cruxible-provider-workspace/pyproject.toml"
    return "extra-metadata" not in pyproject.read_text(encoding="utf-8")


def checkout_predates_web_fetch_material() -> bool:
    """web.fetch declares its captured material only from cruxible-providers ff0dab1."""

    checkout = provider_checkout_path()
    if checkout is None:
        return False
    contract = (
        checkout
        / "packages/cruxible-provider-web/src/cruxible_provider_web/contracts/web.fetch.json"
    )
    return "material" not in json.loads(contract.read_text(encoding="utf-8"))["contracts"]
