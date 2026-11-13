"""A provider package's metadata is read at registration schema 3 only.

Each wheel here is written by hand, so the reader runs with the provider runtime
made unimportable: it imports and executes nothing. A schema-3 package names its
manifest (schema 2, one row per entrypoint), declares typed operations (each
with a description, its source and the capabilities it serves; one entrypoint
may serve several) and carries the Contracts they reach. A schema-2 or schema-1
registration, a schema-1 manifest, and any disagreement between the
operations, the manifest rows, the runtime requirements and the wheel's
``cruxible.providers`` entry points refuse.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from cruxible_client.contracts.providers import (
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    evaluate_provider_law,
    provider_implementation_digest,
    provider_path,
)
from cruxible_client.contracts.shapes import contract_digest
from cruxible_core.providers.package_metadata import read_wheel_registration
from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES,
)
from tests.support.operation_world import REFUSAL, VALUE

NAME = "local-pair"
MODULE = "local_pair"
VERSION = "0.3.0"
ENTRYPOINT = f"{MODULE}.operation:Pair"


def _operation(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "in": "test.value@1",
        "out": "test.value@1",
        "effect": "pure",
        "refusals": {"too_long": {"contract": "test.refusal@1", "absorbable": True}},
        "entrypoint": ENTRYPOINT,
        "description": f"Test operation {name}.",
        "source": {"path": f"src/{MODULE}/operation.py"},
        "serves": [name],
        **extra,
    }


#: Two operations served by one entrypoint, with a runtime resource on it.
REGISTRATION: dict[str, Any] = {
    "schema_version": 3,
    "operations": [_operation("local.double"), _operation("local.increment")],
    "declares": {"contracts": [contract.model_dump(mode="json") for contract in (REFUSAL, VALUE)]},
    "runtime_requirements": [
        {
            "entrypoint": ENTRYPOINT,
            "kind": "runtime_resource",
            "name": "model-file",
            "description": "a model file on disk",
            "probe_entrypoint": f"{MODULE}.operation:model_present",
        }
    ],
}
MANIFEST: dict[str, Any] = {
    "schema_version": 2,
    "provider_id": NAME,
    "distribution": {"name": NAME, "version": VERSION},
    "entrypoint_group": "cruxible.providers",
    "supported_protocol_majors": [2],
    "implementations": [
        {
            "entrypoint": ENTRYPOINT,
            "backends": ["local_env"],
            "requires_extras": [],
            "declared_endpoints": [],
            "deterministic": True,
        }
    ],
}
ENTRY_POINTS = {"local.double": ENTRYPOINT, "local.increment": ENTRYPOINT}


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def build_wheel(
    directory: Path,
    *,
    registration: dict[str, Any] = REGISTRATION,
    manifest: dict[str, Any] = MANIFEST,
    entry_points: dict[str, str] = ENTRY_POINTS,
) -> Path:
    """A provider wheel written by hand, carrying exactly the given metadata."""

    files: dict[str, bytes] = {
        f"{MODULE}/__init__.py": b"",
        f"{MODULE}/operation.py": b"raise RuntimeError('provider code never runs here')\n",
        f"{MODULE}/manifest.json": json.dumps(manifest).encode(),
    }
    files[f"{MODULE}/registration.json"] = json.dumps(
        {
            **registration,
            "manifest": {
                "path": "manifest.json",
                "digest": _digest(files[f"{MODULE}/manifest.json"]),
            },
        }
    ).encode()
    info = f"{MODULE}-{VERSION}.dist-info"
    files[f"{info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {VERSION}\n".encode()
    )
    files[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{info}/entry_points.txt"] = (
        "[cruxible.providers]\n"
        + "".join(f"{name} = {value}\n" for name, value in entry_points.items())
    ).encode()
    files[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in [*files, f"{info}/RECORD"]).encode()
    wheel = directory / f"{MODULE}-{VERSION}-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return wheel


@pytest.fixture(autouse=True)
def no_provider_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``cruxible_provider_runtime`` import fail: core reads alone."""

    for module in [name for name in sys.modules if name.startswith("cruxible_provider_runtime")]:
        monkeypatch.delitem(sys.modules, module)
    monkeypatch.setitem(sys.modules, "cruxible_provider_runtime", None)


def test_a_schema_3_package_with_two_operations_on_one_entrypoint_lowers_to_one_implementation(
    tmp_path: Path,
) -> None:
    document = read_wheel_registration(build_wheel(tmp_path))
    assert [item.name for item in document.operations] == ["local.double", "local.increment"]
    (requirement,) = document.runtime_requirements
    assert requirement.entrypoint == ENTRYPOINT

    build = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=NAME,
            version=VERSION,
            filename=f"{MODULE}-{VERSION}-py3-none-any.whl",
            sha256="sha256:" + "a" * 64,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp313": "sha256:" + "c" * 64},
        ),
        control_domain="operator",
        operations=document.pinned_operations(),
    )
    # One entrypoint, one implementation identity, serving both operations.
    (record,) = build.implementations
    assert record.entrypoint == ENTRYPOINT
    assert record.implementation_digest == provider_implementation_digest(
        entrypoint=ENTRYPOINT, distribution_sha256="sha256:" + "a" * 64
    )
    assert {item.entrypoint for item in build.operations} == {ENTRYPOINT}
    # The build carries the package's copies; the law reads nothing else.
    assert build.declares == document.declares
    assert build.operations[0].in_.digest == contract_digest(VALUE).tagged
    law = evaluate_provider_law(
        build,
        path=provider_path(build.identity.name),
        predecessor=None,
        reserved_refusal_codes=PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES,
    )
    assert law.verdict == "accepted", law.diagnostics


def _schema_1_registration() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "interfaces": [
            {
                "interface_id": "local.increment",
                "interface_digest": "sha256:" + "9" * 64,
                "predecessors": [],
            }
        ],
        "runtime_requirements": [],
    }


def _schema_1_manifest() -> dict[str, Any]:
    manifest = copy.deepcopy(MANIFEST)
    manifest["schema_version"] = 1
    manifest["supported_protocol_majors"] = [1]
    manifest["implementations"][0].update(
        {
            "interface_id": "local.increment",
            "interface_digest": "sha256:" + "9" * 64,
            "declared_input_buckets": ["size=one"],
            "bucket_conformance": {"size=one": "integer"},
            "capture_contract_families": [],
            "side_effects": False,
        }
    )
    return manifest


def _with(**changes: Any) -> dict[str, Any]:
    registration = copy.deepcopy(REGISTRATION)
    registration.update(changes)
    return registration


@pytest.mark.parametrize(
    ("label", "kwargs", "match"),
    [
        (
            "a schema-2 registration (operations that carry no shapes)",
            {"registration": {**_with(), "schema_version": 2}},
            "registration.json schema 2 is not read",
        ),
        (
            "a schema-1 registration (interfaces, vocabularies, classifiers)",
            {"registration": _schema_1_registration()},
            "registration.json schema 1 is not read",
        ),
        (
            "a schema-1 manifest (interface rows, bucket claims, side effects)",
            {"manifest": _schema_1_manifest()},
            "provider manifest schema 1 is not read",
        ),
        (
            "entry points keyed the 1.0 way, one per interface",
            {"entry_points": {"local.increment": ENTRYPOINT}},
            "entry points differ from the registered operations",
        ),
        (
            "an operation declaring a code the runtime reserves",
            {
                "registration": _with(
                    operations=[
                        _operation(
                            "local.double",
                            refusals={"budget_wall_clock": {"contract": "test.refusal@1"}},
                        ),
                        _operation("local.increment"),
                    ]
                )
            },
            "reserves",
        ),
        (
            "an operation naming an entrypoint the manifest lacks",
            {
                "registration": _with(
                    operations=[
                        _operation("local.double", entrypoint=f"{MODULE}.operation:Other"),
                        _operation("local.increment"),
                    ]
                ),
                "entry_points": {
                    "local.double": f"{MODULE}.operation:Other",
                    "local.increment": ENTRYPOINT,
                },
            },
            "does not export",
        ),
        (
            "a runtime requirement keyed by an interface id",
            {
                "registration": _with(
                    runtime_requirements=[
                        {
                            "interface_id": "local.increment",
                            "kind": "python_extra",
                            "name": "engine",
                            "description": "an engine",
                        }
                    ]
                )
            },
            "entrypoint",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and " " in value else None,
)
def test_a_package_outside_schema_3_or_disagreeing_with_itself_is_refused(
    tmp_path: Path, label: str, kwargs: dict[str, Any], match: str
) -> None:
    del label
    with pytest.raises(ValueError, match=match):
        read_wheel_registration(build_wheel(tmp_path, **kwargs))
