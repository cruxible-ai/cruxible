"""Transfer locally built provider wheels to any selected daemon through its CAS."""

from pathlib import Path
from typing import TYPE_CHECKING

from cruxible_client.contracts.provider_installation import (
    PlaybillProviderInstallRequest,
    PlaybillProviderInstallResult,
    ProviderWheelObject,
)

if TYPE_CHECKING:
    from cruxible_client.transport.http import CruxibleClient


def install_provider_package(
    client: "CruxibleClient",
    instance_id: str,
    *,
    wheel: Path,
    lock: Path,
    dependency_wheels: tuple[Path, ...] = (),
    extras: tuple[str, ...] = (),
    control_domain: str = "operator",
    reverify: bool = False,
) -> PlaybillProviderInstallResult:
    """Paths are consumed here on the client; the daemon receives only CAS references."""

    def transfer(path: Path) -> ProviderWheelObject:
        # Validate the filename before uploading any bytes.
        ProviderWheelObject(filename=path.name, digest="sha256:" + "0" * 64)
        stored = client.store_playbill_body(instance_id, path.read_bytes())
        return ProviderWheelObject(filename=path.name, digest=stored.digest)

    root = transfer(wheel)
    dependencies = tuple(transfer(path) for path in dependency_wheels)
    retained_lock = client.store_playbill_body(instance_id, lock.read_bytes())
    return client.install_playbill_provider(
        instance_id,
        PlaybillProviderInstallRequest(
            wheel=root,
            lock_digest=retained_lock.digest,
            dependencies=dependencies,
            extras=tuple(sorted(set(extras))),
            control_domain=control_domain,
            reverify=reverify,
        ),
    )
