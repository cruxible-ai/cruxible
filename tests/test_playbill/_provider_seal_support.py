"""Test-only provider-repository stand-in for materialization seal-v2 fixtures."""

from __future__ import annotations

import hashlib
from pathlib import Path

from cruxible_client.contracts.canonical import canonical_bytes


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_test_provider_seal_v2(
    *,
    environment_root: Path,
    seal_path: Path,
    interpreter_path: Path,
    lock_digest: str,
    materialization_digest: str,
    runtime_version: str = "1.0.0",
    entrypoint: str = "demo.runtime:Provider",
) -> tuple[Path, ...]:
    """Build only test material; production seal ownership stays outside core."""

    module = entrypoint.partition(":")[0]
    module_path = environment_root.joinpath(*module.split(".")).with_suffix(".py")
    module_path.parent.mkdir(parents=True, exist_ok=True)
    package_init = module_path.parent / "__init__.py"
    package_init.write_bytes(b"")
    module_path.write_bytes(b"class Provider:\n    pass\n")

    site_root = environment_root / "lib/site-packages"
    runtime_file = site_root / "cruxible_provider_runtime/__init__.py"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_bytes(b'__version__ = "1.0.0"\n')
    record = site_root / f"cruxible_provider_runtime-{runtime_version}.dist-info/RECORD"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        f"cruxible_provider_runtime/__init__.py,,\n{record.parent.name}/RECORD,,\n",
        encoding="utf-8",
    )
    covered = tuple(sorted((interpreter_path, package_init, module_path, runtime_file, record)))
    seal_path.write_bytes(
        canonical_bytes(
            {
                "tag": "cruxible.provider.seal.v2",
                "installed_distributions": {
                    "cruxible-provider-runtime": runtime_version,
                    "demo-provider": "1.0.0",
                },
                "lock_sha256": lock_digest,
                "materialization_digest": materialization_digest,
                "files": [
                    {
                        "path": path.relative_to(environment_root).as_posix(),
                        "sha256": _digest(path),
                    }
                    for path in sorted(
                        covered,
                        key=lambda item: item.relative_to(environment_root).as_posix().encode(),
                    )
                ],
            }
        )
    )
    return covered


__all__ = ["write_test_provider_seal_v2"]
