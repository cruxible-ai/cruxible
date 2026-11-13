"""Schema-3 registrations for the providers the pilot's copy of the security kit installs.

The kit checkout (read only) and the providers checkout it records ship their
provider packages at registration schema 2, which core no longer reads. The
kit gate lowers two of them into builds while it builds its world:
``cruxible-provider-web`` (the providers checkout) and ``security-osv-provider``
(the kit's own). This module holds their schema-3 registrations, rebuilt once
with the provider runtime's ``write_registration``, and lets the gate read them
in place of the schema-2 files while the pilot runs.

Rebuilding (the kit migration reuses exactly this): for each package, copy its
source directory into scratch, give every operation its schema-3 fields
(``description``, ``source``, ``serves``; ``enumerates`` dropped), and run
``write_registration`` over the copy with the package's own ``contracts/``
files as the pool, so ``declares`` is exactly the closure the operations reach.
The capability each operation serves is the one ``kit_capabilities`` converts
the kit's slot to (by request Contract); the web operations take the
descriptions, sources and capability names of the providers repository's
schema-3 web registration.

    RUNTIME=<schema-3 providers>/packages/cruxible-provider-runtime/src
    PYTHONPATH=src:packages/cruxible-client/src:.:$RUNTIME \\
        python -m tests.pilot_contract_batch.fixtures.schema3 \\
        <recorded providers checkout> <kits checkout> <schema-3 providers checkout>
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent / "schema3"
WEB = "cruxible-provider-web"
OSV = "security-osv-provider"
KITS_REPOSITORY = "https://github.com/cruxible-ai/cruxible-kits"


def web_root(providers: Path) -> Path:
    return providers / "packages/cruxible-provider-web/src/cruxible_provider_web"


def osv_root(kits: Path) -> Path:
    return kits / "kits/security/source/providers/security-osv-provider/src/security_osv_provider"


def registration_bytes(package: str) -> bytes:
    """The committed schema-3 registration of ``package``."""

    return (HERE / f"{package}.registration.json").read_bytes()


# -- the rebuild (run once, with a schema-3 provider runtime on the path) ------------------


def _osv_metadata(operation: Mapping[str, Any]) -> dict[str, Any]:
    from tests.pilot_contract_batch.kit_capabilities import KIT_CAPABILITIES

    capability, _result, _effect, description = KIT_CAPABILITIES[operation["in"].split("@")[0]]
    module = operation["entrypoint"].split(":")[0].split(".")[-1]
    return {
        "description": description,
        "source": {
            "path": f"kits/security/source/providers/security-osv-provider/src/"
            f"security_osv_provider/{module}.py",
            "repository": KITS_REPOSITORY,
        },
        "serves": [capability],
    }


def _web_metadata(schema3_web: Path) -> dict[str, dict[str, Any]]:
    document = json.loads((web_root(schema3_web) / "registration.json").read_text())
    return {
        item["name"]: {key: item[key] for key in ("description", "source", "serves")}
        for item in document["operations"]
    }


def _rebuild(root: Path, metadata: Mapping[str, Mapping[str, Any]] | None) -> bytes:
    from cruxible_provider_runtime.registration import write_registration

    with tempfile.TemporaryDirectory(prefix="pilot-schema3-") as scratch:
        copy = Path(scratch) / root.name
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns("__pycache__"))
        document = json.loads((copy / "registration.json").read_text())
        operations = []
        for item in document["operations"]:
            item = {key: value for key, value in item.items() if key != "enumerates"}
            extra = _osv_metadata(item) if metadata is None else metadata[item["name"]]
            operations.append({**item, **extra})
        operations.sort(key=lambda item: str(item["name"]).encode("utf-8"))
        document.update({"schema_version": 3, "operations": operations})
        (copy / "registration.json").write_text(json.dumps(document))
        pool = [json.loads(path.read_text()) for path in sorted(copy.glob("contracts/*/v*.json"))]
        write_registration(copy, contracts=pool)
        return (copy / "registration.json").read_bytes()


def rebuild(providers: Path, kits: Path, schema3_providers: Path) -> None:
    HERE.mkdir(exist_ok=True)
    built = {
        WEB: _rebuild(web_root(providers), _web_metadata(schema3_providers)),
        OSV: _rebuild(osv_root(kits), None),
    }
    for package, content in built.items():
        (HERE / f"{package}.registration.json").write_bytes(content)


# -- reading them while the kit gate builds its world ---------------------------------------


def _schema(root: Path) -> object:
    return json.loads((root / "registration.json").read_text()).get("schema_version")


@contextmanager
def schema3_packages(providers: Path, kits: Path) -> Iterator[None]:
    """While open, the gate reads schema-3 registrations of the two packages.

    A package whose own registration is schema 3 (the web package of a schema-3
    providers checkout) is read as it is. One still at schema 2 is copied into
    scratch with its committed schema-3 rebuild in place (the manifest is the
    package's own, so the digest it names holds).

    The web Contracts the gate accepts on the instance
    (``web_contracts.members``, which reads a ``contracts/`` folder schema-3
    packages no longer have) are the ones the registration used for the build
    carries: one set of copies for both. The gate's two calls that predate the
    batch are answered too: a pin by the instance's Contracts
    (``tree_contract_digests``, gone from core) and ``pinned_operations``
    taking those digests. An install pins to the carried copies, so both are
    accepted and ignored.
    """

    import web_contracts  # the kit gate's module (its directory is on the path)

    import cruxible_core.providers.package_metadata as package_metadata
    import cruxible_core.service.procedures.provider_installation as provider_installation
    from cruxible_client.contracts.shapes import contract_path, render_contract
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

    class GateDocument(PackageRegistrationDocumentV1):
        def pinned_operations(self, digests: object = None) -> Any:  # type: ignore[override]
            del digests
            return super().pinned_operations()

    original = package_metadata.read_package_registration
    original_members = web_contracts.members
    with tempfile.TemporaryDirectory(prefix="pilot-schema3-") as scratch:
        redirected: dict[Path, Path] = {}
        for package, root in ((WEB, web_root(providers)), (OSV, osv_root(kits))):
            if _schema(root) == 3:
                continue
            copy = Path(scratch) / package / root.name
            shutil.copytree(root, copy, ignore=shutil.ignore_patterns("__pycache__"))
            (copy / "registration.json").write_bytes(registration_bytes(package))
            redirected[root.resolve()] = copy
        web = redirected.get(web_root(providers).resolve(), web_root(providers))
        carried = Path(scratch) / "web-carried"
        for contract in original(web).declares.contracts:
            member = carried / contract_path(contract.name, contract.version)
            member.parent.mkdir(parents=True, exist_ok=True)
            member.write_bytes(render_contract(contract))

        def read(root: Path) -> Any:
            document = original(redirected.get(Path(root).resolve(), Path(root)))
            return GateDocument.model_validate(document.model_dump(mode="python", by_alias=True))

        package_metadata.read_package_registration = read  # type: ignore[assignment]
        provider_installation.tree_contract_digests = lambda tree: None  # type: ignore[attr-defined]
        web_contracts.members = lambda checkout: web_contracts.package_members(carried)
        try:
            yield
        finally:
            package_metadata.read_package_registration = original
            web_contracts.members = original_members
            del provider_installation.tree_contract_digests  # type: ignore[attr-defined]


if __name__ == "__main__":
    rebuild(*(Path(item).resolve() for item in sys.argv[1:4]))
