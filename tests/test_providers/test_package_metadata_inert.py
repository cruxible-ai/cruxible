"""A provider package's bucket metadata is inert: read, carried, never judged.

Each wheel here is a provider-runtime 0.2.x package whose vocabulary, fixtures
and selector claims the pre-removal installer refused (no fixture for a declared
selector, a selector its fixture does not match, a measured bucket the
vocabulary does not know, a vocabulary the bucket model rejects, no declared
bucket at all). Core's reader reads it, lowering registers it, and the interface
and Provider laws accept it -- with the provider runtime made unimportable, so
none of its bucket routines can run.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    ProviderInterfaceRegistration,
    evaluate_provider_interface_law,
    parse_provider_interface,
    provider_external_interface_definition_digest,
    provider_interface_digest,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    evaluate_provider_law,
    provider_path,
)
from cruxible_core.providers.package_metadata import read_wheel_registration

NAME = "local-inert"
MODULE = "local_inert"
VERSION = "0.2.0"
DEFINITION = {
    "interface_id": "local.inert",
    "version": 1,
    "effect_class": "pure",
    "contracts": {
        "input": {"fields": {"n": {"type": "int"}}, "allow_extra": False},
        "output": {"fields": {"n": {"type": "int"}}, "allow_extra": False},
    },
}
VOCABULARY = {
    "interface_id": "local.inert",
    "version": 1,
    "status": "draft",
    "description": "One integer operation",
    "dimensions": [
        {
            "name": "size",
            "description": "input shape",
            "classes": [{"id": "one", "description": "one integer"}],
        }
    ],
}

#: Each case's bucket metadata, and why the pre-removal installer refused it.
CASES: dict[str, dict[str, Any]] = {
    "no fixture for a declared selector": {
        "declared_input_buckets": ["size=one"],
        "bucket_conformance": {},
        "fixtures": [
            {"fixture_id": "integer", "canonical_input": {"n": 2}, "measured_bucket_id": "size=one"}
        ],
    },
    "a selector its fixture does not match": {
        "declared_input_buckets": ["size=two"],
        "bucket_conformance": {"size=two": "integer"},
        "fixtures": [
            {"fixture_id": "integer", "canonical_input": {"n": 2}, "measured_bucket_id": "size=one"}
        ],
    },
    "a measured bucket the vocabulary does not know": {
        "declared_input_buckets": ["size=*"],
        "bucket_conformance": {"size=*": "integer"},
        "fixtures": [
            {
                "fixture_id": "integer",
                "canonical_input": {"n": 2},
                "measured_bucket_id": "weight=huge;size=",
            }
        ],
    },
    "a claim naming a fixture the package does not ship": {
        "declared_input_buckets": ["size=one"],
        "bucket_conformance": {"size=one": "absent"},
        "fixtures": [],
    },
    "a vocabulary the bucket model rejects": {
        "vocabulary": {"interface_id": "local.inert", "dimensions": []},
        "declared_input_buckets": ["size=one"],
        "bucket_conformance": {"size=one": "integer"},
        "fixtures": [
            {"fixture_id": "integer", "canonical_input": {"n": 2}, "measured_bucket_id": "size=one"}
        ],
    },
    "no declared bucket at all": {
        "declared_input_buckets": [],
        "bucket_conformance": {},
        "fixtures": [],
    },
}


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def build_inert_wheel(directory: Path, metadata: dict[str, Any]) -> Path:
    """A provider-runtime 0.2.x wheel, written by hand, carrying ``metadata``."""

    interface_digest = provider_external_interface_definition_digest(
        canonical_bytes(DEFINITION).hex(), domain="cruxible.interface.stub.v1"
    )
    files: dict[str, bytes] = {
        f"{MODULE}/__init__.py": b"",
        f"{MODULE}/operation.py": b"def classify(value):\n    raise RuntimeError('never run')\n",
        f"{MODULE}/contract.json": json.dumps(DEFINITION).encode(),
        f"{MODULE}/vocabulary.json": json.dumps(metadata.get("vocabulary", VOCABULARY)).encode(),
        f"{MODULE}/fixtures.json": json.dumps(metadata["fixtures"]).encode(),
    }
    manifest = {
        "schema_version": 1,
        "provider_id": NAME,
        "distribution": {"name": NAME, "version": VERSION},
        "entrypoint_group": "cruxible.providers",
        "supported_protocol_majors": [1],
        "implementations": [
            {
                "interface_id": "local.inert",
                "interface_digest": interface_digest,
                "entrypoint": f"{MODULE}.operation:Inert",
                "backends": ["local_env"],
                "requires_extras": [],
                "declared_input_buckets": metadata["declared_input_buckets"],
                "bucket_conformance": metadata["bucket_conformance"],
                "declared_endpoints": [],
                "capture_contract_families": [],
                "deterministic": True,
                "side_effects": False,
            }
        ],
    }
    files[f"{MODULE}/manifest.json"] = json.dumps(manifest).encode()

    def reference(name: str) -> dict[str, str]:
        return {"path": name, "digest": _digest(files[f"{MODULE}/{name}"])}

    files[f"{MODULE}/registration.json"] = json.dumps(
        {
            "schema_version": 1,
            "manifest": reference("manifest.json"),
            "interfaces": [
                {
                    "interface_id": "local.inert",
                    "interface_digest": interface_digest,
                    "definition": reference("contract.json"),
                    "vocabulary": reference("vocabulary.json"),
                    "classifier": {
                        "identity": "local.inert.input",
                        "version": 1,
                        "entrypoint": f"{MODULE}.operation:classify",
                        "source": reference("operation.py"),
                    },
                    "fixtures": reference("fixtures.json"),
                    "predecessors": [],
                }
            ],
            "runtime_requirements": [],
        }
    ).encode()
    info = f"{MODULE}-{VERSION}.dist-info"
    files[f"{info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {VERSION}\n".encode()
    )
    files[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{info}/entry_points.txt"] = (
        f"[cruxible.providers]\nlocal.inert = {MODULE}.operation:Inert\n".encode()
    )
    files[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in [*files, f"{info}/RECORD"]).encode()
    wheel = directory / f"{MODULE}-{VERSION}-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return wheel


@pytest.fixture
def no_provider_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``cruxible_provider_runtime`` import fail: its bucket routines
    (``load_registration``, ``BucketSelector``, ``parse_bucket_id``) cannot run."""

    for module in [name for name in sys.modules if name.startswith("cruxible_provider_runtime")]:
        monkeypatch.delitem(sys.modules, module)
    monkeypatch.setitem(sys.modules, "cruxible_provider_runtime", None)


@pytest.mark.usefixtures("no_provider_runtime")
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_package_whose_bucket_metadata_was_refused_reads_registers_and_is_accepted(
    tmp_path: Path, case: str
) -> None:
    metadata = CASES[case]
    document = read_wheel_registration(build_inert_wheel(tmp_path, metadata))
    (exported,) = document.interfaces
    assert exported.classifier_code.entrypoint == f"{MODULE}.operation:classify"
    assert [item.fixture_id for item in exported.fixtures] == [
        row["fixture_id"] for row in metadata["fixtures"]
    ]

    (registration,) = document.interface_registrations()
    assert isinstance(registration, ProviderInterfaceRegistration)
    path = provider_interface_path(registration.interface_id)
    content = render_provider_interface(registration)
    assert parse_provider_interface(content, path=path) == registration
    # Only claims naming a shipped fixture become proof records; none is judged.
    shipped = {row["fixture_id"] for row in metadata["fixtures"]}
    assert [(proof.selector, proof.fixture_id) for proof in registration.conformance_proofs] == [
        (selector, fixture_id)
        for selector, fixture_id in sorted(metadata["bucket_conformance"].items())
        if fixture_id in shipped
    ]
    law = evaluate_provider_interface_law(registration, path=path, predecessor=None)
    assert law.verdict == "accepted", law.diagnostics

    accepted = AcceptedProviderInterfaceRegistration(
        path=path,
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )
    provider = document.provider_definition(
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
        interfaces=(registration,),
    )
    provider_law = evaluate_provider_law(
        provider,
        path=provider_path(provider.identity.name),
        predecessor=None,
        interface_registrations={registration.identity.qualified: accepted},
    )
    assert provider_law.verdict == "accepted", provider_law.diagnostics
